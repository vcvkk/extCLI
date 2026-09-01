# SPDX-License-Identifier: Apache-2.0

"""Commands and how they are looked up."""

from ..render import blocks

# What everybody types first when they do not know a command. `help <name>`
# has always worked, but nobody arriving from a shell tries that before this.
HELP_FLAGS = ("--help", "-h")


def wants_help(args):
    """Is this argument list asking for help rather than for work?

    Only up to `--`. After it the words belong to the command, and a file
    really can be named `-h`.
    """
    for arg in args:
        if arg == "--":
            return False
        if arg in HELP_FLAGS:
            return True
    return False


class CommandError(Exception):
    """A command failed in a way the user should read, not a traceback.

    `hint` is the "did you mean" line under the error.
    """

    def __init__(self, message, hint=None, code=1):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def as_result(self):
        return blocks.error(self.message, self.hint, self.code)


class Command(object):
    """One command. `run(ctx, args)` returns a blocks.Result."""

    name = ""
    summary = ""
    usage = ""
    # a command that changes something is not run by accident from a chat
    mutating = False

    def dispatch(self, ctx, args):
        """Where a command is entered from outside.

        `--help` is answered here rather than in every command, so having it
        is not something each one has to remember to write.
        """
        if wants_help(args):
            return self.help_result()
        return self.run(ctx, args)

    def run(self, ctx, args):
        raise NotImplementedError

    def complete(self, ctx, args):
        """Candidate completions for the last word; empty by default."""
        return []

    def help_result(self):
        rows = []
        if self.usage:
            rows.append(("usage", self.usage))
        if self.summary:
            rows.append(("about", self.summary))
        return blocks.fields(rows, title=self.name)


class Group(Command):
    """A command with subcommands: `plugin list`, `host status`."""

    # When set, a first argument that is not a subcommand goes to this one
    # instead of being an error — so `plugin config <id>` means what it looks
    # like it means rather than complaining about an unknown subcommand.
    default_subcommand = None

    def __init__(self, name, summary="", subcommands=None):
        self.name = name
        self.summary = summary
        self.subcommands = {}
        for sub in (subcommands or []):
            self.add(sub)

    def add(self, command):
        self.subcommands[command.name] = command
        return self

    def dispatch(self, ctx, args):
        # a named subcommand takes the flags with it, so `plugin list --help`
        # is the list's help and not the group's
        if args and args[0] in self.subcommands:
            return self.subcommands[args[0]].dispatch(ctx, args[1:])
        return Command.dispatch(self, ctx, args)

    def help_result(self):
        # what the subcommands are is the useful answer for a group; the bare
        # usage line would only repeat their names
        return self.overview()

    @property
    def usage(self):
        return "%s <%s>" % (self.name, "|".join(sorted(self.subcommands)))

    def run(self, ctx, args):
        if not args:
            return self.overview()
        name = args[0]
        sub = self.subcommands.get(name)
        if sub is None:
            fallback = self.subcommands.get(self.default_subcommand)
            if fallback is not None:
                return fallback.run(ctx, args)
            raise CommandError(
                "unknown subcommand: %s %s" % (self.name, name),
                hint=suggest(name, self.subcommands.keys(),
                             "%s subcommands: " % self.name),
            )
        return sub.run(ctx, args[1:])

    def overview(self):
        rows = [(name, cmd.summary) for name, cmd in sorted(self.subcommands.items())]
        return blocks.Result([
            blocks.Table(rows),
            blocks.Summary("%s: %d subcommands" % (self.name, len(rows))),
        ])

    def complete(self, ctx, args):
        if len(args) <= 1:
            prefix = args[0] if args else ""
            return sorted(n for n in self.subcommands if n.startswith(prefix))
        sub = self.subcommands.get(args[0])
        return sub.complete(ctx, args[1:]) if sub else []


