# SPDX-License-Identifier: Apache-2.0

"""`log` — extCLI's own log buffer.

An unrooted app cannot read logcat back, so the plugin keeps its recent lines
in memory (utils/log.py) and this command reads that.
"""

from ...render import blocks
from ..registry import Command, CommandError, Group, parse_flags

_LEVEL_ROLE = {"E": blocks.ERROR, "D": blocks.DIM, "I": blocks.FG}


class TailCommand(Command):
    name = "tail"
    summary = "recent log lines"
    usage = "log tail [-n <count>] [--errors]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"-n": "int", "--errors": "bool", "-e": "bool"})
        log = ctx.require("log")
        count = flags.get("-n", 40)
        if count <= 0:
            raise CommandError("-n needs a positive count")
        level = "E" if (flags.has("--errors") or flags.has("-e")) else None

        rows = log.tail(count, level)
        if not rows:
            return blocks.summary("log is empty")

        result = blocks.Result()
        for stamp, level_mark, text in _formatted(rows):
            result.add(blocks.Text(["%s %s" % (stamp, text)],
                                   role=_LEVEL_ROLE.get(level_mark, blocks.FG)))
        result.add(blocks.Summary("%d lines" % len(rows)))
        return result


class GrepCommand(Command):
    name = "grep"
    summary = "search the log"
    usage = "log grep <text> [-n <count>]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"-n": "int"})
        if not flags.positional:
            raise CommandError("log grep needs something to look for", hint=self.usage)
        needle = " ".join(flags.positional).lower()
        log = ctx.require("log")
        rows = [row for row in log.tail(2000) if needle in row[2].lower()]
        rows = rows[-flags.get("-n", 40):]
        if not rows:
            return blocks.summary("no log lines match %r" % needle)
        result = blocks.Result()
        for stamp, level_mark, text in _formatted(rows):
            result.add(blocks.Text(["%s %s" % (stamp, text)],
                                   role=_LEVEL_ROLE.get(level_mark, blocks.FG)))
        result.add(blocks.Summary("%d matching lines" % len(rows)))
        return result


class ClearCommand(Command):
    name = "clear"
    summary = "empty the log buffer"
    usage = "log clear"
    mutating = True

    def run(self, ctx, args):
        ctx.require("log").clear()
        return blocks.summary("log cleared", role=blocks.SUCCESS)


def _formatted(rows):
    import time

    out = []
    for timestamp, level, text in rows:
        stamp = time.strftime("%H:%M:%S", time.localtime(timestamp))
        out.append((stamp, level, text))
    return out


def build():
    return Group("log", "extCLI's log buffer", [
        TailCommand(), GrepCommand(), ClearCommand(),
    ])
