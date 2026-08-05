# SPDX-License-Identifier: Apache-2.0

"""Common file utilities implemented in Python.

This is the backend that always works. On a device where /system/bin/sh runs it
is barely used — toybox's own `ls` and `grep` know more flags than these ever
will — but if the system shell is unavailable, or a future Android tightens
something further, the console still has a working toolset instead of
"command not found" for everything.
"""

import fnmatch
import os
import shutil
import stat
import time

from .system import Result


class InprocBackend(object):
    name = "inproc"

    def __init__(self, paths=None):
        # how a path in the shell's terms becomes one on this machine; inert
        # when there is no rootfs, and then the two are the same thing
        self.paths = paths
        self.commands = {
            "ls": _ls,
            "cat": _cat,
            "head": _head,
            "tail": _tail,
            "wc": _wc,
            "grep": _grep,
            "find": _find,
            "mkdir": _mkdir,
            "rm": _rm,
            "cp": _cp,
            "mv": _mv,
            "touch": _touch,
            "stat": _stat,
            "du": _du,
            "basename": _basename,
            "dirname": _dirname,
            "sleep": _sleep,
            "seq": _seq,
        }

    def available(self):
        return True

    def has(self, name):
        return name in self.commands

    def which(self, name):
        return "builtin:%s" % name if name in self.commands else None

    def run(self, argv, stdin_text="", cwd=None, env=None, timeout=None,
            on_output=None, size=None, feed=False, on_channel=None):
        # nothing here takes long enough to be worth watching happen
        del on_output, size, feed, on_channel
        if not argv:
            return Result(127, "", "no command given")
        handler = self.commands.get(argv[0])
        if handler is None:
            return Result(127, "", "%s: not found" % argv[0])
        context = _Context(cwd or os.getcwd(), stdin_text or "", self.paths)
        try:
            return handler(argv[1:], context)
        except FileNotFoundError as e:
            return Result(1, "", "%s: %s" % (argv[0], e.strerror or e))
        except PermissionError as e:
            return Result(1, "", "%s: permission denied: %s" % (argv[0], e.filename))
        except IsADirectoryError:
            return Result(1, "", "%s: is a directory" % argv[0])
        except Exception as e:
            return Result(1, "", "%s: %s: %s" % (argv[0], type(e).__name__, e))

    def commands_list(self):
        return sorted(self.commands)

    def describe(self):
        return [("commands", " ".join(sorted(self.commands)))]


class _Context(object):
    """What a file builtin is handed.

    `cwd` is in the shell's own terms — the guest's, when there is a rootfs —
    and `resolve` is the one place that turns those into somewhere real, so a
    builtin can go on treating a path as a path.
    """

    def __init__(self, cwd, stdin_text, paths=None):
        self.cwd = cwd
        self.stdin = stdin_text
        self.paths = paths

    def resolve(self, path):
        text = str(path)
        if not text.startswith("/"):
            text = self.cwd.rstrip("/") + "/" + text
        if self.paths is not None:
            return self.paths.host(text)
        return os.path.normpath(text)

    def input_lines(self, paths):
        """Lines from the given files, or from stdin when there are none."""
        if not paths:
            text = self.stdin
            return text.split("\n")[:-1] if text.endswith("\n") else \
                (text.split("\n") if text else [])
        lines = []
        for path in paths:
            with open(self.resolve(path), "r", encoding="utf-8",
                      errors="replace") as handle:
                lines.extend(handle.read().split("\n"))
            if lines and lines[-1] == "":
                lines.pop()
        return lines


def _flags(args, known):
    """Splits -abc style flags from positional arguments."""
    flags = set()
    positional = []
    for arg in args:
        if arg.startswith("-") and len(arg) > 1 and not os.path.exists(arg):
            for char in arg[1:]:
                if char in known:
                    flags.add(char)
                else:
                    positional.append(arg)
                    break
            continue
        positional.append(arg)
    return flags, positional


def _ok(text=""):
    if text and not text.endswith("\n"):
        text += "\n"
    return Result(0, text)


# ------------------------------------------------------------------- commands