class Registry(object):
    def __init__(self):
        self._commands = {}
        self._aliases = {}

    def register(self, command, aliases=()):
        self._commands[command.name] = command
        for alias in aliases:
            self._aliases[alias] = command.name
        return command

    def get(self, name):
        real = self._aliases.get(name, name)
        return self._commands.get(real)

    def names(self, include_aliases=False):
        out = set(self._commands)
        if include_aliases:
            out |= set(self._aliases)
        return sorted(out)

    def commands(self):
        return [self._commands[name] for name in sorted(self._commands)]

    def complete(self, ctx, words, trailing_space):
        """Completions for a partially typed line."""
        if not words or (len(words) == 1 and not trailing_space):
            prefix = words[0] if words else ""
            return [n for n in self.names(include_aliases=True) if n.startswith(prefix)]
        command = self.get(words[0])
        if command is None:
            return []
        args = words[1:] + ([""] if trailing_space else [])
        return command.complete(ctx, args)


def suggest(unknown, candidates, prefix="did you mean: "):
    """"did you mean" line, or None when nothing is close."""
    import difflib

    matches = difflib.get_close_matches(str(unknown), sorted(candidates), n=3, cutoff=0.5)
    if not matches:
        return None
    return prefix + ", ".join(matches)


# ------------------------------------------------------------- flag parsing

class Flags(object):
    """Parsed flags plus the leftover positional arguments.

    Deliberately small: commands stay readable, and unknown flags produce a
    normal command error rather than argparse's own exit behaviour.
    """

    def __init__(self, values, positional):
        self.values = values
        self.positional = positional

    def __getitem__(self, name):
        return self.values.get(name)

    def get(self, name, default=None):
        value = self.values.get(name)
        return default if value is None else value

    def has(self, name):
        return bool(self.values.get(name))


def _unknown(arg, spec):
    return CommandError(
        "unknown option: %s" % arg,
        hint=suggest(arg, [k for k in spec if k.startswith("-")], "options: "),
    )


def _typed(flag, raw, kind):
    if kind != "int":
        return raw
    try:
        return int(raw)
    except ValueError:
        raise CommandError("%s needs a number, got %r" % (flag, raw))


def _explode(arg, spec):
    """`-la` as `-l -a`, and `-n5` as `-n 5`.

    Returns None when any letter is not a flag, so the caller can report the
    whole word as unknown rather than blaming one character of it.
    """
    out = []
    for index, letter in enumerate(arg[1:], start=1):
        flag = "-" + letter
        kind = spec.get(flag)
        if kind is None:
            return None
        out.append(flag)
        if kind == "bool":
            continue
        # one that takes a value ends the cluster and takes the rest with it
        rest = arg[index + 1:]
        if rest:
            out.append(rest)
        return out
    return out


def parse_flags(args, spec, command=None):
    """`spec` maps flag -> "bool" | "str" | "int"; aliases go in the same map.

    Deliberately small, but it keeps to the forms anybody coming from a shell
    expects: `--name value` and `--name=value`, short flags on their own or
    run together, `--` to end the options, and a lone `-` or a negative number
    left alone as an argument.
    """
    values = {}
    positional = []
    args = list(args)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1:])
            break
        if arg.startswith("-") and arg != "-" and not _is_negative_number(arg):
            if arg.startswith("--") and "=" in arg:
                flag, raw = arg.split("=", 1)
                kind = spec.get(flag)
                if kind is None:
                    raise _unknown(flag, spec)
                if kind == "bool":
                    raise CommandError("%s takes no value" % flag)
                values[flag] = _typed(flag, raw, kind)
                i += 1
                continue
            if not arg.startswith("--") and len(arg) > 2 and arg not in spec:
                exploded = _explode(arg, spec)
                if exploded is not None:
                    args[i:i + 1] = exploded
                    continue
            kind = spec.get(arg)
            if kind is None:
                raise _unknown(arg, spec)
            if kind == "bool":
                values[arg] = True
                i += 1
                continue
            if i + 1 >= len(args):
                raise CommandError("%s needs a value" % arg)
            values[arg] = _typed(arg, args[i + 1], kind)
            i += 2
            continue
        positional.append(arg)
        i += 1
    return Flags(values, positional)


def _is_negative_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False
