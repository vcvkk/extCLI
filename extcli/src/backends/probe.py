# SPDX-License-Identifier: Apache-2.0

"""Runtime capability probe.

exteraGram targets SDK 36, so SELinux forbids execve() of files inside the
app's data directory. That kills the usual "ship proot in the plugin" plan, but
it does not kill everything: /system/bin/sh and the toybox applets next to it
are system files and may be executed, and the dynamic linker can sometimes be
used to run an ELF that lives in a non-executable place.

Rather than guessing, extCLI measures all of it on the device and reports what
is actually available. This module deliberately imports nothing from android or
java so it can be exercised off device; the few Android-only facts are passed
in through `host`.
"""

import json
import os
import shutil
import stat
import subprocess
import time

SH_PATH = "/system/bin/sh"
TOYBOX_PATH = "/system/bin/toybox"
LINKER64_PATH = "/system/bin/linker64"
LINKER32_PATH = "/system/bin/linker"

TIMEOUT = 6

OK = "ok"
BLOCKED = "blocked"
MISSING = "missing"
UNKNOWN = "unknown"


class HostFacts:
    """Android facts the probe cannot read on its own.

    main.py fills this in from compat/; unit tests leave it empty.
    """

    def __init__(self, abi=None, api_level=None, android_release=None,
                 app_version=None, sdk_version=None, native_lib_dir=None):
        self.abi = abi
        self.api_level = api_level
        self.android_release = android_release
        self.app_version = app_version
        self.sdk_version = sdk_version
        self.native_lib_dir = native_lib_dir


