# SPDX-License-Identifier: Apache-2.0

"""`tg` — the client's own world, from the console.

Everything here needs Telegram to mean anything, which is why it is a group
rather than a handful of top-level commands. `send` and `search` used to sit
beside `cd` and `grep` as if they were the same kind of thing; they are not,
and a console that says `tg send` tells you where the boundary is without
having to be told.

The three are separate commands rather than one with flags because they are
separate acts. Looking a chat up is what you do *before* deciding to write to
it, and a lookup that lives inside `send` reads as if it might send something.

Only loaded chats are searched — the client's own dialog list — so nothing
here touches the network to find a name.
"""

from ...render import blocks
from ..registry import Command, CommandError, Group, parse_flags
from .send import SendCommand


class ChatsCommand(Command):
    name = "chats"
    summary = "find a chat among the ones the client has loaded"
    usage = "tg chats <text>"

    def run(self, ctx, args):
        flags = parse_flags(args, {})
        query = " ".join(flags.positional)
        if not query:
            raise CommandError("tg chats needs something to look for",
                               hint=self.usage)
        found = ctx.require("messaging").search(query)
        if not found:
            return blocks.summary("no loaded chat matches %r" % query)
        rows = []
        for peer in found:
            handle = "@%s" % peer.username if peer.username else str(peer.id)
            rows.append((peer.title, handle, None))
        return blocks.Result([
            blocks.Items(rows),
            blocks.Summary("%d chat%s" % (len(rows), "" if len(rows) == 1 else "s")),
        ])


class IdCommand(Command):
    """What a name actually resolves to.

    Worth knowing before trusting a name to reach the right person, and it
    answers with exactly the peer `tg send` would have used — or with exactly
    the complaint it would have made.
    """

    name = "id"
    summary = "what a name resolves to"
    usage = "tg id <chat>"

    def run(self, ctx, args):
        flags = parse_flags(args, {})
        query = " ".join(flags.positional)
        if not query:
            raise CommandError("tg id needs a chat", hint=self.usage)
        try:
            peer = ctx.require("messaging").resolve(query)
        except LookupError as e:
            raise CommandError(str(e))
        return blocks.fields(peer.as_fields())

    def complete(self, ctx, args):
        return SendCommand().complete(ctx, args)


def build():
    return Group("tg", "the client: chats, and writing to them", [
        SendCommand(),
        ChatsCommand(),
        IdCommand(),
    ])
