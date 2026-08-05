# SPDX-License-Identifier: Apache-2.0

"""Running our own binaries through the dynamic linker.

The device measurement that makes this possible: execve of a file inside the
app's data directory is refused — SELinux, and final on targetSdk 36 — but
`/system/bin/linker64 <our-elf> args` runs it. The linker is a system file, so
the kernel is happy to exec *it*, and the linker then maps and starts the ELF
we hand it. That is the only way a plugin can ship native code at all: a plugin
cannot add anything to the client's APK, so the executable native library
directory is out of reach.

What this backend does not get around: a process started this way still cannot
execve files from the data directory itself. Whether that stops a real rootfs
is what rootfs/exec_probe.py measures.
"""

import os

from .system import BIN_DIRS, DEFAULT_TIMEOUT, Result

LINKER64 = "/system/bin/linker64"
LINKER32 = "/system/bin/linker"


def find_linker(abi=None, exists=os.path.exists):
    """The dynamic linker for this ABI, or None.

    Pure enough to test: the filesystem check is injectable, because the answer
    depends on a device layout that cannot be reproduced here.
    """
    if abi and "64" not in str(abi):
        order = (LINKER32, LINKER64)
    else:
        order = (LINKER64, LINKER32)
    for path in order:
        if exists(path):
            return path
    return None


class LinkerBackend(object):
    """External commands from a directory of our own ELF binaries."""

    name = "linker"

    def __init__(self, bin_dir, abi=None, linker=None, timeout=DEFAULT_TIMEOUT,
                 bin_dirs=BIN_DIRS):
        self.bin_dir = bin_dir
        self.linker = linker or find_linker(abi)
        self.timeout = timeout
        self.bin_dirs = tuple(bin_dirs)
        self._which_cache = {}

    def available(self):
        # find_linker already checked that the linker is there; a caller who
        # passes one explicitly has said so themselves
        return bool(self.linker and self.bin_dir and os.path.isdir(self.bin_dir))

    def which(self, name):
        """Only our own binaries. Everything on the system PATH belongs to the
        system backend, which can exec it directly and more cheaply."""
        if not self.available() or not name or "/" in name:
            return None
        if name in self._which_cache:
            return self._which_cache[name]
        path = os.path.join(self.bin_dir, name)
        found = path if os.path.isfile(path) else None
        self._which_cache[name] = found
        return found

    def has(self, name):
        return self.which(name) is not None

    def forget(self):
        """After installing binaries, so a miss is not cached forever."""
        self._which_cache = {}

    def command_for(self, argv):
        """The real argv: the linker, our binary, then the arguments.

        Pure, and the part worth testing — a multi-call binary dispatches on
        argv[0], so the order here decides whether `ls` means anything.
        """
        path = self.which(argv[0])
        if path is None:
            return None
        return [self.linker, path] + list(argv[1:])

    def run(self, argv, stdin_text="", cwd=None, env=None, timeout=None,
            on_output=None, size=None, feed=False, on_channel=None):
        del on_output, size, feed, on_channel
        import subprocess

        command = self.command_for(argv) if argv else None
        if command is None:
            return Result(127, "", "%s: not found" % (argv[0] if argv else ""))
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if stdin_text else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or self.bin_dir,
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

    def environment(self, extra=None):
        """PATH keeps our directory first, so a name we ship wins."""
        env = dict(os.environ)
        env.update(extra or {})
        parts = [self.bin_dir] + [d for d in self.bin_dirs if d != self.bin_dir]
        env["PATH"] = ":".join(parts)
        return env

    def binaries(self):
        try:
            return sorted(name for name in os.listdir(self.bin_dir)
                          if os.path.isfile(os.path.join(self.bin_dir, name)))
        except Exception:
            return []

    def describe(self):
        if not self.available():
            return [("state", "no linker or no binaries directory")]
        names = self.binaries()
        return [
            ("linker", self.linker),
            ("bin", self.bin_dir),
            ("binaries", " ".join(names) if names else "(none installed)"),
        ]