def _run(argv, timeout=TIMEOUT, cwd=None):
    """Runs a command, never raises. Returns (returncode, stdout, error)."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=cwd,
        )
        out = proc.stdout.decode("utf-8", "replace").strip()
        return proc.returncode, out, None
    except FileNotFoundError as e:
        return None, "", "not found: %s" % e
    except PermissionError as e:
        return None, "", "permission denied: %s" % e
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %ss" % timeout
    except Exception as e:
        return None, "", "%s: %s" % (type(e).__name__, e)


def check_shell(sh_path=SH_PATH):
    """Can we run the system shell at all?"""
    if not os.path.exists(sh_path):
        return {"status": MISSING, "detail": "%s does not exist" % sh_path}
    code, out, err = _run([sh_path, "-c", "echo extcli-probe"])
    if err:
        return {"status": BLOCKED, "detail": err}
    if code == 0 and "extcli-probe" in out:
        return {"status": OK, "detail": sh_path}
    return {"status": BLOCKED, "detail": "exit %s: %s" % (code, out[:120])}


def check_toybox(toybox_path=TOYBOX_PATH):
    """Which coreutils-ish applets the system shell can reach."""
    if not os.path.exists(toybox_path):
        return {"status": MISSING, "detail": "%s does not exist" % toybox_path,
                "applets": []}
    code, out, err = _run([toybox_path])
    if err or code not in (0, 1):
        return {"status": BLOCKED, "detail": err or "exit %s" % code,
                "applets": []}
    applets = sorted(set(out.replace("\n", " ").split()))
    return {"status": OK if applets else UNKNOWN,
            "detail": "%d applets" % len(applets),
            "applets": applets}


def check_data_exec(workdir):
    """Direct execve() of a file we wrote ourselves.

    Expected to fail on any modern Android; a green result here means proot
    and friends could run straight out of the plugin directory.
    """
    script = os.path.join(workdir, ".probe_exec.sh")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!%s\necho extcli-probe\n" % SH_PATH)
        os.chmod(script, stat.S_IRWXU)
    except Exception as e:
        return {"status": UNKNOWN, "detail": "cannot stage script: %s" % e}

    marked_executable = os.access(script, os.X_OK)
    code, out, err = _run([script])
    try:
        os.remove(script)
    except Exception:
        pass

    if err:
        return {"status": BLOCKED,
                "detail": err,
                "x_bit": marked_executable}
    if code == 0 and "extcli-probe" in out:
        return {"status": OK, "detail": "execve from data dir allowed",
                "x_bit": marked_executable}
    return {"status": BLOCKED, "detail": "exit %s: %s" % (code, out[:120]),
            "x_bit": marked_executable}


PROBE_MARKER = "extcli-probe"

# toybox's own complaints. Seeing one of these means the copy in our data
# directory was mapped and executed — it got far enough to print its usage —
# which is the thing being measured, even though the command itself failed.
_TOYBOX_COMPLAINTS = ("unknown command", "toybox:", "usage:")

# what the linker says when the kernel or SELinux refuses
_LINKER_REFUSALS = ("permission denied", "cannot link", "failed to open",
                    "not permitted", "error: unable")


def interpret_linker_output(code, out, err):
    """Classifies a linker attempt. Pure, so the real device strings can be
    regression-tested without a device."""
    if err:
        return BLOCKED, err
    text = (out or "").lower()
    if PROBE_MARKER in (out or ""):
        return OK, "runs ELFs from the data directory"
    for needle in _LINKER_REFUSALS:
        if needle in text:
            return BLOCKED, out.strip()[:160]
    for needle in _TOYBOX_COMPLAINTS:
        if needle in text:
            # the binary ran; it just disliked how it was invoked
            return OK, "ELF executed (guest complained: %s)" % out.strip()[:90]
    if code == 0:
        return OK, "exit 0"
    return BLOCKED, "exit %s: %s" % (code, (out or "").strip()[:120])


def check_linker(workdir, abi=None):
    """Run a real ELF that lives in our data directory via the dynamic linker.

    execve() of the copy is blocked, but the linker itself is a system file; if
    it will map and start our copy, extCLI can ship native binaries after all —
    which is what a real rootfs depends on. Uses toybox as the guinea pig so no
    binary needs to be bundled.

    The copy is named `toybox` on purpose: it is a multi-call binary that
    dispatches on argv[0], and under any other name it refuses the command
    before doing anything useful.
    """
    linker = LINKER64_PATH
    if abi and "64" not in str(abi):
        linker = LINKER32_PATH
    if not os.path.exists(linker):
        linker = LINKER64_PATH if os.path.exists(LINKER64_PATH) else LINKER32_PATH
    if not os.path.exists(linker):
        return {"status": MISSING, "detail": "no dynamic linker found"}
    if not os.path.exists(TOYBOX_PATH):
        return {"status": UNKNOWN, "detail": "no ELF available to test with"}

    copy = os.path.join(workdir, "toybox")
    try:
        shutil.copyfile(TOYBOX_PATH, copy)
        os.chmod(copy, stat.S_IRWXU)
    except Exception as e:
        return {"status": UNKNOWN, "detail": "cannot stage ELF: %s" % e}

    code, out, err = _run([linker, copy, "echo", PROBE_MARKER])
    try:
        os.remove(copy)
    except Exception:
        pass

    status, detail = interpret_linker_output(code, out, err)
    return {"status": status, "detail": "%s: %s" % (linker, detail)}


def check_pty():
    """A pty is what makes the terminal a real terminal rather than a pipe.

    Deliberately does not fork. Forking a live Android app to test a feature is
    a good way to deadlock on a runtime lock or leave a zombie behind; allocating
    a pty pair and closing it proves the same thing — the kernel gives us one and
    the terminal ioctls exist.
    """
    missing = []
    for name in ("pty", "termios", "fcntl", "select"):
        try:
            __import__(name)
        except Exception as e:
            missing.append("%s (%s)" % (name, e))
    if missing:
        return {"status": MISSING, "detail": "modules unavailable: %s"
                % ", ".join(missing)}

    master = slave = None
    try:
        master, slave = os.openpty()
        import termios

        termios.tcgetattr(slave)
        return {"status": OK, "detail": "pty pair allocated, termios works"}
    except Exception as e:
        return {"status": BLOCKED, "detail": "%s: %s" % (type(e).__name__, e)}
    finally:
        for fd in (master, slave):
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass


def run(workdir, host=None):
    """Runs every check. `workdir` must be writable (extCLI's tmp dir)."""
    host = host or HostFacts()
    started = time.time()
    try:
        os.makedirs(workdir, exist_ok=True)
    except Exception:
        pass

    checks = {}
    checks["shell"] = check_shell()
    checks["toybox"] = check_toybox()
    checks["pty"] = check_pty()
    checks["data_exec"] = check_data_exec(workdir)
    checks["linker"] = check_linker(workdir, host.abi)

    result = {
        "version": 1,
        "time": started,
        "duration": round(time.time() - started, 3),
        "host": {
            "abi": host.abi,
            "api_level": host.api_level,
            "android": host.android_release,
            "app_version": host.app_version,
            "sdk_version": host.sdk_version,
        },
        "checks": checks,
        "backends": [],
    }
    result["backends"] = available_backends(result)
    return result


def available_backends(result):
    """Backend names usable on this device, best first.

    `inproc` is unconditional — it is the reason extCLI still has a working
    shell on a device where everything else is denied.
    """
    checks = result.get("checks", {})
    out = []
    if checks.get("shell", {}).get("status") == OK:
        out.append("system")
    out.append("inproc")
    if checks.get("linker", {}).get("status") == OK:
        out.append("linker")
    return out


def rootfs_verdict(result):
    """One line on whether a real Alpine/proot userspace is reachable."""
    checks = result.get("checks", {})
    if checks.get("data_exec", {}).get("status") == OK:
        return "possible: data-dir execve is allowed, proot can run directly"
    if checks.get("linker", {}).get("status") == OK:
        return "possible via linker: binaries run through the dynamic linker"
    return "not available: execve blocked and no fallback found"


# ------------------------------------------------------------------ reporting

_STATUS_MARK = {OK: "+", BLOCKED: "x", MISSING: "-", UNKNOWN: "?"}

# the report is read in a terminal-width dialog, and details can be long file
# paths or exception texts; the untouched values stay in the JSON result
MAX_LINE = 96


def _clip(text, limit=MAX_LINE):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summary_lines(result, extra_checks=None):
    """Plain-text report; the console and the diagnostics dialog share it.

    `extra_checks` is an optional list of (name, ok, detail) from components
    outside the probe — the dex renderer, for instance — so everything the user
    might need to report appears in one block.
    """
    host = result.get("host", {})
    lines = []
    lines.append("extCLI diagnostics")
    lines.append("")
    lines.append("client   %s (sdk %s)" % (host.get("app_version") or "?",
                                           host.get("sdk_version") or "?"))
    lines.append("android  %s (api %s)" % (host.get("android") or "?",
                                           host.get("api_level") or "?"))
    lines.append("abi      %s" % (host.get("abi") or "?"))
    lines.append("")
    for name in ("shell", "toybox", "pty", "data_exec", "linker"):
        check = result.get("checks", {}).get(name)
        if not check:
            continue
        mark = _STATUS_MARK.get(check.get("status"), "?")
        detail = _clip(check.get("detail", ""), MAX_LINE - 15)
        lines.append("[%s] %-9s %s" % (mark, name, detail))
    for name, ok, detail in (extra_checks or []):
        mark = _STATUS_MARK[OK] if ok else _STATUS_MARK[BLOCKED]
        lines.append("[%s] %-9s %s" % (mark, name, _clip(detail, MAX_LINE - 15)))
    lines.append("")
    lines.append("backends %s" % ", ".join(result.get("backends", [])))
    lines.append("rootfs   %s" % _clip(rootfs_verdict(result), MAX_LINE - 9))
    lines.append("")
    lines.append("probe took %ss" % result.get("duration"))
    return lines


# ------------------------------------------------------------------- caching

CACHE_NAME = "probe.json"


def cache_path(state_dir):
    return os.path.join(state_dir, CACHE_NAME)


def load_cached(state_dir, app_version=None):
    """Cached result, or None when absent or stale (client was updated)."""
    path = cache_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if app_version and data.get("host", {}).get("app_version") != app_version:
        return None
    return data


def save_cached(state_dir, result):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(cache_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return True
    except Exception:
        return False


def get(workdir, state_dir, host=None, force=False):
    """Cached probe result, running the checks only when needed."""
    host = host or HostFacts()
    if not force:
        cached = load_cached(state_dir, host.app_version)
        if cached:
            return cached
    result = run(workdir, host)
    save_cached(state_dir, result)
    return result
