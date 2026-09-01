# SPDX-License-Identifier: Apache-2.0

"""Tries the available backends in order.

`system` first, because toybox's utilities know more flags and edge cases than
anything reimplemented here; `inproc` second, so a device that blocks the system
shell still has a working console. The chain is also what makes the boundary
honest: a command that no backend can run gets one clear "not found" instead of
a different error per backend.
"""

from .inproc import InprocBackend
from .linker import LinkerBackend
from .system import Result, SystemBackend


class ChainBackend(object):
    name = "chain"

    def __init__(self, backends):
        self.backends = [backend for backend in backends if backend is not None]

    def available(self):
        return any(backend.available() for backend in self.backends)

    def which(self, name):
        for backend in self.backends:
            if not backend.available():
                continue
            path = backend.which(name)
            if path:
                return path
        return None

    def has(self, name):
        return self.which(name) is not None

    def owner(self, name):
        """Which backend would run this command; shown by `which`."""
        for backend in self.backends:
            if backend.available() and backend.which(name):
                return backend.name
        return None

    def run(self, argv, stdin_text="", cwd=None, env=None, timeout=None,
            on_output=None, size=None, feed=False, on_channel=None):
        if not argv:
            return Result(127, "", "no command given")
        last = None
        for backend in self.backends:
            if not backend.available() or not backend.which(argv[0]):
                continue
            last = backend.run(argv, stdin_text, cwd, env, timeout,
                               on_output=on_output, size=size, feed=feed,
                               on_channel=on_channel)
            # 127 means this backend could not run it after all; try the next
            if last.status != 127:
                return last
        if last is not None:
            return last
        return Result(127, "", "%s: command not found" % argv[0])

    def run_shell(self, command, stdin_text="", cwd=None, env=None, timeout=None):
        for backend in self.backends:
            runner = getattr(backend, "run_shell", None)
            if runner and backend.available():
                return runner(command, stdin_text, cwd, env, timeout)
        return Result(127, "", "no system shell available")

    def commands(self):
        """Every name any backend would answer to.

        Only used to suggest a correction for a typo, so a backend that cannot
        list itself is skipped rather than being made to.
        """
        names = set()
        for backend in self.backends:
            if not backend.available():
                continue
            # inproc holds its commands in a dict of the same name, so the
            # callable one is asked for by a name only it has
            lister = getattr(backend, "commands_list", None) or \
                getattr(backend, "commands", None)
            try:
                found = lister() if callable(lister) else None
            except Exception:
                continue
            names.update(found or ())
        return sorted(names)

    def describe(self):
        rows = []
        for backend in self.backends:
            if not backend.available():
                continue
            for label, value in backend.describe():
                rows.append(("%s.%s" % (backend.name, label), value))
        return rows


def build(native_dir=None, abi=None, probe_result=None, rootfs=None,
          paths=None):
    """The backend chain for this device.

    Without a rootfs, order matters the way it always did. `system` first:
    toybox knows more flags and edge cases than anything reimplemented here.
    `linker` second, for binaries extCLI ships itself — it only ever claims
    those, so it never shadows a system tool. `inproc` last, so a device that
    blocks the system shell still has a console.

    With one, the shell is inside it, and toybox drops out. Not for taste: the
    shell's paths are the guest's now, and toybox would open `/etc/passwd` on
    the phone. The rootfs backend translates at the syscall and `inproc` is
    given the same map, so those two answer about the same world; a backend
    that cannot would answer about a different one, which is worse than not
    answering.

    `probe_result` is only a hint for logging: the backends check for themselves
    what they can do, so a stale probe cache cannot disable a working shell.

    The last step is a latch rather than a comment. Being inside a rootfs and
    having a backend that can translate are two different facts, and they come
    apart: the paths are settled by what is installed, and the rootfs backend
    is built separately and can fail to appear — no linker on this device, or
    anything else that makes `_build_rootfs` return None. A chain built in that
    state used to keep `system`, and then a guest path went to a backend that
    reads it as a path on the phone: `rm -rf /*` inside the container asked the
    phone to delete its own root. So a chain that is inside a rootfs drops
    every backend that cannot translate, whatever the reason it is there.
    `inproc` always can when it has the map, so the console is never left with
    nothing.
    """
    backends = []
    if rootfs is not None and rootfs.available():
        backends.append(rootfs)
    else:
        backends.append(SystemBackend())
        if native_dir:
            linker = LinkerBackend(native_dir, abi=abi)
            if linker.available():
                backends.append(linker)
    backends.append(InprocBackend(paths=paths))
    if paths is not None and paths.active:
        backends = [backend for backend in backends
                    if getattr(backend, "translates", False)]
    return ChainBackend(backends)
