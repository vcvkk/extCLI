# SPDX-License-Identifier: Apache-2.0

"""Commands that only make sense inside the shell itself.

These have to be builtins rather than programs: `cd` must change *this* shell's
directory, `export` must alter the environment the next command inherits, and
`source` must run in the current scope.
"""

import os

from ...render import blocks
from ..registry import Command, CommandError, parse_flags


class CdCommand(Command):
    name = "cd"
    summary = "change directory"
    usage = "cd [path]"

    def run(self, ctx, args):
        env = _env(ctx)
        target = args[0] if args else "~"
        if target == "-":
            target = env.get("OLDPWD") or env.cwd
        previous = env.cwd
        ok, detail = env.chdir(target)
        if not ok:
            raise CommandError("cd: %s" % detail)
        env.set("OLDPWD", previous)
        return blocks.Result()

    def complete(self, ctx, args):
        return _complete_paths(ctx, args, directories_only=True)


class PwdCommand(Command):
    name = "pwd"
    summary = "print the working directory"
    usage = "pwd"

    def run(self, ctx, args):
        return blocks.text(_env(ctx).cwd)


class ExportCommand(Command):
    name = "export"
    summary = "mark variables for external commands"
    usage = "export [NAME[=value] ...]"

    def run(self, ctx, args):
        env = _env(ctx)
        if not args:
            lines = ["%s=%s" % (name, env.get(name)) for name in sorted(env.exported)]
            return blocks.text(lines) if lines else blocks.summary("nothing exported")
        for argument in args:
            name, _, value = argument.partition("=")
            if not name:
                raise CommandError("export: invalid name %r" % argument)
            env.export(name, value if "=" in argument else None)
        return blocks.Result()


class UnsetCommand(Command):
    name = "unset"
    summary = "remove variables or functions"
    usage = "unset NAME ..."

    def run(self, ctx, args):
        if not args:
            raise CommandError("unset: needs a name", hint=self.usage)
        env = _env(ctx)
        for name in args:
            env.unset(name)
        return blocks.Result()


class SetCommand(Command):
    name = "set"
    summary = "list variables"
    usage = "set"

    def run(self, ctx, args):
        env = _env(ctx)
        lines = ["%s=%s" % (name, env.variables[name])
                 for name in sorted(env.variables)]
        return blocks.text(lines)


class AliasCommand(Command):
    name = "alias"
    summary = "define or list aliases"
    usage = "alias [name='command']"

    def run(self, ctx, args):
        env = _env(ctx)
        if not args:
            if not env.aliases:
                return blocks.summary("no aliases")
            return blocks.text(["%s='%s'" % (name, env.aliases[name])
                                for name in sorted(env.aliases)])
        for argument in args:
            if "=" not in argument:
                value = env.alias(argument)
                if value is None:
                    raise CommandError("alias: %s not found" % argument)
                return blocks.text("%s='%s'" % (argument, value))
            name, _, value = argument.partition("=")
            env.set_alias(name, value)
        return blocks.Result()


class UnaliasCommand(Command):
    name = "unalias"
    summary = "remove an alias"
    usage = "unalias name ..."

    def run(self, ctx, args):
        if not args:
            raise CommandError("unalias: needs a name", hint=self.usage)
        env = _env(ctx)
        for name in args:
            if not env.unset_alias(name):
                raise CommandError("unalias: %s not found" % name)
        return blocks.Result()


class SourceCommand(Command):
    name = "source"
    summary = "run a script in this shell"
    usage = "source <file>"
    mutating = True

    def run(self, ctx, args):
        if not args:
            raise CommandError("source: needs a file", hint=self.usage)
        env = _env(ctx)
        path = env.host(args[0])
        if not os.path.isfile(path):
            raise CommandError("source: no such file: %s" % args[0])
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except UnicodeDecodeError:
            raise CommandError("source: %s is not text" % args[0])
        except OSError as e:
            raise CommandError("source: cannot read %s: %s"
                               % (args[0], e.strerror or e))
        runner = getattr(ctx, "run_script_text", None)
        if runner is None:
            raise CommandError("source: no shell available here")
        return runner(text)

    def complete(self, ctx, args):
        return _complete_paths(ctx, args)


class TrueCommand(Command):
    name = "true"
    summary = "do nothing, successfully"
    usage = "true"

    def run(self, ctx, args):
        return blocks.Result()


class FalseCommand(Command):
    name = "false"
    summary = "do nothing, unsuccessfully"
    usage = "false"

    def run(self, ctx, args):
        return blocks.Result(code=1)


