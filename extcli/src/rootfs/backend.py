# SPDX-License-Identifier: Apache-2.0

"""Running programs that live in the rootfs.

The device's answer was that our processes can start our own binaries only
through the linker, never by path. That rules out proot, whose whole method is
letting the guest exec what it likes and correcting the paths afterwards.

It does not rule out a rootfs, because in extCLI the shell is *ours*. Pipes,
redirections, `if`, `for` and every command in a pipeline are executed by
extcli's own executor, which starts each program itself — so no guest process
ever has to exec another one. `ls | grep x` is two separate launches through
the linker, and the guest never notices it is not running under a shell of its
own.

What this does not give you is a guest shell script: `sh script.sh` inside the
rootfs would have to exec the programs it names, and that is the thing the
device refuses.
"""

import os

from ..backends.system import Result, stream
from . import guest, layout

DEFAULT_TIMEOUT = 30


class RootfsBackend(object):
    """A backend whose commands come out of the installed rootfs."""

    name = "rootfs"
    # the loader rewrites paths at the syscall, so a guest path means what the
    # guest meant by it
    translates = True

    def __init__(self, root, linker, strategy=None, native_dir=None,
                 timeout=DEFAULT_TIMEOUT, blocked=None, mount_rows=None,
                 start=None, no_tmpfile=False):
        self.root = root
        self.linker = linker
        self.strategy = strategy or layout.saved_strategy(root)
        self.native_dir = native_dir
        self.timeout = timeout
        # what the device's syscall filter refuses; the loader turns those
        # calls into answers instead of letting them kill the guest
        self.blocked = blocked or None
        # what the guest can see, and where it opens; both are settings
        self.mount_rows = list(mount_rows) if mount_rows else None
        self.start = start
        # whether an unnamed file can be linked into place here; when it cannot,
        # the guest is better off not being offered one
        self.no_tmpfile = bool(no_tmpfile)
        self._loader = None
        self._which_cache = {}

    @property
    def bin_dirs(self):
        """Where a command may be, including the home a user installs into.

        The same list for `which` and for PATH, because a shell that can start
        a program the guest's PATH does not name — or the other way round —
        answers "not found" about something that is plainly there.
        """
        from . import mounts

        return layout.bin_dirs(self.start or mounts.HOME)

    def available(self):
        return bool(self.root and self.linker and self.strategy
                    and layout.installed(self.root))

    @property
    def loader(self):
        if self._loader is None:
            self._loader = guest.loader_in(self.root) or False
        return self._loader or None

    def which(self, name):
        """The guest path of a command, as the guest would write it."""
        if not self.available() or not name:
            return None
        if name in self._which_cache:
            return self._which_cache[name]
        found = None
        if name.startswith("/"):
            # resolved the guest's way: most of a rootfs is absolute symlinks
            if layout.resolve(self.root, name):
                found = name
        else:
            for directory in self.bin_dirs:
                if layout.resolve(self.root, "%s/%s" % (directory, name)):
                    found = "%s/%s" % (directory, name)
                    break
        self._which_cache[name] = found
        return found

    def has(self, name):
        return self.which(name) is not None

    def forget(self):
        self._which_cache = {}
        self._loader = None

    def command_for(self, argv):
        """The real argv, guest paths resolved and the linker in front."""
        path = self.which(argv[0]) if argv else None
        if path is None:
            return None
        arguments = [self.translate(word) for word in argv[1:]]
        return guest.command_for(self.strategy, self.root, self.linker,
                                 [path] + arguments, self.loader,
                                 native_dir=self.native_dir, argv0=argv[0])

    @property
    def mapping(self):
        """Does the loader give the guest the rootfs as its own `/`?"""
        return self.strategy == guest.LOADER

    def translate(self, word):
        """An argument as the guest meant it.

        Only for the strategies that hand the guest straight to the kernel.
        Under the loader the supervisor translates at the syscall, where a
        path is known to be a path — here `grep /usr file` and `ls /usr` are
        indistinguishable, and translating the first is wrong.
        """
        if self.mapping or not word.startswith("/"):
            return word
        return layout.translate(self.root, word) or word

    def environment(self, extra=None):
        env = dict(os.environ)
        env.update(guest.environment_for(self.strategy, self.root,
                                         blocked=self.blocked,
                                         mount_rows=self.mount_rows,
                                         no_tmpfile=self.no_tmpfile,
                                         linker=self.linker,
                                         native_dir=self.native_dir,
                                         home=self.start))
        env.update(extra or {})
        # PATH as the guest writes it; our executor resolves through which()
        env["PATH"] = ":".join(self.bin_dirs)
        return env

    def run(self, argv, stdin_text="", cwd=None, env=None, timeout=None,
            on_output=None, size=None, feed=False, on_channel=None):
        import subprocess

        command = self.command_for(argv) if argv else None
        if command is None:
            return Result(127, "", "%s: not in the rootfs"
                          % (argv[0] if argv else ""))
        if on_output is not None:
            columns, rows = size or (0, 0)
            return stream(command, stdin_text, self.host_cwd(cwd),
                          self.environment(env), timeout, on_output,
                          columns, rows, feed, on_channel)
        try:
            process = subprocess.Popen(
                command,
                # never the app's own stdin: a guest that read it would wait
                # for something nobody can type into
                stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.host_cwd(cwd),
                env=self.environment(env),
                universal_newlines=True,
            )
            out, err = process.communicate(stdin_text or None,
                                           timeout=timeout or self.timeout)
            return Result(process.returncode, out, err)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:
                pass
            return Result(124, "", "%s: timed out" % argv[0])
        except Exception as e:
            return Result(126, "", "%s: %s: %s" % (argv[0], type(e).__name__, e))

    def host_cwd(self, cwd):
        """The directory to start the process in, on this machine.

        What arrives is in the shell's terms — `/root` means the rootfs's, and
        handing that to subprocess asks the phone for a directory it does not
        have. That is what `ls /` answered with the first time the console
        stood inside Alpine.
        """
        if not cwd:
            return self.start_host()
        if self.mapping and self.mount_rows:
            from . import mounts

            found = mounts.host_path(self.mount_rows, cwd)
            if found and os.path.isdir(found):
                return found
        return cwd if os.path.isdir(cwd) else self.start_host()

    def start_host(self):
        """Where a guest program begins, as a host path.

        Its own home when the rootfs is mounted, and the first mount that is on
        when it is not — a process has to start in a directory that exists, and
        it may as well be one the user can reach.
        """
        if self.mapping and self.start and self.mount_rows:
            from . import mounts

            found = mounts.host_path(self.mount_rows, self.start)
            if found and os.path.isdir(found):
                return found
        return self.root

    def commands(self, limit=None):
        """What the rootfs offers, by guest path."""
        names = set()
        for directory in self.bin_dirs:
            path = os.path.join(self.root, directory.lstrip("/"))
            try:
                names.update(os.listdir(path))
            except OSError:
                continue
        ordered = sorted(names)
        return ordered[:limit] if limit else ordered

    def describe(self):
        if not self.available():
            reason = "not installed" if not layout.installed(self.root) else (
                "no launch strategy — run `rootfs probe launch`")
            return [("state", reason)]
        return [
            ("root", self.root),
            ("launch", self.strategy),
            ("commands", str(len(self.commands()))),
        ]
