# SPDX-License-Identifier: Apache-2.0

"""`tg send` — put something in a chat from the console.

One command, one job: `tg send <chat> <text>`, or `tg send <chat> --photo
<path>` with the rest of the line as the caption. Looking chats up is
`tg chats`, which is a separate command because finding a chat and writing to
one are separate things — and because a lookup that lives inside a send reads
as if it might send something.

Destinations are resolved against chats the client has already loaded, so a
typo cannot silently reach a stranger: an ambiguous name is an error listing
what it matched, not a guess.

Sending to anyone other than yourself goes through policy.SEND_TO_OTHERS. In
the development build that only records the action; it is the hook the release
build turns into a confirmation.
"""

import os

from ...render import blocks
from ..registry import Command, CommandError, parse_flags

FLAGS = {
    "--photo": "str",
    "--file": "str",
    "--document": "str",
    "--caption": "str",
    "--markdown": "bool",
    "-m": "bool",
}


def _messaging(ctx):
    return ctx.require("messaging")


def _resolve(ctx, query):
    try:
        return _messaging(ctx).resolve(query)
    except LookupError as e:
        raise CommandError(str(e), hint="try `tg chats %s` to find the chat" % query)


def _check_policy(ctx, peer, detail):
    policy = ctx.services.policy
    if policy is None:
        return
    action = policy.SEND_MESSAGE if peer.is_self else policy.SEND_TO_OTHERS
    policy.require(action, "%s -> %s" % (detail, peer.label()),
                   assume_yes=ctx.assume_yes)


class SendCommand(Command):
    name = "send"
    summary = "send a message, photo or file to a chat"
    usage = ("tg send <chat> <text>\n"
             "tg send <chat> --photo <path> [caption]\n"
             "tg send <chat> --file <path> [caption]")
    mutating = True

    def run(self, ctx, args):
        if not args:
            raise CommandError("tg send needs a destination", hint=self.usage)
        flags = parse_flags(args, FLAGS)
        if not flags.positional:
            raise CommandError("tg send needs a destination", hint=self.usage)

        peer = _resolve(ctx, flags.positional[0])
        rest = " ".join(flags.positional[1:])
        photo = flags.get("--photo")
        document = flags.get("--file") or flags.get("--document")
        if photo and document:
            raise CommandError("tg send takes either --photo or --file, not both")
        if photo or document:
            # anything left on the line is the caption, so both of these work:
            #   send me --photo shot.png look at this
            #   send me --photo shot.png --caption "look at this"
            caption = flags.get("--caption") or rest or None
            return self._send_file(ctx, peer, photo or document,
                                   caption, as_photo=bool(photo))
        if not rest:
            raise CommandError("tg send needs a message",
                               hint="tg send <chat> <text>, or --photo/--file")
        return self._send_text(ctx, peer, rest, flags)

    def _send_text(self, ctx, peer, text, flags):
        _check_policy(ctx, peer, "message")
        markdown = flags.has("--markdown") or flags.has("-m")
        ok, detail = _messaging(ctx).send_text(peer.id, text,
                                               "markdown" if markdown else None)
        if not ok:
            raise CommandError("tg send failed: %s" % detail)
        return blocks.summary("sent to %s" % peer.label(), role=blocks.SUCCESS)

    def _send_file(self, ctx, peer, raw, caption, as_photo):
        env = getattr(ctx, "env", None)
        path = env.host(raw) if env is not None else raw
        if not os.path.isfile(path):
            raise CommandError("no such file: %s" % raw)

        _check_policy(ctx, peer, os.path.basename(path))
        messaging = _messaging(ctx)
        if as_photo:
            ok, detail = messaging.send_photo(peer.id, path, caption)
        else:
            ok, detail = messaging.send_document(peer.id, path, caption)
        if not ok:
            raise CommandError("tg send failed: %s" % detail)
        return blocks.summary(
            "sent %s (%d bytes) to %s" % (os.path.basename(path),
                                          os.path.getsize(path), peer.label()),
            role=blocks.SUCCESS,
        )

    def complete(self, ctx, args):
        """The first word is a chat, so offer the ones the client has loaded."""
        if len(args) > 1 or not ctx.has("messaging"):
            return []
        prefix = args[0] if args else ""
        names = []
        try:
            for peer in ctx.services.messaging.dialogs(30):
                names.append(peer.username or peer.title)
        except Exception:
            return []
        return [name for name in names if name.startswith(prefix)]

