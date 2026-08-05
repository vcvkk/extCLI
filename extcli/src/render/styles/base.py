# SPDX-License-Identifier: Apache-2.0

"""Shared pieces for output styles: color codes and column math."""

import re

from .. import blocks

# Every escape a program may send. One pattern, in one place, because the two
# that existed disagreed: this one dropped everything up to the next `m`, so a
# sequence that ends in any other letter — erase the line, move the cursor —
# was counted as visible text, and a line made of nothing else looked like a
# line with something on it.
ESCAPE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC: a title, a colour query
    r"|\x1b\[[0-9;?<>!]*[A-Za-z@`]"           # CSI, private forms included
    r"|\x1b[()][0-9A-Za-z]"                   # a character set being chosen
    r"|\x1b[=>78MZc]"                         # the short ones
    # A sequence cut off by the end of the text. Output arrives in pieces and a
    # piece can end in the middle of one; there is nothing visible in the part
    # that arrived, so it does not become text.
    r"|\x1b\[[0-9;?<>!]*$"
    r"|\x1b$")

RESET = "\x1b[0m"
BOLD = "\x1b[1m"

MIN_WIDTH = 20


def sgr(color):
    """Truecolor foreground escape. The renderer parses 38;2;r;g;b."""
    color = int(color)
    return "\x1b[38;2;%d;%d;%dm" % ((color >> 16) & 255, (color >> 8) & 255, color & 255)


def colored(text, color, bold=False):
    return "%s%s%s%s" % (BOLD if bold else "", sgr(color), text, RESET)


def strip_codes(text):
    """The text without escape sequences — what a clipboard should get."""
    return ESCAPE.sub("", str(text))


def is_blank(text):
    """Nothing to see. Not the same as an empty string: a program ending its
    output moves the cursor about, and those lines carry characters that draw
    nothing."""
    return not strip_codes(text).strip()


def visible_length(text):
    """Length without escape sequences — what the user actually sees."""
    return len(strip_codes(text))


def cut(text, width):
    """Splits after `width` visible characters, never inside an escape.

    Counting by characters would put the knife through the middle of a colour
    sequence, and the halves are worse than useless: one paints everything
    after it and the other prints as text.
    """
    if width <= 0:
        return "", text
    seen = 0
    index = 0
    while index < len(text) and seen < width:
        match = ESCAPE.match(text, index)
        if match:
            index = match.end()
            continue
        index += 1
        seen += 1
    return text[:index], text[index:]


