# SPDX-License-Identifier: Apache-2.0

"""Putting the chosen packages into the container.

Separate from `setup` because it is a different question. Setup is what has to
happen for the container to work at all and nobody is asked about it; this is
somebody choosing what they want in it, and the answer may be nothing.

Nothing here imports the client: what it needs from the phone arrives as
arguments, so it runs in the tests against a container in a temporary folder.
"""

import os

from . import layout, native, packages, sandbox, writes

# a download of tens of megabytes over whatever connection the phone has
TIMEOUT = 1800

# where a pip install puts what it installs, inside the container
PIP_BIN_DIRS = ("root/.local/bin", "usr/bin", "usr/local/bin")

# what fastfetch is told to look like, and where it reads that from. A phone is
# forty columns wide and the logo every desktop shows is a third of that.
FASTFETCH_CONFIG = "fastfetch.jsonc"
FASTFETCH_HOME = "root/.config/fastfetch"


def wanted(root, selection):
    """The chosen packages that are not in the container already."""
    return [name for name in selection.packages()
            if not present(root, name)]


def anything_to_do(root, selection):
    return bool(wanted(root, selection))


def present(root, name):
    """Is this package in the container, whichever way it got there?

    Alpine keeps a database and is asked. pip keeps one this side cannot read
    without running Python in the container, which is a process per question —
    so what is looked for is the thing the package puts on the PATH, which is
    what having it installed actually means to somebody typing.
    """
    item = _package(name)
    if item is None or _kind_of(name) == packages.APK:
        return layout.installed_package(root, name)
    return _command_present(root, item.command)


def _command_present(root, command):
    for directory in PIP_BIN_DIRS:
        if os.path.exists(os.path.join(root, directory, command)):
            return True
    return False


def _package(name):
    for item in packages.GROUPS:
        found = item.package(name)
        if found is not None:
            return found
    return None


def _kind_of(name):
    for item in packages.GROUPS:
        if item.package(name) is not None:
            return item.kind
    return packages.APK


def installed_groups(root):
    """The groups the container already has in full.

    What a group needs may be there without anybody ticking it, and a group
    that is complete is not worth offering: both answers come from here.
    """
    found = []
    for item in packages.GROUPS:
        if item.names and all(present(root, name) for name in item.names):
            found.append(item.name)
    return found


def usable(root):
    """Groups that count as satisfying somebody else's requirement.

    Partly installed is enough: a container with python3 in it can have a
    Python tool put into it whether or not pip is there as well, because uv
    brings its own.
    """
    found = []
    for item in packages.GROUPS:
        if any(present(root, name) for name in item.names):
            found.append(item.name)
    return found


class Outcome(object):
    def __init__(self, ok, detail, installed=()):
        self.ok = bool(ok)
        self.detail = detail or ""
        self.installed = tuple(installed)

    def sentence(self):
        if self.ok:
            text = ("%d packages installed" % len(self.installed)
                    if self.installed else "everything was already there")
            # `ok` with something to say: everything asked for is there and
            # the package manager still complained on the way out
            return "%s, but %s" % (text, self.detail) if self.detail else text
        return self.detail or "the packages could not be installed"


def install(res_dir, state_dir, root, abi, linker, selection,
            on_progress=None, on_output=None, runner=None):
    """Fetches what the selection asks for and the container lacks.

    Two kinds of package and therefore two kinds of install: everything Alpine
    has in one `apk add`, and whatever is only on PyPI one `uv tool install` at
    a time — uv when the container has it, pip when it does not.

    Never raises. `on_progress(fraction, label)` is called as the numbers come
    in; `on_output(text)` is given the output as it is written, for a console
    to show.
    """
    names = wanted(root, selection)
    if not names:
        return Outcome(True, "already there")
    runner = runner or _runner(res_dir, state_dir, root, abi, linker)
    if runner is None:
        return Outcome(False, "the container cannot run anything yet")

    from_apk = [name for name in names if _kind_of(name) == packages.APK]
    from_pip = [name for name in names if _kind_of(name) == packages.PIP]

    def watch(text):
        if on_output is not None:
            try:
                on_output(text)
            except Exception:
                pass
        if on_progress is None:
            return
        for fraction in _percentages(text):
            try:
                on_progress(fraction, "installing")
            except Exception:
                pass

    listener = watch if (on_progress or on_output) else None
    complaint = None
    if from_apk:
        complaint = _run(runner, ["apk", "add", "--no-cache"] + from_apk,
                         listener)
    for name in from_pip:
        # one at a time: a tool that is not on PyPI should not take the others
        # down with it, and the name of the one that failed is the useful part
        failed = _run(runner, _pip_command(root, name), listener)
        if failed is not None and complaint is None:
            complaint = failed

    # What arrived decides, not what the package manager said on the way out.
    # `apk add unzip` exits 1 when one file of one package could not be
    # extracted — usr/bin/zipinfo, the one hardlink in it — with all sixty
    # packages installed and working. Reporting that as a failed install left
    # `_settle_in` unrun and somebody re-running a command that had already
    # done its job.
    left = [name for name in names if not present(root, name)]
    if left:
        return Outcome(False, complaint.detail if complaint
                       else "%s did not arrive" % ", ".join(left[:3]))
    _settle_in(res_dir, root, names)
    return Outcome(True, complaint.detail if complaint else "", names)


