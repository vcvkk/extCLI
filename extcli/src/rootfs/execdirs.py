# SPDX-License-Identifier: Apache-2.0

"""Is there anywhere at all we can write to and then execute from?

Everything else in rootfs/ works around the answer being no: the linker trick,
the launcher, our shell doing the exec'ing. All of that is unnecessary if some
directory the app can write to still permits execve — and one measurement is
cheaper than a week of building around an assumption.

The expectation is that there is not. W^X has applied to app data since API 29
and targetSdk 36 makes it final; external storage is mounted noexec; and
/data/local/tmp belongs to the shell user, not to us. But "expected" is how the
last three of these went, and two of the three surprised us.

A control runs first, as ever: the same ELF from a place that is known to be
executable. If that fails the scan proves nothing.
"""

import os
import shutil
import stat

MARKER = "extcli-execdir"

OK = "ok"
BLOCKED = "blocked"
UNWRITABLE = "unwritable"
UNKNOWN = "unknown"

# a real bionic executable, and one that is on every Android
GUINEA_PIG = "/system/bin/toybox"
# toybox dispatches on argv[0], so the copy has to keep the name
GUINEA_NAME = "toybox"


def read_attempt(code, out, err):
    """Pure; the strings are the device's."""
    text = "%s\n%s" % (out or "", err or "")
    if MARKER in text:
        return OK, "ran"
    lowered = text.lower()
    for needle in ("permission denied", "not permitted", "eacces",
                   "cannot execute", "can't execute", "not executable",
                   "text file busy", "operation not permitted"):
        if needle in lowered:
            # the raw text here is a Python traceback line repeating the path
            # that is already in the row above it — nine of those is a wall
            return BLOCKED, "execve refused"
    if any(needle in lowered for needle in ("unknown command", "usage:")):
        # it ran and disliked the arguments — the experiment's fault, not the
        # directory's, and a distinction that once cost us a wrong verdict
        return UNKNOWN, _first_line(text) or "the guinea pig complained"
    if code == 0:
        return UNKNOWN, _first_line(text) or "exit 0 without output"
    return BLOCKED, _first_line(text) or ("exit %s" % code)


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:140]
    return ""


def try_directory(directory, runner, source=GUINEA_PIG):
    """Copies a real executable in and runs it. Cleans up after itself."""
    if not directory:
        return {"status": UNKNOWN, "detail": "no path"}
    target = os.path.join(directory, GUINEA_NAME)
    try:
        os.makedirs(directory, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, stat.S_IRWXU)
    except PermissionError:
        return {"status": UNWRITABLE, "detail": "cannot write here"}
    except Exception as e:
        return {"status": UNWRITABLE, "detail": "%s: %s" % (type(e).__name__, e)}
    try:
        code, out, err = runner([target, "echo", MARKER])
        status, detail = read_attempt(code, out, err)
    finally:
        try:
            os.remove(target)
        except Exception:
            pass
    return {"status": status, "detail": detail}


def scan(directories, runner, source=GUINEA_PIG):
    """directories: [(label, path)]. Returns {label: result}, control first."""
    results = {}
    if not os.path.exists(source):
        return {"control": {"status": UNKNOWN,
                            "detail": "no %s to test with" % source}}
    code, out, err = runner([source, "echo", MARKER])
    status, detail = read_attempt(code, out, err)
    results["control"] = {"status": status, "detail": "%s from /system" % detail}
    if status != OK:
        return results
    for label, path in directories:
        results[label] = try_directory(path, runner, source)
    return results


def allowed(results):
    """Labels of directories we may write to and then execute from."""
    return [label for label, result in results.items()
            if label != "control" and result.get("status") == OK]


def verdict(results):
    control = results.get("control", {})
    if control.get("status") != OK:
        return None, ("the scan did not run: %s. Nothing has been learned"
                      % (control.get("detail") or "the control failed"))
    found = allowed(results)
    if found:
        return found, ("execve is allowed from %s — a rootfs can run the "
                       "ordinary way, no linker tricks needed"
                       % ", ".join(sorted(found)))
    return [], ("nothing we can write to may be executed, which is what the "
                "linker trick and the launcher are for")


def summary_lines(results, order=None):
    marks = {OK: "+", BLOCKED: "x", UNWRITABLE: "-", UNKNOWN: "?"}
    labels = order or [label for label in results if label != "control"]
    lines = []
    control = results.get("control")
    if control:
        lines.append("[%s] %-22s %s" % (marks.get(control["status"], "?"),
                                        "control (/system/bin)",
                                        control.get("detail", "")))
    for label in labels:
        result = results.get(label)
        if not result:
            continue
        lines.append("[%s] %-22s %s" % (marks.get(result["status"], "?"), label,
                                        result.get("detail", "")))
    lines.append("")
    lines.append(verdict(results)[1])
    return lines
