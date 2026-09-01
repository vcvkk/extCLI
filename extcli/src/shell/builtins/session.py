# SPDX-License-Identifier: Apache-2.0

"""Commands that act on the console session itself."""

from ...render import blocks
from ..registry import Command, CommandError, parse_flags

HISTORY_SHOWN = 20


class ClearCommand(Command):
    name = "clear"
    summary = "wipe the scrollback"
    usage = "clear"

    def run(self, ctx, args):
        ctx.request_clear()
        return blocks.Result()


class ExitCommand(Command):
    name = "exit"
    summary = "close the console"
    usage = "exit"

    def run(self, ctx, args):
        ctx.request_exit()
        return blocks.Result()


class EchoCommand(Command):
    name = "echo"
    summary = "print the arguments"
    usage = "echo [text ...]"

    def run(self, ctx, args):
        return blocks.text(" ".join(args))


class CopyCommand(Command):
    """The whole scrollback at once.

    Selecting part of it is a long press in the terminal — this is for when the
    answer is "all of it", which is most of the time it is being sent to
    somebody.
    """

    name = "copy"
    summary = "put the whole scrollback on the clipboard"
    usage = "copy"

    def run(self, ctx, args):
        console = ctx.require("terminal")
        copy = getattr(console, "copy_transcript", None)
        if not callable(copy):
            raise CommandError("this console cannot reach the clipboard")
        lines = copy()
        return blocks.summary("copied %d line%s" % (lines, "" if lines == 1 else "s"),
                              role=blocks.SUCCESS)


class HistoryCommand(Command):
    """The console keeps a history and the up arrow walks it; this is how you
    look at it, and how you throw it away."""

    name = "history"
    summary = "commands run in this console"
    usage = "history [count]\nhistory clear"

    def run(self, ctx, args):
        history = getattr(ctx, "history", None)
        if history is None:
            raise CommandError(
                "history is not available here",
                hint="the console keeps it; scripts and chat commands do not",
            )
        if args and args[0] == "clear":
            del history[:]
            return blocks.summary("history cleared")

        flags = parse_flags(args, {})
        count = HISTORY_SHOWN
        if flags.positional:
            try:
                count = int(flags.positional[0])
            except ValueError:
                raise CommandError("history takes a number of lines",
                                   hint=self.usage)
        if not history:
            return blocks.summary("no history yet")
        shown = history[-count:] if count > 0 else list(history)
        first = len(history) - len(shown) + 1
        rows = [[str(first + i), line] for i, line in enumerate(shown)]
        return blocks.Result([
            blocks.Table(rows, aligns=["r", "l"]),
            blocks.Summary("%d of %d" % (len(shown), len(history))),
        ])


class ExtcliCommand(Command):
    """The program answering for itself.

    `host version` says the same thing and is where it belongs among the other
    facts about this device. This exists because `extcli --version` is what a
    person types, and a tool that does not answer its own name looks broken
    before it has done anything.
    """

    name = "extcli"
    summary = "what this is, and which version of it"
    usage = "extcli [--version]"

    def run(self, ctx, args):
        from ...compat import host

        parse_flags(args, {"--version": "bool", "-V": "bool"})
        version = host.plugin_version() or "unknown"
        return blocks.Result([
            blocks.Text("extCLI %s" % version),
            blocks.Summary("`help` lists the commands", role=blocks.DIM),
        ])


def build_all():
    return [ClearCommand(), ExitCommand(), EchoCommand(), HistoryCommand(),
            CopyCommand(), ExtcliCommand()]
