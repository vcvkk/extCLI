# SPDX-License-Identifier: Apache-2.0

"""`search` — find a chat among the ones the client has loaded.

Split out of `send` deliberately. Looking a chat up is the thing you do
*before* deciding to write to it, and a lookup that lives inside `send` reads
like it might send something. It also answers the other question `send` used
to: what a destination actually resolves to, which is worth knowing before
trusting a name to reach the right person.

Only loaded chats are searched — the client's own dialog list — so nothing here
touches the network.
"""

from ...render import blocks
from ..registry import Command, CommandError, parse_flags


class SearchCommand(Command):
    name = "search"
    summary = "find a chat, and see what a name resolves to"
    usage = "search <text>\nsearch --id <chat>"

    def run(self, ctx, args):
        if not args:
            raise CommandError("search needs something to look for",
                               hint=self.usage)
        flags = parse_flags(args, {"--id": "bool", "-i": "bool"})
        query = " ".join(flags.positional)
        if not query:
            raise CommandError("search needs something to look for",
                               hint=self.usage)
        messaging = ctx.require("messaging")

        if flags.has("--id") or flags.has("-i"):
            # the exact peer `send` would use for this name, or the same
            # complaint `send` would make about it
            try:
                peer = messaging.resolve(query)
            except LookupError as e:
                raise CommandError(str(e))
            return blocks.fields(peer.as_fields())

        found = messaging.search(query)
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


def build():
    return SearchCommand()
