# SPDX-License-Identifier: Apache-2.0

"""Can a process we started start another one?

The plugin can already run its own ELF binaries: `/system/bin/linker64 <elf>`
works, measured on the device. A rootfs needs more than that. A shell inside a
rootfs spends its life exec'ing other guest binaries, and those execs are made
by the guest process, not by us — so the question that decides whether Alpine is
possible is not "can we exec" but "can what we exec, exec".

Three questions, in order of what they rule in:

  system   a process of ours runs /system/bin/echo — the ordinary case, and if
           this fails nothing else matters
  direct   it runs one of our binaries by path, the way any shell would. If
           this works, proot works and a normal rootfs follows
  wrapped  it runs one of our binaries through the linker, the way we do. If
           only this works, a rootfs is still possible, but every exec inside
           it has to be rewritten — which is a different, much larger job

The reading of the answers is a pure function so the verdict can be pinned in
tests against the strings a real device produced.
"""

import os
import shutil
import stat
import subprocess

MARKER = "extcli-exec-probe"

# The parent process every question is asked through. It has to be a real
# shell: Android's toybox has no `sh` applet — the system shell is mksh, a
# separate binary — so using toybox as the parent produced "Unknown command sh"
# for all three questions and read as a flat refusal from the device. It was
# the experiment that had failed, not the exec.
SHELL_CANDIDATES = ("/system/bin/sh", "/bin/sh")

# how each question turned out
OK = "ok"
BLOCKED = "blocked"
UNKNOWN = "unknown"

# what the harness itself did; without this a broken experiment is
# indistinguishable from a device that refuses everything
CONTROL = "control"

# what the rootfs can be, given the answers
NONE = "none"
DIRECT = "direct"
WRAPPED = "wrapped"

TIMEOUT = 15


def verdict(results):
    """Turns the three answers into what kind of rootfs this device allows.

    Returns (strategy, sentence). Deliberately blunt: a maybe here costs weeks
    of building something that cannot run. Which is exactly why the control
    comes first — announcing that a device forbids everything, when really the
    test program never started, is the more expensive mistake of the two.
    """
    control = results.get(CONTROL, {})
    if control.get("status") not in (None, OK):
        return NONE, ("the experiment did not run: %s. Nothing has been "
                      "learned about this device yet"
                      % (control.get("detail") or "the test shell would not start"))
    system = results.get("system", {}).get("status")
    direct = results.get("direct", {}).get("status")
    wrapped = results.get("wrapped", {}).get("status")

    if system != OK:
        return NONE, ("our processes cannot start any other program, so no "
                      "rootfs is possible on this device")
    if direct == OK:
        return DIRECT, ("guest binaries can exec each other, so proot and an "
                        "ordinary rootfs will work")
    if wrapped == OK:
        return WRAPPED, ("guest binaries run only through the linker. proot "
                         "will not do, but extCLI's own shell starts every "
                         "command itself, so a rootfs still works")
    if direct == UNKNOWN or wrapped == UNKNOWN:
        return NONE, "the exec experiments did not complete; run them again"
    return NONE, ("nothing our processes start can start anything of ours, so "
                  "a rootfs would be a single program with no children")


def read_attempt(code, out, err):
    """Did the child actually run? Pure; the strings come from the device."""
    text = "%s\n%s" % (out or "", err or "")
    if MARKER in text:
        return OK, "child ran"
    lowered = text.lower()
    # the program ran and did not understand the request: the experiment is
    # wrong, not the device. Saying "blocked" here is how a missing toybox
    # applet turned into "no rootfs is possible on this device"
    for needle in ("unknown command", "usage:", "--help"):
        if needle in lowered:
            return UNKNOWN, _first_line(text) or needle
    for needle in ("permission denied", "not permitted", "eacces",
                   "can't execute", "cannot execute", "text file busy",
                   "no such file", "not executable"):
        if needle in lowered:
            return BLOCKED, _first_line(text) or needle
    if code == 0:
        # exit 0 without the marker means the command did not do what was asked
        return UNKNOWN, _first_line(text) or "exit 0 without output"
    return BLOCKED, _first_line(text) or ("exit %s" % code)


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


QUESTIONS = ("system", "direct", "wrapped")


def _all(status, detail):
    return {name: {"status": status, "detail": detail} for name in QUESTIONS}


def find_shell(exists=os.path.exists):
    for path in SHELL_CANDIDATES:
        if exists(path):
            return path
    return None


def run(workdir, linker, shell=None, runner=None):
    """Runs the experiments. Needs a device; returns a result per name.

    The parent is a copy of the system shell started through the linker — the
    same position a rootfs's own shell would be in. The control proves that
    parent runs at all before any conclusion is drawn from what it can spawn.
    """
    runner = runner or _run
    if not linker or not os.path.exists(linker):
        return _all(UNKNOWN, "no dynamic linker")
    source = shell or find_shell()
    if not source:
        return _all(UNKNOWN, "no system shell to test with")

    copy = os.path.join(workdir, "sh")
    try:
        os.makedirs(workdir, exist_ok=True)
        shutil.copyfile(source, copy)
        os.chmod(copy, stat.S_IRWXU)
    except Exception as e:
        return _all(UNKNOWN, "cannot stage the shell: %s" % e)

    results = {}
    code, out, err = runner([linker, copy, "-c", "echo %s" % MARKER])
    status, detail = read_attempt(code, out, err)
    results[CONTROL] = {"status": status, "detail": detail}
    if status != OK:
        results.update(_all(UNKNOWN, "the test shell itself did not start"))
        _remove(copy)
        return results

    # the parent is ours and running; each script asks it to start something
    # else, and the child is what is being measured
    scripts = {
        "system": "/system/bin/echo %s" % MARKER,
        "direct": "%s -c 'echo %s'" % (copy, MARKER),
        "wrapped": "%s %s -c 'echo %s'" % (linker, copy, MARKER),
    }
    for name in QUESTIONS:
        script = scripts[name]
        code, out, err = runner([linker, copy, "-c", script])
        status, detail = read_attempt(code, out, err)
        results[name] = {"status": status, "detail": detail, "command": script}

    _remove(copy)
    return results


def _remove(path):
    try:
        os.remove(path)
    except Exception:
        pass


LABELS = {
    CONTROL: "our own shell starts at all",
    "system": "run a system program",
    "direct": "run our binary by path",
    "wrapped": "run our binary via the linker",
}


def summary_lines(results):
    """The matrix, for `rootfs check`."""
    marks = {OK: "+", BLOCKED: "x", UNKNOWN: "?"}
    lines = []
    for name in (CONTROL,) + QUESTIONS:
        if name not in results:
            continue
        result = results[name]
        status = result.get("status", UNKNOWN)
        lines.append("[%s] %-30s %s" % (marks.get(status, "?"), LABELS[name],
                                        result.get("detail", "")))
    strategy, sentence = verdict(results)
    lines.append("")
    lines.append("rootfs: %s" % sentence)
    lines.append("strategy: %s" % strategy)
    return lines


def _run(command):
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   universal_newlines=True)
        out, err = process.communicate(timeout=TIMEOUT)
        return process.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:
            pass
        return 124, "", "timed out"
    except Exception as e:
        return 126, "", "%s: %s" % (type(e).__name__, e)