class TestCommand(Command):
    """`test` / `[` — the conditional every shell script uses."""

    name = "test"
    summary = "evaluate a condition"
    usage = "test <expression>   or   [ <expression> ]"

    UNARY = {
        "-e": lambda p: _exists(p),
        "-f": lambda p: _isfile(p),
        "-d": lambda p: _isdir(p),
        "-s": lambda p: _nonempty(p),
        "-r": lambda p: _readable(p),
        "-w": lambda p: _writable(p),
        "-n": lambda s: bool(s),
        "-z": lambda s: not s,
    }

    def run(self, ctx, args):
        args = list(args)
        if args and args[-1] == "]":
            args.pop()
        return blocks.Result(code=0 if self._evaluate(ctx, args) else 1)

    def _evaluate(self, ctx, args):
        env = _env(ctx)
        if not args:
            return False
        if len(args) == 1:
            return bool(args[0])
        if args[0] == "!":
            return not self._evaluate(ctx, args[1:])
        if len(args) == 2:
            check = self.UNARY.get(args[0])
            if check is None:
                return bool(args[1])
            if args[0] in ("-n", "-z"):
                return check(args[1])
            return check(env.host(args[1]))
        left, operator, right = args[0], args[1], args[2]
        return _compare(left, operator, right)


def _compare(left, operator, right):
    if operator in ("=", "=="):
        return left == right
    if operator == "!=":
        return left != right
    numeric = {"-eq": "==", "-ne": "!=", "-gt": ">", "-ge": ">=",
               "-lt": "<", "-le": "<="}
    if operator in numeric:
        try:
            a, b = int(left), int(right)
        except ValueError:
            return False
        return {
            "-eq": a == b, "-ne": a != b, "-gt": a > b,
            "-ge": a >= b, "-lt": a < b, "-le": a <= b,
        }[operator]
    return False


def _exists(path):
    import os

    return os.path.exists(path)


def _isfile(path):
    import os

    return os.path.isfile(path)


def _isdir(path):
    import os

    return os.path.isdir(path)


def _nonempty(path):
    import os

    return os.path.isfile(path) and os.path.getsize(path) > 0


def _readable(path):
    import os

    return os.access(path, os.R_OK)


def _writable(path):
    import os

    return os.access(path, os.W_OK)


class WhichCommand(Command):
    name = "which"
    summary = "show what would run"
    usage = "which <name>"

    def run(self, ctx, args):
        if not args:
            raise CommandError("which: needs a name", hint=self.usage)
        name = args[0]
        registry = getattr(ctx, "registry", None)
        if registry is not None and registry.get(name) is not None:
            return blocks.text("%s: extCLI builtin" % name)
        env = _env(ctx)
        if env.alias(name):
            return blocks.text("%s: alias for %s" % (name, env.alias(name)))
        if env.function(name) is not None:
            return blocks.text("%s: shell function" % name)
        backend = getattr(ctx, "backend", None)
        if backend is not None:
            path = backend.which(name)
            if path:
                owner = backend.owner(name) if hasattr(backend, "owner") else None
                detail = "%s (%s)" % (path, owner) if owner else path
                return blocks.text("%s: %s" % (name, detail))
        raise CommandError("which: %s not found" % name)


class EnvCommand(Command):
    name = "env"
    summary = "show the environment external commands get"
    usage = "env"

    def run(self, ctx, args):
        env = _env(ctx)
        values = env.environment()
        return blocks.text(["%s=%s" % (name, values[name])
                            for name in sorted(values)])


class BackendsCommand(Command):
    name = "backends"
    summary = "what can run external commands here"
    usage = "backends"

    def run(self, ctx, args):
        backend = getattr(ctx, "backend", None)
        if backend is None:
            return blocks.summary("no execution backend")
        rows = backend.describe() if hasattr(backend, "describe") else []
        names = [b.name for b in getattr(backend, "backends", []) if b.available()]
        return blocks.Result([
            blocks.Fields(rows) if rows else blocks.Text("no details"),
            blocks.Summary("active: %s" % (", ".join(names) or "none")),
        ])


def _env(ctx):
    env = getattr(ctx, "env", None)
    if env is None:
        raise CommandError("this command needs a shell session")
    return env


def _complete_paths(ctx, args, directories_only=False):
    import os

    env = getattr(ctx, "env", None)
    if env is None:
        return []
    prefix = args[-1] if args else ""
    directory = os.path.dirname(prefix) or "."
    base = os.path.basename(prefix)
    try:
        entries = os.listdir(env.host(directory))
    except Exception:
        return []
    out = []
    for entry in sorted(entries):
        if not entry.startswith(base):
            continue
        full = os.path.join(directory, entry) if directory != "." else entry
        if directories_only and not os.path.isdir(env.host(full)):
            continue
        out.append(full + "/" if os.path.isdir(env.host(full)) else full)
    return out


def build_all():
    return [
        CdCommand(), PwdCommand(), ExportCommand(), UnsetCommand(), SetCommand(),
        AliasCommand(), UnaliasCommand(), SourceCommand(), TrueCommand(),
        FalseCommand(), TestCommand(), WhichCommand(), EnvCommand(),
        BackendsCommand(),
    ]
