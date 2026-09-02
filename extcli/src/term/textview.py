# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""A terminal built from ordinary Android views.

The dex renderer is faster and is what the TUI mode will need, but it had not
been seen working on a device yet, and a console that might be blank is worse
than a slower one that is not. This implementation uses a ScrollView and a
TextView — nothing to load, nothing to reflect — and exposes the same interface
as term/bridge.Terminal, so the console does not care which one it got.

ANSI colors are parsed into spans, so output looks the same either way.
"""

import re

from ..compat import fonts
from ..render.styles import base as style_base
from ..utils import log

SCROLLBACK_LINES = 1500

# how long output may wait to be drawn, in milliseconds. Twenty-five frames a
# second is more than a terminal needs and a fraction of what a program writing
# a progress bar asks for.
RENDER_INTERVAL = 40

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
# Everything a program may say that this cannot obey, so it is dropped rather
# than shown: `ESC[?7l` — turn line wrapping off, which fastfetch sends before
# its logo — came out as the text `[?7l`. Shared with the styles, so that what
# counts as invisible is one answer rather than two that drift apart.
_ANY_ESCAPE = style_base.ESCAPE


def parse_ansi(text, default_color, palette=None):
    """Splits text into (segment, foreground, background) runs.

    The background is None for almost everything, and is the whole point for
    the rest: a full-screen program draws its title and its shortcut bars by
    turning the colours round — `ESC[7m` — and without a background those bars
    came out as ordinary text with the rest of the screen showing through. It
    is what made nano look broken rather than merely plain.

    Pure, so it is unit-tested.
    """
    segments = []
    colors = (default_color, None)
    position = 0
    for match in _SGR.finditer(text):
        if match.start() > position:
            segments.append((text[position:match.start()],) + colors)
        colors = _apply_sgr(match.group(1), colors, default_color, palette)
        position = match.end()
    if position < len(text):
        segments.append((text[position:],) + colors)
    # any other escape sequence is dropped rather than shown as garbage
    return [(_ANY_ESCAPE.sub("", chunk), fg, bg)
            for chunk, fg, bg in segments if chunk]


# What the cell under the cursor is made of when there is nothing in it.
#
# Not a space. The cursor is a cell with its colours swapped, and Android does
# not paint a background over trailing whitespace at the end of a line — so a
# cursor sitting past the last character was invisible however carefully the
# cell was put there. A no-break space is a character the layout keeps, and it
# looks like exactly what it is: nothing.
CURSOR_CELL = "\u00a0"


def blank_cell(color):
    """A line with a cell on it and nothing in the cell.

    While a command runs there is no prompt to draw, and an empty input line
    meant no line at all — so the cursor vanished instead of sitting under the
    output where the eye expects it.

    A run, built the way `parse_ansi` builds them, and here rather than inline
    for exactly that reason: it was written by hand with two parts when a run
    grew a third, and every redraw after a command started raised instead of
    drawing. The console froze on the first command with the line already
    echoed and never ran it.
    """
    return [(CURSOR_CELL, color, None)]


def _apply_sgr(params, current, default_color, palette=None):
    """The (foreground, background) a run of text ends up in.

    This used to understand a reset and a 24-bit colour and nothing else, which
    is why `fastfetch` came out in one colour — every ordinary `ESC[31m` fell
    through unrecognised. The numbers a program actually uses are the plain
    thirty-somethings, and — for anything that draws a screen — 7, which turns
    the two round.
    """
    color, background = current
    if not params:
        return (default_color, None)
    parts = params.split(";")
    index = 0
    while index < len(parts):
        try:
            code = int(parts[index] or 0)
        except ValueError:
            code = 0
        if code == 0:
            color, background = default_color, None
        elif code == 39:
            color = default_color
        elif code == 49:
            background = None
        elif code == 7:
            # not a colour of its own: whatever the two are, the other way up
            color, background = (background if background is not None
                                 else _reverse_fg(palette, default_color),
                                 color)
        elif code == 27:
            color, background = (background if background is not None
                                 else default_color,
                                 None)
        elif 30 <= code <= 37:
            color = _from_palette(palette, code - 30, color)
        elif 90 <= code <= 97:
            color = _from_palette(palette, code - 90 + 8, color)
        elif 40 <= code <= 47:
            background = _from_palette(palette, code - 40, background)
        elif 100 <= code <= 107:
            background = _from_palette(palette, code - 100 + 8, background)
        elif code in (38, 48) and index + 4 < len(parts) and \
                parts[index + 1] == "2":
            try:
                chosen = _signed((0xFF << 24) | (int(parts[index + 2]) << 16)
                                 | (int(parts[index + 3]) << 8)
                                 | int(parts[index + 4]))
                if code == 38:
                    color = chosen
                else:
                    background = chosen
            except ValueError:
                pass
            index += 4
        elif code in (38, 48) and index + 2 < len(parts) and \
                parts[index + 1] == "5":
            try:
                chosen = _from_palette(palette, int(parts[index + 2]),
                                       color if code == 38 else background)
                if code == 38:
                    color = chosen
                else:
                    background = chosen
            except ValueError:
                pass
            index += 2
        index += 1
    return (color, background)


def _reverse_fg(palette, default_color):
    """What the text becomes when the colours are turned round and there is no
    background to turn round with: the background the terminal is painted in,
    so the bar reads as a bar rather than as a hole."""
    if palette is None:
        return default_color
    try:
        return palette.role("bg")
    except Exception:
        return default_color


def _from_palette(palette, index, fallback):
    if palette is None:
        return fallback
    try:
        return palette.ansi_color(index)
    except Exception:
        return fallback


def _signed(color):
    import ctypes

    return ctypes.c_int32(int(color)).value


def _runnable(function):
    """A Runnable, from the one class there is.

    Not a class defined here: this is posted on every frame of output, and a
    proxy class per call is what made the redraw timer die inside
    `Handler.handleCallback` with an AttributeError about `_chaquopyGetDict`.
    """
    from ..compat import proxies

    return proxies.runnable(function)


class TextViewTerminal(object):
    """Same surface as bridge.Terminal, built from stock widgets."""

    kind = "views"

    # the console draws the line being typed inside the terminal, Termux-style,
    # instead of showing a text field; renderers that cannot do that say so
    echoes_input = True

    def __init__(self, context, palette, text_size_sp=12.0,
                 scrollback=SCROLLBACK_LINES):
        from android.util import TypedValue
        from android.widget import ScrollView, TextView

        self.palette = palette
        self.scrollback = scrollback
        self._lines = []          # [[(text, color)]] — one list per line
        # the lines the cursor can still reach; what falls out of them is
        # scrollback and can never change again
        self._screen = style_base.Screen()
        self._input = []          # the prompt + what is being typed right now
        self._cursor = None       # column of the block cursor on that line
        # a redraw rebuilds every line in the scrollback, and output arrives in
        # chunks far faster than a screen changes; see _later_render
        self._render_soon = False
        self._follow_after_render = False
        # the text as it stands, and how much of it is already right
        self._buffer = None
        self._tail_start = 0
        self._drawn_lines = 0
        self._buffer_stale = True

        self._text_view = TextView(context)
        self._text_view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, float(text_size_sp))
        self._text_view.setTextColor(palette.role("fg"))
        # Selectable, so a long press selects text the way it does everywhere
        # else on the phone — handles, drag, the copy toolbar. That makes the
        # view focusable, and it will take focus from the console's input; the
        # console handles that by giving focus straight back unless a selection
        # is under way. See ConsoleSession.on_terminal_focus.
        self._text_view.setTextIsSelectable(True)
        self._text_view.setPadding(20, 16, 20, 16)
        typeface = fonts.mono_typeface()
        if typeface is not None:
            self._text_view.setTypeface(typeface)

        self._scroll = ScrollView(context)
        self._scroll.setBackgroundColor(palette.role("bg"))
        self._scroll.setFillViewport(True)
        self._scroll.addView(self._text_view)

        self._char_width = self._measure_char()

    def _measure_char(self):
        try:
            paint = self._text_view.getPaint()
            width = float(paint.measureText("M"))
            return width if width > 0 else 0.0
        except Exception:
            return 0.0

    # -------------------------------------------------------------- interface

    @property
    def view(self):
        return self._scroll

    def append(self, text):
        """Adds output, carrying out the cursor moves in it.

        The line being written is kept as text rather than as coloured runs,
        because that is what the moves act on: going back to a column means
        cutting the line there, and a colour set before the cut still applies
        after it. It is turned into runs when it is drawn.
        """
        default = self.palette.role("fg")
        following = self.at_bottom()
        self._fit_screen(default)
        for line in self._screen.write(str(text)):
            self._lines.append(parse_ansi(line, default, self.palette))
        if len(self._lines) > self.scrollback:
            del self._lines[:-self.scrollback]
            # the buffer holds lines that are no longer there
            self._buffer_stale = True
        self._later_render(following)

    def _fit_screen(self, default):
        """How far up the cursor may go: as far as the screen the program was
        told it had. Keeping more would keep lines nothing can reach.

        The real number of rows goes with it, because a program on the
        alternate screen is drawing exactly that many and no more."""
        rows = int(self.metrics()[1])
        wanted = max(rows + 4, 24)
        if wanted == self._screen.height and rows == self._screen.rows:
            return
        for line in self._screen.resize(wanted, rows=rows):
            self._lines.append(parse_ansi(line, default, self.palette))

    def _later_render(self, follow):
        """Draws soon rather than now.

        A redraw builds the whole scrollback into one spanned string and hands
        it to the TextView; that is fine per keystroke and ruinous per chunk of
        output. `apk` writes its progress bar dozens of times a second, and
        every one of those was a full rebuild — which is what made scrolling
        during an install feel like wading.

        So chunks are collected and drawn on a timer instead. Nothing is lost:
        the lines are already in place, only the drawing waits.
        """
        self._follow_after_render = self._follow_after_render or follow
        if self._render_soon:
            return
        self._render_soon = True

        def draw():
            self._render_soon = False
            follow_now, self._follow_after_render = (
                self._follow_after_render, False)
            try:
                self._render()
                if follow_now:
                    self.scroll_to_bottom()
            except Exception as e:
                log.error("term: cannot draw output", e)

        try:
            self._text_view.postDelayed(_runnable(draw), RENDER_INTERVAL)
        except Exception:
            self._render_soon = False
            self._render()
            if follow:
                self.scroll_to_bottom()

    def _draw_now(self):
        """Everything that was waiting, at once. For the end of a command,
        where the next thing on screen is a prompt."""
        if not self._render_soon:
            return
        self._render_soon = False
        follow, self._follow_after_render = self._follow_after_render, False
        self._render()
        if follow:
            self.scroll_to_bottom()

    def write_line(self, text=""):
        self.append("%s\n" % text)

    def set_input_line(self, text, cursor=None):
        """Replaces the transient last line — the prompt and the typed text.

        It is not part of the scrollback: it is redrawn on every keystroke and
        vanishes when the command is echoed, which is what makes the caret look
        like it lives in the terminal.

        `cursor` is a column, not a character. The block is painted over the
        cell by swapping the colours there — putting a █ in the text instead
        would mean copying a line gave you the cursor along with it.
        """
        text = str(text or "")
        if text:
            self._input = parse_ansi(text, self.palette.role("fg"),
                                     self.palette)
        elif cursor is not None:
            self._input = blank_cell(self.palette.role("fg"))
        else:
            self._input = []
        self._cursor = None if cursor is None else max(0, int(cursor))
        self._render()
        # only if the reader is there: this is redrawn on every keystroke and
        # on every line of output, and it was what pulled a reader who had
        # scrolled up back down again
        self.scroll_to_bottom(only_if_following=True)

    def write_lines(self, lines):
        self.append("".join("%s\n" % line for line in lines))

    def blit(self, chars, fg, bg, cols, rows):
        """Grid mode: draws a frame as text. Colors follow the first cell of
        each run, which is all a TextView can express cheaply."""
        out = []
        for row in range(rows):
            base = row * cols
            out.append("".join(chr(chars[base + column]) for column in range(cols)))
        self.clear()
        self.append("\n".join(out))

    def trim_trailing_blanks(self):
        """Drops empty lines from the end.

        A program that finishes by moving its cursor down past a logo leaves a
        handful, and they would push the next prompt down the screen for
        nothing. Done here rather than on the way in, because on the way in
        there is no telling which blank line is the last one.
        """
        self._draw_now()
        removed = 0
        while self._screen.lines and style_base.is_blank(
                self._screen.lines[-1]):
            self._screen.lines.pop()
            self._screen.row = min(self._screen.row,
                                   max(len(self._screen.lines) - 1, 0))
            removed += 1
        if not self._screen.lines:
            self._screen.lines = [""]
            self._screen.row = 0
            self._screen.column = 0
        while self._lines and not any(self._screen.lines):
            drawn = "".join(run[0] for run in self._lines[-1])
            if not style_base.is_blank(drawn):
                break
            self._lines.pop()
            removed += 1
        if removed:
            self._render()
        return removed

    def clear(self):
        self._lines = []
        self._screen = style_base.Screen()
        self._buffer_stale = True
        self._render()

    def set_palette(self, palette):
        self.palette = palette
        self._buffer_stale = True
        try:
            self._scroll.setBackgroundColor(palette.role("bg"))
            self._text_view.setTextColor(palette.role("fg"))
        except Exception as e:
            log.error("term: cannot apply the palette", e)

    def set_text_size(self, text_size_sp):
        from android.util import TypedValue

        self._text_view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, float(text_size_sp))
        self._char_width = self._measure_char()

    def metrics(self):
        """(cols, rows, cell_width, cell_height) — the size of the screen.

        Rows used to be how many lines had been written, which is a fact about
        the scrollback and not about the screen: `stty size` answered with it,
        and a program that draws a page believed it.

        The width leaves out the padding. Text is not drawn in it, so counting
        it promises a program room that is not there.
        """
        try:
            width = int(self._text_view.getWidth() or self._scroll.getWidth() or 0)
            width -= int(self._text_view.getPaddingLeft() or 0)
            width -= int(self._text_view.getPaddingRight() or 0)
        except Exception:
            width = 0
        cell = self._char_width or 0.0
        cols = int(width / cell) if cell and width > 0 else 0
        try:
            line_height = int(self._text_view.getLineHeight() or 0)
        except Exception:
            line_height = 0
        try:
            height = int(self._scroll.getHeight() or 0)
            height -= int(self._text_view.getPaddingTop() or 0)
            height -= int(self._text_view.getPaddingBottom() or 0)
        except Exception:
            height = 0
        rows = int(height / line_height) if line_height and height > 0 else 0
        return (cols, rows, int(cell), line_height)

    def describe(self):
        cols, rows, cell, line_height = self.metrics()
        return ("mode=views cols=%d rows=%d cell=%dx%d lines=%d"
                % (cols, rows, cell, line_height, len(self._lines)))

    def set_on_tap(self, listener):
        """Wires a tap on the terminal. Both views need it: a tap can land on
        the text or on the empty space under it."""
        for view in (self._scroll, self._text_view):
            try:
                view.setClickable(True)
                view.setOnClickListener(listener)
            except Exception as e:
                log.log("term: cannot attach the tap listener: %s" % e, debug=True)

    def has_selection(self):
        """True while some of the scrollback is highlighted."""
        try:
            return int(self._text_view.getSelectionStart()) != \
                int(self._text_view.getSelectionEnd())
        except Exception:
            return False

    def selected_text(self):
        if not self.has_selection():
            return ""
        try:
            start = int(self._text_view.getSelectionStart())
            end = int(self._text_view.getSelectionEnd())
            return str(self._text_view.getText())[min(start, end):max(start, end)]
        except Exception:
            return ""

    def clear_selection(self):
        from android.text import Selection

        try:
            Selection.removeSelection(self._text_view.getText())
        except Exception:
            pass

    def set_size_watcher(self, callback):
        """Reports when the terminal changes size, in columns and rows.

        The keyboard coming up and going down is the common one, and a program
        drawing a screen has to be told: it measured itself once, when it
        started, and nothing since has said otherwise.
        """
        from ..compat import proxies

        last = {"size": None}
        terminal = self

        def moved(view, left, top, right, bottom,
                  old_left, old_top, old_right, old_bottom):
            cols, rows = terminal.metrics()[:2]
            if not cols or not rows or (cols, rows) == last["size"]:
                return
            last["size"] = (cols, rows)
            callback(int(cols), int(rows))

        try:
            listener = proxies.layout_listener(moved)
            self._scroll.addOnLayoutChangeListener(listener)
            self._text_view.addOnLayoutChangeListener(listener)
        except Exception as e:
            log.error("term: cannot watch the size", e)

    def set_focus_watcher(self, callback):
        """Reports when the terminal takes focus.

        It will: a selectable TextView is focusable in touch mode. The console
        uses this to hand focus back to its input, so typing survives a tap and
        every redraw.
        """
        from ..compat import proxies

        try:
            self._text_view.setOnFocusChangeListener(
                proxies.focus_listener(callback))
        except Exception as e:
            log.error("term: cannot watch focus", e)

    def set_selection_watcher(self, on_start, on_end):
        """Brackets the selection toolbar's life.

        Between these two the console leaves the terminal alone: it does not
        take focus back and does not redraw the input line, so the highlight
        survives whatever is being typed or printed.
        """
        from ..compat import proxies

        text_view = self._text_view

        def ended():
            # posted: this runs mid-teardown, and the callback moves focus
            text_view.post(proxies.runnable(on_end))

        try:
            text_view.setCustomSelectionActionModeCallback(
                proxies.selection_callback(on_start, ended))
        except Exception as e:
            log.error("term: cannot watch the selection toolbar", e)

    def set_on_long_press(self, listener):
        for view in (self._scroll, self._text_view):
            try:
                view.setLongClickable(True)
                view.setOnLongClickListener(listener)
            except Exception as e:
                log.log("term: cannot attach the long-press listener: %s" % e,
                        debug=True)

    # how far from the bottom still counts as being at the bottom. A line and
    # a bit: enough that a stray pixel does not read as "the reader has scrolled
    # away", and little enough that one deliberate swipe does.
    FOLLOW_SLACK = 48

    def at_bottom(self):
        """Is the newest line on the screen?

        Asked before following the output down. A program that prints for a
        minute was dragging the screen back to the bottom every time it said
        anything, so scrolling up during an install was impossible: the reader
        got one line of what they had gone to look at.
        """
        try:
            bottom = int(self._text_view.getBottom())
            visible = int(self._scroll.getHeight())
            position = int(self._scroll.getScrollY())
        except Exception:
            return True
        return position + visible >= bottom - self.FOLLOW_SLACK

    def scroll_to_bottom(self, only_if_following=False):
        """Deliberately not fullScroll(FOCUS_DOWN).

        fullScroll moves focus as well as scrolling — that is what the FOCUS_
        prefix means — and this runs on every keystroke, so it pulled focus out
        of the input each time a character was typed and closed the keyboard.
        scrollTo only scrolls.
        """
        if only_if_following and not self.at_bottom():
            return
        try:
            self._scroll.post(_Runnable(self._jump_to_bottom))
        except Exception:
            pass

    def _jump_to_bottom(self):
        bottom = int(self._text_view.getBottom())
        visible = int(self._scroll.getHeight())
        self._scroll.scrollTo(0, max(bottom - visible, 0))

    def text(self):
        lines = ["".join(run[0] for run in line) for line in self._lines]
        for line in self._screen.lines:
            if line:
                lines.append(style_base.strip_codes(line))
        return "\n".join(lines)

    def release(self):
        self._lines = []
        self._screen = style_base.Screen()
        self._input = []
        self._buffer = None
        self._buffer_stale = True

    # ---------------------------------------------------------------- drawing

    def _render(self):
        """Draws what changed.

        A terminal is almost entirely a thing that does not change: everything
        above the last screenful has scrolled past and will never move again.
        Rebuilding all of it — fifteen hundred lines of spans — for every
        chunk of output is what made each redraw stutter, and it is work
        nobody asked for.

        So the text is built once and kept, and a redraw rewrites only its
        tail: the lines a program can still reach, and the prompt under them.
        Lines that have scrolled out of reach are appended once and left alone.

        The finished text is still handed over with setText. Giving the view
        an editable buffer to hold and writing into that is faster still, and
        it cost the fling: an editable TextView scrolls its own text, so a
        flick that used to carry the scrollback from one end to the other
        stopped after an inch.
        """
        try:
            if self._buffer is None or self._buffer_stale or self._screen.alt:
                # the alternate screen is redrawn whole: every frame of it can
                # change every line, so there is no unchanged head to keep
                self._render_all()
                return
        except Exception as e:
            log.error("term: cannot redraw", e)
            return
        try:
            self._render_tail()
        except Exception as e:
            log.error("term: cannot redraw the tail", e)
            try:
                self._render_all()
            except Exception as second:
                # a console that cannot draw is bad; one that cannot take a
                # command because it cannot draw is unusable
                log.error("term: cannot redraw at all", second)

    def _render_all(self):
        from android.text import SpannableStringBuilder

        builder = SpannableStringBuilder()
        if not self._screen.alt:
            # while a program has a screen of its own, the scrollback is not on
            # screen at all — it is underneath, waiting to come back untouched
            self._append_lines(builder, self._lines)
        self._tail_start = builder.length()
        self._append_tail(builder)
        self._buffer = builder
        self._drawn_lines = len(self._lines)
        self._buffer_stale = False
        self._show()

    def _render_tail(self):
        from android.text import SpannableStringBuilder

        piece = SpannableStringBuilder()
        fresh = self._lines[self._drawn_lines:]
        if fresh:
            self._append_lines(piece, fresh, lead=self._tail_start > 0)
        mark = piece.length()
        self._append_tail(piece)
        self._buffer.replace(self._tail_start, self._buffer.length(), piece)
        self._tail_start += mark
        self._drawn_lines = len(self._lines)
        self._show()

    def _show(self):
        """Hands the text over the ordinary way.

        Plain setText, so the view stays what it was: text that is selected
        with a long press and scrolled by the ScrollView around it. The
        copying it does is native and is not what was slow — building the
        spans was, and that is now done once per line rather than once per
        frame.
        """
        try:
            self._text_view.setText(self._buffer)
        except Exception as e:
            log.error("term: cannot set text", e)

    def _append_lines(self, builder, lines, lead=False):
        """Adds parsed lines, one per line, keeping the colours."""
        for line in lines:
            if lead or builder.length():
                builder.append("\n")
            lead = False
            self._append_runs(builder, line)

    def _append_tail(self, builder):
        """The part that is still being written: the lines a program can still
        reach, and the prompt under them."""
        if self._screen.alt:
            self._append_screen(builder)
            return
        default = self.palette.role("fg")
        live = self._screen.lines
        if live and not live[-1]:
            # the last one is where the next character goes, not a blank line
            live = live[:-1]
        rows = [parse_ansi(line, default, self.palette) for line in live]
        input_start = None
        for line in rows:
            if builder.length() or self._tail_start:
                builder.append("\n")
            self._append_runs(builder, line)
        if self._input:
            if builder.length() or self._tail_start:
                builder.append("\n")
            input_start = builder.length()
            self._append_runs(builder, self._input)
        if input_start is not None and self._cursor is not None:
            self._paint_cursor(builder, input_start + self._cursor)

    def _append_screen(self, builder):
        """A screen a program owns: every row of it, blanks included.

        Nothing of ours is drawn over it — no input line, no prompt. The line
        the user types on belongs to the program now, and the cursor goes where
        the program put it, which is how an editor shows what it is editing.
        """
        default = self.palette.role("fg")
        cursor = None
        for index, line in enumerate(self._screen.lines):
            if index:
                builder.append("\n")
            start = builder.length()
            self._append_runs(builder, parse_ansi(line, default, self.palette))
            if index != self._screen.row:
                continue
            # There has to be a cell for the cursor to be. Typing at the end of
            # a line puts it past the last character, where the only thing to
            # swap the colours of was the newline — nothing at all, which is
            # why it went out while text was being added and came back the
            # moment anything was deleted.
            width = builder.length() - start
            if width <= self._screen.column:
                builder.append(" " * (self._screen.column - width))
                builder.append(CURSOR_CELL)
            cursor = start + self._screen.column
        if cursor is not None:
            self._paint_cursor(builder, cursor)

    def _append_runs(self, builder, line):
        from android.text import Spanned
        from android.text.style import BackgroundColorSpan, ForegroundColorSpan

        for chunk, color, background in line:
            start = builder.length()
            builder.append(chunk)
            if color is not None:
                builder.setSpan(ForegroundColorSpan(int(color)), start,
                                builder.length(),
                                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            if background is not None:
                builder.setSpan(BackgroundColorSpan(int(background)), start,
                                builder.length(),
                                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)

    def _paint_cursor(self, builder, position):
        """Swaps the colours of one cell, so the block is drawn and not typed.

        A █ in the text would look identical and copy along with the line, which
        is not what a cursor is.
        """
        from android.text import Spanned
        from android.text.style import BackgroundColorSpan, ForegroundColorSpan

        try:
            if position >= builder.length():
                # at the end of the line there is no cell yet; this is the
                # smallest thing that can carry the highlight
                builder.append(CURSOR_CELL)
            end = position + 1
            for span in (BackgroundColorSpan(int(self.palette.role("fg"))),
                         ForegroundColorSpan(int(self.palette.role("bg")))):
                builder.setSpan(span, position, end,
                                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
        except Exception as e:
            log.log("term: cannot draw the cursor: %s" % e, debug=True)


def _Runnable(function):
    """Kept as a name because callers read as if they were constructing one."""
    from ..compat import proxies

    return proxies.runnable(function)