def _run(runner, argv, listener):
    """Runs one install. Returns an Outcome if it went wrong, None if not."""
    try:
        result = runner.run(argv, on_output=listener, size=(80, 24),
                            timeout=TIMEOUT)
    except Exception as e:
        return Outcome(False, "%s: %s" % (type(e).__name__, e))
    if result.status != 0:
        detail = (result.err or result.out or "").strip().splitlines()
        return Outcome(False, detail[-1] if detail else
                       "%s exited %s" % (argv[0], result.status))
    return None


def _pip_command(root, name):
    """uv where the container has it, pip where it does not.

    uv is what the plugin's own build needs anyway, it keeps each tool in a
    virtual environment of its own, and it is very much faster over a phone's
    connection. pip is the fallback, and needs telling that the container's
    Python is not a system it has to protect from us — it is ours.
    """
    if _command_present(root, "uv"):
        return ["uv", "tool", "install", name]
    return ["pip3", "install", "--break-system-packages", name]


def _settle_in(res_dir, root, names):
    """Whatever a package needs beyond being unpacked.

    Only fastfetch so far, and only because the logo every desktop shows is
    wider than half a phone. The config is the plugin's, copied in rather than
    written by hand here, so it can be looked at and changed.
    """
    if "fastfetch" not in names:
        return
    source = os.path.join(res_dir, "config", FASTFETCH_CONFIG)
    if not os.path.exists(source):
        return
    try:
        home = os.path.join(root, FASTFETCH_HOME)
        if not os.path.isdir(home):
            os.makedirs(home)
        target = os.path.join(home, "config.jsonc")
        if os.path.exists(target):
            # somebody has been in here; theirs, not ours
            return
        with open(source, "rb") as read, open(target, "wb") as write:
            write.write(read.read())
    except Exception:
        pass


def _percentages(text):
    """The percentages apk prints, as fractions.

    It draws a bar and rewrites it with carriage returns, so a chunk of its
    output holds several of them; the last one in the chunk is where it is.
    """
    found = []
    for piece in str(text).replace("\r", "\n").split("\n"):
        piece = piece.strip()
        if "%" not in piece:
            continue
        digits = "".join(c for c in piece.split("%")[0] if c.isdigit())
        if digits:
            found.append(min(int(digits), 100) / 100.0)
    return found[-1:]


def _runner(res_dir, state_dir, root, abi, linker):
    from . import backend as backend_module

    if not layout.installed(root) or not layout.saved_strategy(root):
        return None
    measured = writes.load(state_dir) or {}
    runner = backend_module.RootfsBackend(
        root, linker, layout.saved_strategy(root),
        native_dir=native.directory(res_dir, abi),
        blocked=sandbox.blocked_for(state_dir),
        # only the container: apk has no business anywhere else
        mount_rows=[("/", root)], start="/root",
        no_tmpfile=writes.needs_named_temporary(measured),
        timeout=TIMEOUT)
    return runner if runner.available() else None


def describe(root, selection=None):
    """Rows for `rootfs pkg`: what each group is, and what is there."""
    rows = []
    for item in packages.GROUPS:
        missing = [name for name in item.names
                   if not layout.installed_package(root, name)]
        if not missing:
            state = "installed"
        elif len(missing) == len(item.names):
            state = "not installed"
        else:
            state = "%d of %d missing" % (len(missing), len(item.names))
        chosen = "" if selection is None else (
            " *" if selection.is_on(item.name) else "  ")
        rows.append("[%s]%s %-7s %4d MB  %-44s %s"
                    % (" " if missing else "+", chosen, item.name,
                       item.installed, item.summary, state))
    return rows