def clip(text, width):
    """Truncates to width, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if visible_length(text) <= width:
        return text
    if width == 1:
        return "…"
    return cut(text, width - 1)[0] + "…"


def wrap(text, width, indent=0):
    """Word wrap that never returns a line wider than `width`.

    Measured by what is drawn rather than by what is in the string. Counting
    the characters counted the colour sequences too — a coloured line is a
    dozen invisible characters longer than it looks — so program output was
    broken up long before the edge of the screen, with the right-hand third
    left empty.
    """
    width = max(width, MIN_WIDTH)
    limit = max(width - indent, 8)
    pad = " " * indent
    out = []
    for paragraph in str(text).split("\n"):
        if not paragraph:
            out.append("")
            continue
        line = ""
        for word in paragraph.split(" "):
            if not line:
                line = word
            elif visible_length(line) + 1 + visible_length(word) <= limit:
                line += " " + word
            else:
                out.append(pad + line)
                line = word
            while visible_length(line) > limit:
                # a single word longer than the screen still has to fit
                head, line = cut(line, limit)
                out.append(pad + head)
        out.append(pad + line)
    return out


CONTROLS = re.compile(
    # `?` included: the private forms carry the alternate screen, which is how
    # a full-screen program says "the scrollback is not mine to write on"
    r"\x1b\[([0-9;?]*)([A-Za-z@`])"            # CSI, the ones with arguments
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC
    r"|\x1b[()][0-9A-Za-z]"
    r"|\x1b[=>78MZc]"
    r"|[\n\r\b\t]")

TAB = 8

# How many lines stay reachable by the cursor. A program can only move about
# the screen it was told it had, so this is a screenful and a little.
DEFAULT_HEIGHT = 60


def apply_controls(line, text):
    """Where characters land once the cursor has been moved about.

    A progress bar is one line written over and over. It says so by sending
    the cursor back to the start — with a carriage return, or with "go to
    column one", which is the same thing said the other way — and then writing
    the line again. Ignore that and an install comes out as sixty lines of
    percentages; treat it only as a space and they come out as one very long
    one.

    So the moves are carried out: back to a column truncates what is there,
    forward to one pads with spaces, and what follows is written from there.
    Truncating is what makes this a line and not a grid — a program that goes
    back to overwrite two characters of ten loses the other eight — and it is
    what every program that redraws a line does anyway, because it has no idea
    what was on the line either.

    Returns (the lines that ended, the one still being written).
    """
    finished = []
    column = visible_length(line)
    position = 0

    def seek(target):
        """Puts the cursor at a column, and the line with it."""
        nonlocal line, column
        target = max(0, target)
        if target < column:
            line = cut(line, target)[0]
        elif target > column:
            line += " " * (target - column)
        column = target

    while position < len(text):
        match = CONTROLS.search(text, position)
        if match is None:
            line += text[position:]
            column += visible_length(text[position:])
            break
        if match.start() > position:
            piece = text[position:match.start()]
            line += piece
            column += visible_length(piece)
        position = match.end()
        token = match.group(0)
        if token == "\n":
            finished.append(line)
            line = ""
            column = 0
            continue
        if token == "\r":
            seek(0)
            continue
        if token == "\b":
            seek(column - 1)
            continue
        if token == "\t":
            width = TAB - (column % TAB)
            line += " " * width
            column += width
            continue
        letter = match.group(2)
        if letter is None:
            continue          # an escape with nothing to do here
        try:
            count = int((match.group(1) or "").split(";")[0] or 0)
        except ValueError:
            count = 0
        if letter == "m":
            line += token     # a colour, which is not a movement
        elif letter == "G":
            seek(max(count, 1) - 1)
        elif letter == "C":
            seek(column + max(count, 1))
        elif letter == "D":
            seek(column - max(count, 1))
        elif letter == "K" and count in (0, None):
            # erase to the end of the line, which after a seek is already so
            line = cut(line, column)[0]
        # anything else draws nothing and moves nothing that can be followed
    return finished, line


class Screen(object):
    """The last lines of output, and where the cursor is among them.

    `apply_controls` above can only move the cursor along one line, which is
    all a plain progress bar needs — carriage return, rewrite, repeat. It is
    not what anything modern does: `uv` draws a spinner, a line per file and a
    total, then moves the cursor *up* and redraws the lot. A model that cannot
    go up has no choice but to add every redraw as new lines, which is how an
    install came out as four hundred lines of the same three.

    So the last `height` lines are kept reachable and the cursor has a row as
    well as a column. Lines that fall off the top can no longer be changed and
    are handed back, to be kept as scrollback.

    Height is bounded because a program can only move the cursor about the
    screen it was told it had; keeping more would be keeping lines nothing can
    ever reach again.
    """

    def __init__(self, height=DEFAULT_HEIGHT, rows=0):
        self.height = max(int(height), 2)
        # how tall the real screen is, which is not the same thing: `height` is
        # how far back the cursor may reach, and a page taller than the screen
        # is right for a program that scrolls. On the alternate screen they are
        # the same, because there the program is drawing a screen and nothing
        # else exists.
        self.rows = max(int(rows or 0), 0)
        self.lines = [""]
        self.row = 0
        self.column = 0
        self.alt = False
        self._saved = None
        self._stack = None

    def resize(self, height, rows=None):
        if rows is not None:
            self.rows = max(int(rows or 0), 0)
        if not self.alt:
            self.height = max(int(height), 2)
            return self._trim()
        # A screen that changes size keeps its top row. Trimming from the front
        # is scrolling, which is right when a program writes past the bottom
        # and wrong here: the keyboard coming up took the title bar off the top
        # of nano, and the redraw that follows a resize only rewrites what the
        # program believes has changed — so it never came back.
        self.height = max(self.rows or int(height), 2)
        if len(self.lines) > self.height:
            del self.lines[self.height:]
        while len(self.lines) < self.height:
            self.lines.append("")
        self.row = min(self.row, self.height - 1)
        return []

    def write(self, text):
        """Draws text and carries out the moves in it.

        Returns the lines that scrolled out of reach, oldest first.
        """
        gone = []
        position = 0
        text = str(text)
        while position < len(text):
            match = CONTROLS.search(text, position)
            if match is None:
                self._put(text[position:])
                break
            if match.start() > position:
                self._put(text[position:match.start()])
            position = match.end()
            gone.extend(self._control(match))
        return gone

    def finish(self):
        """Everything that is left, for when the output has ended."""
        if self.alt:
            # a program that died without handing its screen back hands it
            # back anyway: what it drew was never anybody's scrollback
            self._leave_alt()
        lines = [line for line in self.lines]
        self.lines = [""]
        self.row = 0
        self.column = 0
        return lines

    # ------------------------------------------------------------- internals

    def _put(self, piece):
        line = self.lines[self.row]
        length = visible_length(line)
        if self.column > length:
            line += " " * (self.column - length)
        head = cut(line, self.column)[0] if self.column < length else line
        tail = ""
        width = visible_length(piece)
        if self.column + width < length:
            # writing over the middle of a line leaves what is beyond it
            tail = cut(line, self.column + width)[1]
        self.lines[self.row] = head + piece + tail
        self.column += width

    def _control(self, match):
        token = match.group(0)
        if token == "\n":
            return self._newline()
        if token == "\r":
            self.column = 0
            return []
        if token == "\b":
            self.column = max(0, self.column - 1)
            return []
        if token == "\t":
            self.column += TAB - (self.column % TAB)
            return []
        if token == "\x1b7":
            self._saved = (self.row, self.column)
            return []
        if token == "\x1b8":
            if self._saved:
                self.row, self.column = self._saved
                self.row = min(self.row, len(self.lines) - 1)
            return []
        letter = match.group(2)
        if letter is None:
            return []
        argument = match.group(1) or ""
        if argument.startswith("?"):
            return self._private(argument[1:], letter)
        try:
            numbers = [int(part or 0) for part in argument.split(";")]
        except ValueError:
            numbers = [0]
        count = numbers[0]
        if letter == "m":
            # a colour is not a movement; it belongs in the line
            self._put(token)
        elif letter == "G":
            self.column = max(count, 1) - 1
        elif letter == "C":
            self.column += max(count, 1)
        elif letter == "D":
            self.column = max(0, self.column - max(count, 1))
        elif letter == "A":
            self.row = max(0, self.row - max(count, 1))
        elif letter == "B":
            return self._down(max(count, 1))
        elif letter in ("H", "f"):
            # the screen it thinks it has is the part of ours it can reach
            self.row = min(max(count, 1) - 1, self.height - 1)
            self.column = max(numbers[1] if len(numbers) > 1 else 1, 1) - 1
            while self.row >= len(self.lines):
                self.lines.append("")
        elif letter == "d":
            # the row on its own, which is how nano moves between its bars
            self.row = min(max(count, 1) - 1, self.height - 1)
            while self.row >= len(self.lines):
                self.lines.append("")
        elif letter == "K":
            self._erase_line(count)
        elif letter == "J":
            self._erase_below(count)
        elif letter == "L":
            self._insert_lines(max(count, 1))
        elif letter == "M":
            self._delete_lines(max(count, 1))
        elif letter == "P":
            self._delete_characters(max(count, 1))
        elif letter == "X":
            self._blank_characters(max(count, 1))
        return []

    def _private(self, argument, letter):
        """The `ESC[?...` forms. Only one of them changes anything here.

        1049 (and the two older spellings of it) is the alternate screen: an
        editor asks for a screen of its own, draws on it, and gives it back
        when it leaves. Everything the shell had printed is still underneath,
        untouched, which is exactly what makes `nano` leave no trace. The rest
        — the cursor being hidden, bracketed paste, wrap on or off — is either
        nothing to do with what the text says or is not ours to obey.
        """
        try:
            number = int(argument.split(";")[0] or 0)
        except ValueError:
            return []
        if number not in (47, 1047, 1049):
            return []
        if letter == "h":
            self._enter_alt()
        elif letter == "l":
            self._leave_alt()
        return []

    def _enter_alt(self):
        if self.alt:
            return
        self._stack = (self.lines, self.row, self.column, self.height)
        self.alt = True
        self.height = max(self.rows or self.height, 2)
        self.lines = [""]
        self.row = 0
        self.column = 0

    def _leave_alt(self):
        if not self.alt:
            return
        lines, row, column, height = self._stack
        self._stack = None
        self.alt = False
        self.lines, self.row, self.column, self.height = (
            lines, row, column, height)

    def _insert_lines(self, count):
        for _ in range(min(count, self.height)):
            self.lines.insert(self.row, "")
        del self.lines[self.height:]

    def _delete_lines(self, count):
        for _ in range(min(count, self.height)):
            if self.row < len(self.lines):
                self.lines.pop(self.row)
        while len(self.lines) <= self.row:
            self.lines.append("")

    def _delete_characters(self, count):
        line = self.lines[self.row]
        head = cut(line, self.column)[0]
        rest = cut(line, self.column + count)[1] if \
            visible_length(line) > self.column + count else ""
        self.lines[self.row] = head + rest

    def _blank_characters(self, count):
        line = self.lines[self.row]
        head = cut(line, self.column)[0]
        rest = cut(line, self.column + count)[1] if \
            visible_length(line) > self.column + count else ""
        self.lines[self.row] = head + " " * count + rest

    def _newline(self):
        self.column = 0
        return self._down(1)

    def _down(self, count):
        for _ in range(count):
            self.row += 1
            while self.row >= len(self.lines):
                self.lines.append("")
        return self._trim()

    def _trim(self):
        gone = []
        while len(self.lines) > self.height:
            line = self.lines.pop(0)
            self.row = max(0, self.row - 1)
            if not self.alt:
                gone.append(line)
        # what scrolls off the alternate screen is gone: a program with a
        # screen of its own has no scrollback, and keeping its frames would
        # bury the shell's output under a thousand redraws of an editor
        return gone

    def _erase_line(self, count):
        line = self.lines[self.row]
        if count == 1:
            rest = cut(line, self.column)[1]
            self.lines[self.row] = " " * self.column + rest
        elif count == 2:
            self.lines[self.row] = ""
        else:
            self.lines[self.row] = cut(line, self.column)[0]

    def _erase_below(self, count):
        if count == 2:
            self.lines = [""] * len(self.lines)
            return
        if count == 1:
            for index in range(self.row):
                self.lines[index] = ""
            self._erase_line(1)
            return
        self._erase_line(0)
        del self.lines[self.row + 1:]


def column_widths(rows, header=None, available=None, gap=2):
    """Natural column widths, shrunk proportionally when they do not fit."""
    all_rows = list(rows) + ([header] if header else [])
    if not all_rows:
        return []
    count = max(len(row) for row in all_rows)
    widths = [0] * count
    for row in all_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    if available is None:
        return widths

    total = sum(widths) + gap * (count - 1)
    if total <= available:
        return widths
    # shave the widest column until it fits; keeps short columns readable
    room = max(available - gap * (count - 1), count)
    while sum(widths) > room:
        widest = widths.index(max(widths))
        if widths[widest] <= 3:
            break
        widths[widest] -= 1
    return widths


def format_row(cells, widths, aligns=None, gap=2):
    parts = []
    for i, width in enumerate(widths):
        cell = str(cells[i]) if i < len(cells) else ""
        cell = clip(cell, width)
        align = (aligns[i] if aligns and i < len(aligns) else "l")
        parts.append(cell.rjust(width) if align == "r" else cell.ljust(width))
    return (" " * gap).join(parts).rstrip()


class Style(object):
    """Renders blocks into terminal text.

    Subclasses implement prompt(), echo() and the block_* methods they care
    about; unknown block kinds fall back to their plain text.
    """

    name = "base"

    def __init__(self, palette, width=40):
        self.palette = palette
        self.width = max(int(width), MIN_WIDTH)

    def color(self, role):
        return self.palette.role(role)

    def prompt(self, cwd="~", user="extcli", host="exteraGram"):
        raise NotImplementedError

    def echo(self, command, cwd="~"):
        raise NotImplementedError

    def render(self, result):
        """Result -> list of lines, escape codes included."""
        lines = []
        for block in result:
            lines.extend(self.render_block(block))
        return lines

    def render_block(self, block):
        handler = getattr(self, "block_" + block.kind, None)
        if handler is None:
            return self.block_unknown(block)
        return handler(block)

    def block_unknown(self, block):
        return [colored(str(block), self.color(blocks.DIM))]

    def block_blank(self, block):
        return [""]
