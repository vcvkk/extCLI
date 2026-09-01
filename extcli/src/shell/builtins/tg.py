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
from .send import SendCommand, _resolve


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


DEFAULT_COUNT = 20


def _when(stamp):
    import datetime

    return datetime.datetime.fromtimestamp(int(stamp or 0))


def _one_line(message):
    """A message on one line, for grep.

    The text is flattened rather than truncated: a line that stops in the
    middle is a line that will not match what somebody is looking for, and
    finding it is the whole reason this form exists.
    """
    text = " ".join(str(message.get("text") or "").split())
    if message.get("media"):
        text = ("[%s] %s" % (message["media"], text)).strip()
    return "%s  %s  %s: %s" % (
        message.get("id", 0),
        _when(message.get("date")).strftime("%Y-%m-%d %H:%M"),
        message.get("author") or ("you" if message.get("out") else "?"),
        text,
    )


def _as_json(message):
    import json

    return json.dumps(message, ensure_ascii=False, sort_keys=True)


def _conversation(messages):
    """The reading form: a date when the day turns, then who and when.

    Grouped by day because a timestamp on every line is thirty characters of
    the same thing on a screen forty wide, and the day is the part that
    actually changes.
    """
    result = blocks.Result()
    day = None
    for message in messages:
        when = _when(message.get("date"))
        if when.date() != day:
            if day is not None:
                result.add(blocks.Blank())
            result.add(blocks.Text(when.strftime("%Y-%m-%d"), role=blocks.ACCENT))
            day = when.date()
        who = message.get("author") or ("you" if message.get("out") else "?")
        head = "%s  %s" % (when.strftime("%H:%M"), who)
        if message.get("media"):
            head += "  [%s]" % message["media"]
        result.add(blocks.Text(head, role=blocks.DIM))
        text = str(message.get("text") or "")
        if text:
            result.add(blocks.Text(text.split("\n")))
    return result


class ReadCommand(Command):
    """`tg read <chat>` — a conversation as text.

    The reason this plugin is worth having over a terminal that is not inside
    a messenger. Once the chat is on stdout it is a stream like any other:
    `tg read @chat -n 200 | grep -i release | tail -5` is a question nobody
    can ask their phone otherwise.

    Oldest first, so `| tail` is the end of the conversation the way it is the
    end of a file.
    """

    name = "read"
    summary = "print a chat as text"
    usage = ("tg read <chat> [-n <count>]\n"
             "tg read <chat> --oneline\n"
             "tg read <chat> --json")

    def run(self, ctx, args):
        flags = parse_flags(args, {"-n": "int", "--count": "int",
                                   "--oneline": "bool", "--json": "bool",
                                   "--before": "int"})
        if not flags.positional:
            raise CommandError("tg read needs a chat", hint=self.usage)
        count = flags.get("-n", flags.get("--count", DEFAULT_COUNT))
        if count < 1:
            raise CommandError("-n needs a positive count")
        if flags.has("--oneline") and flags.has("--json"):
            raise CommandError("tg read takes either --oneline or --json")

        peer = _resolve(ctx, " ".join(flags.positional))
        messages = ctx.require("messaging").history(
            peer.id, limit=count, before=flags.get("--before", 0))
        if not messages:
            return blocks.summary("nothing to read in %s" % peer.label())

        if flags.has("--json"):
            return blocks.text([_as_json(m) for m in messages])
        if flags.has("--oneline"):
            return blocks.text([_one_line(m) for m in messages])
        result = _conversation(messages)
        result.add(blocks.Blank())
        result.add(blocks.Summary("%d message%s from %s"
                                  % (len(messages),
                                     "" if len(messages) == 1 else "s",
                                     peer.label())))
        return result

    def complete(self, ctx, args):
        return SendCommand().complete(ctx, args)


class GetCommand(Command):
    """`tg get <chat>` — the attachments, as files.

    The other half of `tg read`. Once a chat's pictures and documents are in a
    directory they are ordinary files, and everything in the container can
    have them: `tg get @chat -n 50 --out shots && mogrify -resize 50% shots/*`.

    A record per attachment rather than one verdict for the batch: on a phone
    some of them arriving and some not is the ordinary outcome.
    """

    name = "get"
    summary = "save a chat's attachments as files"
    usage = "tg get <chat> [-n <count>] [--out <dir>]"
    mutating = True

    def run(self, ctx, args):
        flags = parse_flags(args, {"-n": "int", "--count": "int",
                                   "--out": "str", "-o": "str",
                                   "--before": "int"})
        if not flags.positional:
            raise CommandError("tg get needs a chat", hint=self.usage)
        count = flags.get("-n", flags.get("--count", DEFAULT_COUNT))
        if count < 1:
            raise CommandError("-n needs a positive count")
        peer = _resolve(ctx, " ".join(flags.positional))

        wanted = flags.get("--out", flags.get("-o", "."))
        env = getattr(ctx, "env", None)
        target = env.host(wanted) if env is not None else wanted

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.FS_WRITE, "save attachments into %s" % wanted,
                           assume_yes=ctx.assume_yes)

        saved = ctx.require("messaging").download(
            peer.id, limit=count, before=flags.get("--before", 0),
            target=target)
        if not saved:
            return blocks.summary("nothing attached in the last %d messages"
                                  % count)
        rows = []
        for record in saved:
            rows.append((record.get("name") or "?",
                         _size(record.get("size") or 0) if record.get("ok")
                         else (record.get("detail") or "failed"),
                         "on" if record.get("ok") else "off"))
        done = [r for r in saved if r.get("ok")]
        total = sum(r.get("size") or 0 for r in done)
        return blocks.Result([
            blocks.Items(rows),
            blocks.Summary("%d of %d saved into %s%s"
                           % (len(done), len(saved), wanted,
                              ", %s" % _size(total) if total else "")),
        ])

    def complete(self, ctx, args):
        return SendCommand().complete(ctx, args)


def _size(count):
    """Bytes as somebody would say them out loud."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%d %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024


def build():
    return Group("tg", "the client: chats, and writing to them", [
        SendCommand(),
        ReadCommand(),
        GetCommand(),
        ChatsCommand(),
        IdCommand(),
    ])
