# SPDX-License-Identifier: Apache-2.0

"""What a command returns.

A command produces blocks, not text. Each block says what kind of information
it carries — a table, a key/value listing, an error — and the active style
decides how that looks. Nothing here knows about colors, escape codes or the
width of the screen.
"""

# semantic roles a style maps to actual colors
FG = "fg"
DIM = "dim"
ACCENT = "accent"
ERROR = "error"
SUCCESS = "success"
WARN = "warn"


class Block(object):
    """Base class; a style dispatches on `kind`."""

    kind = "block"


class Text(Block):
    """One or more plain lines, optionally in a semantic color."""

    kind = "text"

    def __init__(self, lines, role=FG):
        if isinstance(lines, str):
            lines = [lines]
        self.lines = list(lines)
        self.role = role


class Summary(Block):
    """The one-line answer to "what happened", shown right under the command."""

    kind = "summary"

    def __init__(self, text, role=DIM):
        self.text = text
        self.role = role


class Error(Block):
    kind = "error"

    def __init__(self, message, hint=None):
        self.message = message
        self.hint = hint


class Fields(Block):
    """Aligned label/value pairs: `host status`, `plugin info`."""

    kind = "fields"

    def __init__(self, rows, title=None):
        # rows: [(label, value)] or [(label, value, role)]
        self.rows = [self._normalize(row) for row in rows]
        self.title = title

    @staticmethod
    def _normalize(row):
        if len(row) == 2:
            return (str(row[0]), str(row[1]), FG)
        return (str(row[0]), str(row[1]), row[2])


class Table(Block):
    """Column-aligned rows with an optional header."""

    kind = "table"

    def __init__(self, rows, header=None, aligns=None):
        self.rows = [[str(cell) for cell in row] for row in rows]
        self.header = [str(cell) for cell in header] if header else None
        # 'l' or 'r' per column; missing entries default to left
        self.aligns = list(aligns) if aligns else None


class Items(Block):
    """A list where each entry has a state marker, a name and details.

    Used by `plugin list`: the marker carries enabled/disabled, so a style can
    render it as [on]/[off], a filled circle, or anything else.
    """

    kind = "items"

    def __init__(self, entries):
        # entries: [(name, detail, state)] with state in {"on","off","warn",None}
        self.entries = [
            (str(name), "" if detail is None else str(detail), state)
            for name, detail, state in entries
        ]


class Blank(Block):
    kind = "blank"


class Result(object):
    """What the dispatcher hands to the console: blocks plus an exit code.

    Commands that only need one line can use the helpers below instead of
    building this by hand.
    """

    def __init__(self, blocks=None, code=0):
        self.blocks = list(blocks or [])
        self.code = int(code)

    @property
    def ok(self):
        return self.code == 0

    def add(self, block):
        self.blocks.append(block)
        return self

    def __iter__(self):
        return iter(self.blocks)


def text(lines, role=FG, code=0):
    return Result([Text(lines, role)], code)


def summary(message, role=DIM, code=0):
    return Result([Summary(message, role)], code)


def error(message, hint=None, code=1):
    return Result([Error(message, hint)], code)


def fields(rows, title=None, code=0):
    return Result([Fields(rows, title)], code)


def table(rows, header=None, aligns=None, code=0):
    return Result([Table(rows, header, aligns)], code)


def items(entries, code=0):
    return Result([Items(entries)], code)
