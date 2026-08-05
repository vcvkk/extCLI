# SPDX-License-Identifier: Apache-2.0

"""Running real programs through /system/bin/sh and the toybox applets.

Measured on exteraGram 12.9.0 (targetSdk 36): the system shell runs, toybox
offers 210 applets, and a pty can be allocated — so extCLI does not need to
reimplement `ls`, `grep` or `find`, it can call the ones already on the device.
What it cannot do is execve a file inside the app's data directory; that is what
the linker backend is for.

Nothing here imports Android APIs, so the whole thing is exercised by the tests
against the host's own /bin/sh.
"""

import os
import subprocess

SH_CANDIDATES = ("/system/bin/sh", "/bin/sh")
TOYBOX_CANDIDATES = ("/system/bin/toybox", "/bin/busybox")

# where Android keeps executables; PATH inside the app is often empty
BIN_DIRS = ("/system/bin", "/system/xbin", "/vendor/bin", "/product/bin",
            "/apex/com.android.runtime/bin", "/bin", "/usr/bin")

DEFAULT_TIMEOUT = 20


class Result(object):
    def __init__(self, status, out="", err=""):
        self.status = int(status)
        self.out = out or ""
        self.err = err or ""

    @property
    def ok(self):
        return self.status == 0

    @property
    def text(self):
        """stdout and stderr merged, which is what a terminal shows."""
        if self.err and self.out:
            return self.out if self.out.endswith("\n") else self.out + "\n" + self.err
        return self.out or self.err


# How long a command whose output is being watched may run. The ordinary
# timeout is for a command whose answer arrives all at once and would otherwise
# hang the console; one that is showing its work is being watched by somebody
# who can see it is still going.
STREAM_TIMEOUT = 300


def open_terminal(columns, rows):
    """A pseudo-terminal for a guest program to write into, or None.

    Not decoration. A program asks the fd it is writing to whether it is a
    terminal and decides everything from the answer: `fastfetch` came out with
    no colour at all, and `ls --color=auto`, `grep --color=auto` and every
    progress bar make the same decision the same way. Down a pipe they are all
    right to; the console is a terminal, and this is how the program gets told
    so.

    The size goes with it. A program that formats to the width of the screen
    has to be told what that is, and a terminal's width is not something an
    environment variable can be trusted to carry.
    """
    try:
        import fcntl
        import pty
        import struct
        import termios
    except Exception:
        return None
    try:
        master, slave = pty.openpty()
    except Exception:
        return None
    try:
        settings = termios.tcgetattr(slave)
        # No echo: whatever is written to this terminal would otherwise come
        # straight back as output, and the console has already shown the line
        # it sent. And no NL-to-CRNL: the console draws the lines it is given,
        # and a carriage return in one of them is a character it has to strip.
        settings[3] &= ~termios.ECHO
        settings[1] &= ~termios.ONLCR
        termios.tcsetattr(slave, termios.TCSANOW, settings)
    except Exception:
        pass
    try:
        size = struct.pack("HHHH", int(rows) or 24, int(columns) or 80, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
    except Exception:
        pass
    return master, slave


def stream(argv, stdin_text, cwd, environment, timeout, on_output,
           columns=0, rows=0, feed=False, on_channel=None):
    """Runs a process and hands each line over as it arrives.

    stderr is merged into stdout on purpose. The console shows them in one
    stream anyway, and keeping them apart would mean holding one back until the
    other had finished — which is the thing being fixed.

    The text is delivered, not returned: whoever asked for it has already had
    it, and returning it as well would print everything twice.

    `feed` says the program is being handed text by something else — the left
    side of a pipe — rather than by whoever is typing. It matters because a
    terminal has no end-of-file: `ls | grep yaml` gave grep the terminal as its
    stdin, wrote ls's output into it, and grep sat waiting for an end that a
    terminal cannot deliver. Fed programs get a pipe, which can be closed; the
    terminal stays on their output, where it is what makes colour and width
    work.

    `on_channel` is offered a way to write into that terminal while the program
    runs, and given None when it ends. That is what typing at a running program
    is.
    """
    import time

    terminal = open_terminal(columns, rows) if columns else None
    master, slave = terminal if terminal else (None, None)
    if slave is not None:
        # never the app's own stdin: a program that read it would wait on
        # something nobody can type into
        source = subprocess.PIPE if feed else slave
    else:
        source = subprocess.PIPE if stdin_text else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            argv,
            stdin=source,
            stdout=slave if slave is not None else subprocess.PIPE,
            stderr=slave if slave is not None else subprocess.STDOUT,
            cwd=cwd or None,
            env=environment,
            universal_newlines=slave is None,
            bufsize=1 if slave is None else 0,
            start_new_session=slave is not None,
        )
    except FileNotFoundError:
        _shut(master, slave)
        return Result(127, "", "%s: not found" % argv[0])
    except PermissionError as e:
        _shut(master, slave)
        return Result(126, "", "%s: %s" % (argv[0], e))
    except Exception as e:
        _shut(master, slave)
        return Result(1, "", "%s: %s: %s" % (argv[0], type(e).__name__, e))

    if terminal:
        return _stream_terminal(process, master, slave, stdin_text, timeout,
                                on_output, argv, feed, on_channel)

    deadline = time.time() + (timeout or STREAM_TIMEOUT)
    try:
        if process.stdin is not None:
            # small inputs only, which is what a console line is; a large one
            # written before anything is read could fill the pipe and wait
            try:
                if stdin_text:
                    process.stdin.write(stdin_text)
                process.stdin.close()
            except Exception:
                pass
        for line in process.stdout:
            on_output(line)
            if time.time() > deadline:
                process.kill()
                on_output("%s: timed out after %ss\n"
                          % (argv[0], timeout or STREAM_TIMEOUT))
                break
        process.wait()
    except Exception as e:
        try:
            process.kill()
        except Exception:
            pass
        return Result(1, "", "%s: %s: %s" % (argv[0], type(e).__name__, e))
    return Result(process.returncode or 0, "", "")