def _ls(args, ctx):
    flags, paths = _flags(args, "laH")
    targets = paths or ["."]
    out = []
    multiple = len(targets) > 1
    for target in targets:
        path = ctx.resolve(target)
        if os.path.isdir(path):
            names = sorted(os.listdir(path))
            if "a" not in flags:
                names = [name for name in names if not name.startswith(".")]
            if multiple:
                out.append("%s:" % target)
            if "l" in flags:
                out.extend(_long_entry(os.path.join(path, name), name)
                           for name in names)
            else:
                out.extend(names)
        elif os.path.exists(path):
            out.append(_long_entry(path, target) if "l" in flags else target)
        else:
            return Result(1, "\n".join(out), "ls: %s: no such file" % target)
        if multiple:
            out.append("")
    return _ok("\n".join(out))


def _long_entry(path, name):
    try:
        info = os.lstat(path)
    except OSError:
        return "?????????  ?  %s" % name
    kind = "d" if stat.S_ISDIR(info.st_mode) else \
        ("l" if stat.S_ISLNK(info.st_mode) else "-")
    permissions = stat.filemode(info.st_mode)[1:]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.st_mtime))
    return "%s%s %8d %s %s" % (kind, permissions, info.st_size, when, name)


def _cat(args, ctx):
    if not args:
        return _ok(ctx.stdin)
    out = []
    for path in args:
        with open(ctx.resolve(path), "r", encoding="utf-8", errors="replace") as f:
            out.append(f.read())
    return _ok("".join(out))


def _head(args, ctx):
    count, paths = _count_flag(args, 10)
    lines = ctx.input_lines(paths)
    return _ok("\n".join(lines[:count]))


def _tail(args, ctx):
    count, paths = _count_flag(args, 10)
    lines = ctx.input_lines(paths)
    return _ok("\n".join(lines[-count:] if count else []))


def _count_flag(args, default):
    count = default
    paths = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-n" and i + 1 < len(args):
            try:
                count = int(args[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if arg.startswith("-") and arg[1:].isdigit():
            count = int(arg[1:])
            i += 1
            continue
        paths.append(arg)
        i += 1
    return count, paths


def _wc(args, ctx):
    flags, paths = _flags(args, "lwc")
    lines = ctx.input_lines(paths)
    text = "\n".join(lines)
    counts = {
        "l": len(lines),
        "w": len(text.split()),
        "c": len(text) + (1 if lines else 0),
    }
    order = [flag for flag in "lwc" if flag in flags] or ["l", "w", "c"]
    return _ok(" ".join(str(counts[flag]) for flag in order))


def _grep(args, ctx):
    flags, rest = _flags(args, "invlc")
    if not rest:
        return Result(2, "", "grep: no pattern given")
    pattern, paths = rest[0], rest[1:]
    needle = pattern.lower() if "i" in flags else pattern
    matches = []
    for line in ctx.input_lines(paths):
        haystack = line.lower() if "i" in flags else line
        hit = needle in haystack
        if "v" in flags:
            hit = not hit
        if hit:
            matches.append(line)
    if "c" in flags:
        return _ok(str(len(matches)))
    if not matches:
        return Result(1, "")
    return _ok("\n".join(matches))


def _find(args, ctx):
    flags, rest = _flags(args, "")
    # resolve(".") rather than cwd: cwd is in the shell's terms, which are the
    # guest's when there is a rootfs, and this is about to open a directory
    root = ctx.resolve(rest[0]) if rest else ctx.resolve(".")
    name_pattern = None
    if "-name" in rest:
        index = rest.index("-name")
        if index + 1 < len(rest):
            name_pattern = rest[index + 1]
    out = []
    for directory, subdirectories, files in os.walk(root):
        entries = [directory] + [os.path.join(directory, f) for f in sorted(files)]
        for entry in entries:
            if name_pattern and not fnmatch.fnmatch(os.path.basename(entry),
                                                    name_pattern):
                continue
            out.append(entry)
        subdirectories.sort()
    return _ok("\n".join(out))


def _mkdir(args, ctx):
    flags, paths = _flags(args, "p")
    if not paths:
        return Result(2, "", "mkdir: no directory given")
    for path in paths:
        target = ctx.resolve(path)
        if "p" in flags:
            os.makedirs(target, exist_ok=True)
        else:
            os.mkdir(target)
    return _ok()


def _rm(args, ctx):
    flags, paths = _flags(args, "rf")
    if not paths:
        return Result(2, "", "rm: no path given")
    for path in paths:
        target = ctx.resolve(path)
        if os.path.isdir(target) and not os.path.islink(target):
            if "r" not in flags:
                return Result(1, "", "rm: %s is a directory" % path)
            shutil.rmtree(target, ignore_errors=("f" in flags))
        elif os.path.exists(target) or os.path.islink(target):
            os.remove(target)
        elif "f" not in flags:
            return Result(1, "", "rm: %s: no such file" % path)
    return _ok()


def _cp(args, ctx):
    flags, paths = _flags(args, "r")
    if len(paths) < 2:
        return Result(2, "", "cp: need a source and a destination")
    *sources, destination = paths
    target = ctx.resolve(destination)
    for source in sources:
        origin = ctx.resolve(source)
        if os.path.isdir(origin):
            if "r" not in flags:
                return Result(1, "", "cp: %s is a directory" % source)
            shutil.copytree(origin, os.path.join(target, os.path.basename(origin))
                            if os.path.isdir(target) else target)
        else:
            shutil.copy2(origin, target)
    return _ok()


def _mv(args, ctx):
    if len(args) < 2:
        return Result(2, "", "mv: need a source and a destination")
    *sources, destination = args
    target = ctx.resolve(destination)
    for source in sources:
        shutil.move(ctx.resolve(source), target)
    return _ok()


def _touch(args, ctx):
    if not args:
        return Result(2, "", "touch: no path given")
    for path in args:
        target = ctx.resolve(path)
        with open(target, "a", encoding="utf-8"):
            os.utime(target, None)
    return _ok()


def _stat(args, ctx):
    if not args:
        return Result(2, "", "stat: no path given")
    out = []
    for path in args:
        info = os.stat(ctx.resolve(path))
        out.append("%s  size=%d  mode=%s  mtime=%s" % (
            path, info.st_size, oct(stat.S_IMODE(info.st_mode)),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime)),
        ))
    return _ok("\n".join(out))


