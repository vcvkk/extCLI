# SPDX-License-Identifier: Apache-2.0

"""ANSI parsing for the view-based terminal.

The console writes styled text through escape codes regardless of which
renderer it got, so the fallback has to understand the same subset the dex one
does — otherwise switching renderers would silently show escape codes as
garbage, or lose every color.
"""

from extcli_src.term.textview import parse_ansi

DEFAULT = -1
RED = -0x123456


def esc(params):
    return "\x1b[%sm" % params


def test_plain_text_is_one_run():
    assert parse_ansi("hello", DEFAULT) == [("hello", DEFAULT, None)]


def test_truecolor_sets_the_run_color():
    text = esc("38;2;255;0;0") + "danger" + esc("0") + "calm"
    runs = parse_ansi(text, DEFAULT)
    assert runs[0][0] == "danger"
    assert runs[0][1] == -65536  # 0xFFFF0000 as a signed int
    assert runs[1] == ("calm", DEFAULT, None)


def test_reset_returns_to_the_default():
    runs = parse_ansi(esc("38;2;1;2;3") + "x" + esc("") + "y", DEFAULT)
    assert runs[-1] == ("y", DEFAULT, None)


def test_code_39_also_resets():
    runs = parse_ansi(esc("38;2;1;2;3") + "x" + esc("39") + "y", DEFAULT)
    assert runs[-1] == ("y", DEFAULT, None)


def test_unknown_codes_do_not_break_the_text():
    runs = parse_ansi(esc("1") + "bold" + esc("22") + "plain", DEFAULT)
    assert "".join(chunk for chunk, _fg, _bg in runs) == "boldplain"


def test_other_escape_sequences_are_dropped_not_shown():
    runs = parse_ansi("before\x1b[2Jafter", DEFAULT)
    assert "".join(chunk for chunk, _fg, _bg in runs) == "beforeafter"


def test_newlines_are_preserved_for_the_line_splitter():
    runs = parse_ansi("one\ntwo", DEFAULT)
    assert "".join(chunk for chunk, _fg, _bg in runs) == "one\ntwo"


def test_empty_input():
    assert parse_ansi("", DEFAULT) == []


def test_colors_survive_across_newlines():
    runs = parse_ansi(esc("38;2;0;255;0") + "green\nstill green", DEFAULT)
    assert len(runs) == 1
    assert "\n" in runs[0][0]


def test_truncated_truecolor_does_not_eat_the_text():
    # only three of the five parameters arrived
    runs = parse_ansi(esc("38;2;255") + "text", DEFAULT)
    assert "".join(chunk for chunk, _fg, _bg in runs) == "text"


def test_out_of_range_components_do_not_raise():
    runs = parse_ansi(esc("38;2;999;0;0") + "text", DEFAULT)
    assert "".join(chunk for chunk, _fg, _bg in runs) == "text"


def test_the_renderer_and_the_style_agree_on_the_escape_format():
    # what render/styles/base.py emits must be what this parses
    from extcli_src.render.styles import base

    line = base.colored("output", RED)
    runs = parse_ansi(line, DEFAULT)
    assert [chunk for chunk, _fg, _bg in runs] == ["output"]
    assert runs[0][1] != DEFAULT


# ------------------------------------- the colours turned round, and a screen

def test_reverse_video_paints_a_bar():
    """nano's title and shortcut bars are `ESC[7m`: not a colour of its own,
    but the two it has, the other way up. Without a background they came out
    as ordinary text and the interface read as broken."""
    from extcli_src.render import palette as palette_module

    palette = palette_module.termux()
    runs = parse_ansi("\x1b[0;7m bar \x1b[m after", palette.role("fg"), palette)
    text, fg, bg = runs[0]
    assert text == " bar "
    assert bg == palette.role("fg")   # the text colour, now behind the text
    assert fg == palette.role("bg")   # and the background, now in front
    assert runs[1][2] is None         # and it ends where it says it ends


def test_reverse_keeps_a_real_background_if_there_is_one():
    from extcli_src.render import palette as palette_module

    palette = palette_module.termux()
    runs = parse_ansi("\x1b[31;44;7mx", palette.role("fg"), palette)
    _text, fg, bg = runs[0]
    assert fg == palette.ansi_color(4)
    assert bg == palette.ansi_color(1)


def test_reverse_can_be_turned_off_again():
    from extcli_src.render import palette as palette_module

    palette = palette_module.termux()
    runs = parse_ansi("\x1b[7mon\x1b[27moff", palette.role("fg"), palette)
    assert runs[0][2] is not None
    assert runs[1][2] is None


def test_a_background_colour_is_kept():
    from extcli_src.render import palette as palette_module

    palette = palette_module.termux()
    runs = parse_ansi("\x1b[41mred behind\x1b[49mnone", palette.role("fg"),
                      palette)
    assert runs[0][2] == palette.ansi_color(1)
    assert runs[1][2] is None


def test_every_run_the_terminal_builds_has_the_same_shape():
    """The blank cell was written by hand with two parts when a run grew a
    third. Every redraw after a command started raised instead of drawing: the
    console froze with the line echoed and the command never ran, on the first
    command of the session, whatever it was."""
    from extcli_src.term.textview import blank_cell

    parsed = parse_ansi("x", DEFAULT)[0]
    made = blank_cell(DEFAULT)[0]
    assert len(made) == len(parsed) == 3
    assert made[1] == DEFAULT and made[2] is None
    # and the cell is one the layout keeps: a background is not painted over
    # trailing whitespace, which is where the cursor sits while a program runs
    assert made[0] == "\u00a0"