class Channel(object):
    """A way to reach a program while it is running.

    Called, it types at the program. It also carries the two ways of stopping
    one, which a terminal has and this console did not: ^C did nothing here
    because the child has no controlling terminal, so the tty driver has no
    foreground process group to send SIGINT to. We send it ourselves instead.

    To the group, not the process. What is started is the loader, and the guest
    it supervises is its child; signalling only the loader would leave the
    program that is actually running untouched. `start_new_session` put the
    pair in a group of their own, which is what makes this safe — nothing else
    of the app's is in it.
    """

    def __init__(self, master, process):
        self._master = master
        self._process = process

    def __call__(self, text):
        import os as _os

        try:
            _os.write(self._master, (text or "").encode("utf-8"))
            return True
        except Exception:
            return False

    def resize(self, columns, rows):
        """Tells the program its screen is a different size now.

        The keyboard coming and going changes the height of a terminal on a
        phone the way dragging a window's corner does on a desktop, and a
        full-screen program has no way of noticing on its own. Setting the size
        on the pty makes the kernel send SIGWINCH to whatever is in the
        foreground, which is the signal every such program already listens for
        — so it measures itself again and redraws.
        """
        import fcntl
        import signal
        import struct
        import termios

        if not columns or not rows:
            return False
        try:
            size = struct.pack("HHHH", int(rows), int(columns), 0, 0)
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, size)
        except Exception:
            return False
        # And the signal by hand, for the same reason ^C is sent by hand: the
        # child has no controlling terminal, so the tty driver has no
        # foreground process group to tell. Setting the size alone left the
        # new one there to be read by a program with no reason to look.
        self._signal(signal.SIGWINCH)
        return True

    def raw(self):
        """Is the program reading keys rather than lines?

        The pty knows: a program that wants every keystroke turns the line
        discipline off — no line editing, no echo — which is the same ICANON
        flag `stty -icanon` clears. Asking it beats guessing from the name of
        the program, and it is right the moment nano starts and again the
        moment it leaves.
        """
        import termios

        try:
            return not (termios.tcgetattr(self._master)[3] & termios.ICANON)
        except Exception:
            return False

    def _signal(self, number):
        import os as _os

        if self._process.poll() is not None:
            return False
        try:
            _os.killpg(_os.getpgid(self._process.pid), number)
            return True
        except Exception:
            try:
                self._process.send_signal(number)
                return True
            except Exception:
                return False

    def interrupt(self):
        """^C: asks the program to stop, and lets it decide how."""
        import signal

        return self._signal(signal.SIGINT)

    def stop(self):
        """And when it will not, or cannot — the loader is a ptrace supervisor
        and its tracee dies with it, so this reaches both."""
        import signal

        return self._signal(signal.SIGKILL)

    def running(self):
        return self._process.poll() is None


def _shut(*handles):
    import os as _os

    for handle in handles:
        if handle is None:
            continue
        try:
            _os.close(handle)
        except Exception:
            pass


def _stream_terminal(process, master, slave, stdin_text, timeout, on_output,
                     argv, feed=False, on_channel=None):
    """Reads a pseudo-terminal until the program on the other end is done.

    A terminal has no end-of-file to wait for the way a pipe does: when the
    last program holding it exits, reading it fails with EIO, and that is the
    signal. Anything else would either stop early or wait forever.
    """
    import os as _os
    import time

    # the child holds it now; keeping a copy here would mean the read never
    # ended, because this process would still be something to talk to
    _shut(slave)
    deadline = time.time() + (timeout or STREAM_TIMEOUT)

    channel = Channel(master, process)

    try:
        if feed:
            # the whole of it, then the end of it — which is the part a
            # terminal could not have given
            try:
                if stdin_text:
                    process.stdin.write(stdin_text.encode("utf-8"))
                process.stdin.close()
            except Exception:
                pass
        elif stdin_text:
            try:
                _os.write(master, stdin_text.encode("utf-8"))
            except Exception:
                pass
        if on_channel is not None and not feed:
            on_channel(channel)
        while True:
            try:
                chunk = _os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            on_output(chunk.decode("utf-8", "replace").replace("\r\n", "\n"))
            if time.time() > deadline:
                # the group: the loader's tracee is the program that is
                # actually running, and killing only the loader leaves it
                channel.stop()
                on_output("%s: timed out after %ss\n"
                          % (argv[0], timeout or STREAM_TIMEOUT))
                break
        process.wait()
    except Exception as e:
        try:
            channel.stop()
        except Exception:
            pass
        return Result(1, "", "%s: %s: %s" % (argv[0], type(e).__name__, e))
    finally:
        # before the terminal is closed under it, and whatever went wrong
        if on_channel is not None and not feed:
            try:
                on_channel(None)
            except Exception:
                pass
        _shut(master)
    return Result(process.returncode or 0, "", "")


