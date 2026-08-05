# SPDX-License-Identifier: Apache-2.0

"""`help` — the command list, and per-command detail."""

from ...render import blocks
from ..registry import Command, CommandError, Group, suggest


class HelpCommand(Command):
    name = "help"
    summary = "list commands, or explain one"
    usage = "help [command [subcommand]]"

    def __init__(self, registry):
        self.registry = registry

    def run(self, ctx, args):
        if not args:
            return self._overview()
        command = self.registry.get(args[0])
        if command is None:
            raise CommandError(
                "no help for: %s" % args[0],
                hint=suggest(args[0], self.registry.names(include_aliases=True)),
            )
        if len(args) > 1 and isinstance(command, Group):
            sub = command.subcommands.get(args[1])
            if sub is None:
                raise CommandError(
                    "unknown subcommand: %s %s" % (args[0], args[1]),
                    hint=suggest(args[1], command.subcommands.keys(),
                                 "%s subcommands: " % args[0]),
                )
            return sub.help_result()
        if isinstance(command, Group):
            return self._group_help(command)
        return command.help_result()

    def _overview(self):
        rows = [(cmd.name, cmd.summary) for cmd in self.registry.commands()]
        return blocks.Result([
            blocks.Table(rows),
            blocks.Blank(),
            blocks.Text("help <command> for details", role=blocks.DIM),
        ])

    def _group_help(self, group):
        rows = [(name, sub.summary) for name, sub in sorted(group.subcommands.items())]
        return blocks.Result([
            blocks.Fields([("usage", group.usage), ("about", group.summary)],
                          title=group.name),
            blocks.Blank(),
            blocks.Table(rows),
        ])

    def complete(self, ctx, args):
        if len(args) <= 1:
            prefix = args[0] if args else ""
            return [n for n in self.registry.names(include_aliases=True)
                    if n.startswith(prefix)]
        command = self.registry.get(args[0])
        if isinstance(command, Group):
            prefix = args[1] if len(args) > 1 else ""
            return sorted(n for n in command.subcommands if n.startswith(prefix))
        return []