def _du(args, ctx):
    flags, paths = _flags(args, "sh")
    total = 0
    for path in (paths or ["."]):
        root = ctx.resolve(path)
        if os.path.isfile(root):
            total += os.path.getsize(root)
            continue
        for directory, _subdirectories, files in os.walk(root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(directory, name))
                except OSError:
                    pass
    if "h" in flags:
        return _ok(_human(total))
    return _ok(str(total))


def _human(size):
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return "%.1f%s" % (size, unit) if unit != "B" else "%dB" % size
        size /= 1024.0
    return "%dB" % size


def _basename(args, ctx):
    if not args:
        return Result(2, "", "basename: no path given")
    return _ok(os.path.basename(args[0].rstrip("/")))


def _dirname(args, ctx):
    if not args:
        return Result(2, "", "dirname: no path given")
    return _ok(os.path.dirname(args[0].rstrip("/")) or ".")


def _sleep(args, ctx):
    if not args:
        return Result(2, "", "sleep: no duration given")
    try:
        seconds = float(args[0])
    except ValueError:
        return Result(2, "", "sleep: %s is not a number" % args[0])
    # a console must not be blocked for minutes by a typo
    time.sleep(min(seconds, 5.0))
    return _ok()


def _seq(args, ctx):
    numbers = [int(value) for value in args if value.lstrip("-").isdigit()]
    if not numbers:
        return Result(2, "", "seq: need a count")
    if len(numbers) == 1:
        start, end, step = 1, numbers[0], 1
    elif len(numbers) == 2:
        start, end, step = numbers[0], numbers[1], 1
    else:
        start, end, step = numbers[0], numbers[2], numbers[1]
    if step == 0:
        return Result(2, "", "seq: step cannot be zero")
    values = []
    value = start
    while (value <= end and step > 0) or (value >= end and step < 0):
        values.append(str(value))
        value += step
        if len(values) > 10000:
            break
    return _ok("\n".join(values))
