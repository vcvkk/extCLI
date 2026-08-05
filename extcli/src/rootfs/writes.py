# SPDX-License-Identifier: Apache-2.0

"""How a package manager may write a file here.

`apk update` fetched its index and then answered "Permission denied", and the
trace named the call: `linkat`, twice, once per repository. apk-tools 3 writes
a file the modern way — create it with O_TMPFILE, so it exists with no name and
cannot be seen half-written, then `linkat` it into place through
/proc/self/fd/N. That is the documented unprivileged way to do it and it works
on any ordinary Linux.

Whether it works *here* is a fact about this device's kernel and its SELinux
policy, which is not something to reason about from a desk: the last two
guesses in this project were both wrong and both cost a round trip. So it is
measured, from the plugin's own process — same uid, same domain, same
filesystem as the guest — and the three ways of writing a file are tried in the
order a program would fall back through them.

Nothing here writes anything a user would miss: it works in a directory of its
own and takes it away afterwards.
"""

import os

TMPFILE = "tmpfile"
LINKAT = "linkat"
RENAME = "rename"

ORDER = (TMPFILE, LINKAT, RENAME)

LABELS = {
    TMPFILE: "create an unnamed file (O_TMPFILE)",
    LINKAT: "link it into place (/proc/self/fd)",
    RENAME: "write a named temporary and rename it",
}

OK = "ok"
FAILED = "failed"
UNSUPPORTED = "unsupported"


def run(directory):
    """Tries each way. Returns {name: (status, detail)}.

    The directory is made and removed here, so this can be pointed at a rootfs
    without leaving anything in it.
    """
    results = {}
    workspace = os.path.join(directory, ".extcli-write-test")
    try:
        os.makedirs(workspace)
    except FileExistsError:
        pass
    except Exception as e:
        return {name: (FAILED, "%s: %s" % (type(e).__name__, e))
                for name in ORDER}

    handle = None
    try:
        flags = getattr(os, "O_TMPFILE", None)
        if flags is None:
            results[TMPFILE] = (UNSUPPORTED, "this Python has no O_TMPFILE")
        else:
            try:
                handle = os.open(workspace, flags | os.O_RDWR, 0o600)
                os.write(handle, b"extcli")
                results[TMPFILE] = (OK, "created")
            except Exception as e:
                handle = None
                results[TMPFILE] = (FAILED, _reason(e))

        target = os.path.join(workspace, "linked")
        if handle is None:
            results[LINKAT] = (UNSUPPORTED, "nothing to link")
        else:
            try:
                os.link("/proc/self/fd/%d" % handle, target,
                        follow_symlinks=True)
                results[LINKAT] = (OK, "linked")
                os.unlink(target)
            except Exception as e:
                results[LINKAT] = (FAILED, _reason(e))

        # the older way, and the one every program falls back to
        try:
            named = os.path.join(workspace, "named.tmp")
            with open(named, "wb") as file:
                file.write(b"extcli")
            os.rename(named, os.path.join(workspace, "renamed"))
            results[RENAME] = (OK, "written and renamed")
        except Exception as e:
            results[RENAME] = (FAILED, _reason(e))
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except Exception:
                pass
        _remove(workspace)
    return results


def _reason(error):
    name = getattr(error, "strerror", None) or str(error)
    number = getattr(error, "errno", None)
    return "%s (errno %s)" % (name, number) if number else name


def _remove(path):
    try:
        for name in os.listdir(path):
            try:
                os.unlink(os.path.join(path, name))
            except Exception:
                pass
        os.rmdir(path)
    except Exception:
        pass


def summary_lines(results):
    marks = {OK: "+", FAILED: "x", UNSUPPORTED: "-"}
    lines = []
    for name in ORDER:
        status, detail = results.get(name, (UNSUPPORTED, "not tried"))
        lines.append("[%s] %-38s %s" % (marks.get(status, "?"), LABELS[name],
                                        detail))
    return lines


def verdict(results):
    """What this means for a package manager.

    It says what was measured rather than what it sounds like. The first
    version of this sentence called the failure a refusal; it is EXDEV, a
    cross-device link, and a reader who takes "refused" at face value goes
    looking for a permission that was never involved.
    """
    def status(name):
        return results.get(name, (UNSUPPORTED, ""))[0]

    def detail(name):
        return results.get(name, (UNSUPPORTED, ""))[1]

    if status(LINKAT) == OK:
        return True, ("a package manager can write files the way apk does, so "
                      "whatever stopped it is not this")
    if status(RENAME) == OK:
        reason = detail(LINKAT) or "it fails"
        return False, ("an unnamed file cannot be linked into place here (%s), "
                       "and a named temporary can — so the guest is told this "
                       "filesystem has no unnamed files, which sends apk down "
                       "the way that works" % reason)
    return False, "nothing can write a file here, which is a bigger problem"


# The measurement is kept, like the syscall scan: it describes the device, it
# is the same answer every time, and everything that starts a guest needs it.
FILE = "writes"
HEADER = "extcli-writes 1"


def encode(results):
    return " ".join("%s=%s" % (name, results.get(name, (UNSUPPORTED, ""))[0])
                    for name in ORDER)


def decode(text):
    found = {}
    for part in (text or "").split():
        name, _, status = part.partition("=")
        if name in ORDER and status:
            found[name] = (status, "")
    return found


def save(state_dir, results):
    try:
        if not os.path.isdir(state_dir):
            os.makedirs(state_dir)
        with open(os.path.join(state_dir, FILE), "w", encoding="utf-8") as f:
            f.write("%s\n%s\n" % (HEADER, encode(results)))
        return True
    except Exception:
        return False


def load(state_dir):
    try:
        with open(os.path.join(state_dir, FILE), "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    if not lines or lines[0].strip() != HEADER:
        return None
    found = decode(lines[1] if len(lines) > 1 else "")
    return found or None


def needs_named_temporary(results):
    """Should the guest be told this filesystem has no unnamed files?

    Only when linking one into place is what fails and the older way works —
    a program told to fall back to something that also fails is worse off than
    one that was left alone.
    """
    if not results:
        return False

    def status(name):
        return results.get(name, (UNSUPPORTED, ""))[0]

    return status(LINKAT) == FAILED and status(RENAME) == OK