class SystemBackend(object):
    """External commands via subprocess. Interactive ptys come later."""

    name = "system"

    def __init__(self, shell=None, bin_dirs=None, timeout=DEFAULT_TIMEOUT):
        self.shell = shell or self._find(SH_CANDIDATES)
        self.bin_dirs = tuple(bin_dirs or BIN_DIRS)
        self.timeout = timeout
        self._toybox = self._find(TOYBOX_CANDIDATES)
        self._applets = None
        self._which_cache = {}

    @staticmethod
    def _find(candidates):
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def available(self):
        return bool(self.shell)

    # --------------------------------------------------------------- lookup

    def applets(self):
        """Applet names the multi-call binary provides."""
        if self._applets is not None:
            return self._applets
        self._applets = frozenset()
        if not self._toybox:
            return self._applets
        try:
            proc = subprocess.run([self._toybox], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=5)
            text = proc.stdout.decode("utf-8", "replace")
            self._applets = frozenset(text.replace("\n", " ").split())
        except Exception:
            pass
        return self._applets

    def which(self, name):
        """Absolute path of a command, or None.

        Falls back to the toybox multiplexer: many Android builds ship applets
        without a symlink for every one of them.
        """
        if name in self._which_cache:
            return self._which_cache[name]
        found = None
        if "/" in name:
            if os.path.isfile(name) and os.access(name, os.X_OK):
                found = name
        else:
            for directory in self.bin_dirs:
                candidate = os.path.join(directory, name)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    found = candidate
                    break
            if found is None and name in self.applets():
                found = self._toybox
        self._which_cache[name] = found
        return found

    def has(self, name):
        return self.which(name) is not None

    # ------------------------------------------------------------- execution

    def run(self, argv, stdin_text="", cwd=None, env=None, timeout=None,
            on_output=None, size=None, feed=False, on_channel=None):
        """Runs a command directly (no shell), returning a Result."""
        if not argv:
            return Result(127, "", "no command given")
        name = argv[0]
        path = self.which(name)
        if path is None:
            return Result(127, "", "%s: not found" % name)
        # a multi-call binary needs the applet name as its first argument
        if os.path.basename(path) != name and path == self._toybox:
            argv = [path, name] + list(argv[1:])
        else:
            argv = [path] + list(argv[1:])
        return self._spawn(argv, stdin_text, cwd, env, timeout, on_output,
                           size, feed, on_channel)

    def run_shell(self, command, stdin_text="", cwd=None, env=None, timeout=None):
        """Runs a command line through the system shell.

        Used by `sh -c`-style invocations and by anything that needs the real
        shell's own parsing rather than ours.
        """
        if not self.shell:
            return Result(127, "", "no system shell available")
        return self._spawn([self.shell, "-c", command], stdin_text, cwd, env, timeout)

    def _spawn(self, argv, stdin_text, cwd, env, timeout, on_output=None,
               size=None, feed=False, on_channel=None):
        environment = dict(os.environ)
        if env:
            environment.update({str(k): str(v) for k, v in env.items()})
        environment.setdefault("PATH", ":".join(self.bin_dirs))
        if on_output is not None:
            columns, rows = size or (0, 0)
            return stream(argv, stdin_text, cwd, environment, timeout,
                          on_output, columns, rows, feed, on_channel)
        try:
            proc = subprocess.run(
                argv,
                input=(stdin_text or "").encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or None,
                env=environment,
                timeout=timeout or self.timeout,
            )
        except FileNotFoundError:
            return Result(127, "", "%s: not found" % argv[0])
        except PermissionError as e:
            return Result(126, "", "%s: %s" % (argv[0], e))
        except subprocess.TimeoutExpired:
            return Result(124, "", "%s: timed out after %ss"
                          % (argv[0], timeout or self.timeout))
        except Exception as e:
            return Result(1, "", "%s: %s: %s" % (argv[0], type(e).__name__, e))
        return Result(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    def describe(self):
        rows = [("shell", self.shell or "unavailable")]
        if self._toybox:
            rows.append(("multicall", "%s (%d applets)"
                         % (self._toybox, len(self.applets()))))
        rows.append(("path", ":".join(d for d in self.bin_dirs if os.path.isdir(d))))
        return rows
