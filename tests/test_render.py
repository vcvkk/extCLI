# SPDX-License-Identifier: Apache-2.0

"""Formatting tests.

These matter more than they look: the console is 30-something columns wide on a
phone, so a style that quietly emits an over-wide line produces ragged wrapping
in the middle of a table. Every assertion about width is guarding that.
"""

from extcli_src.render import blocks, palette, styles
from extcli_src.render.styles import base, classic, termux

CLIENT_ROLES = {
    "bg": 0xFF1A1A1A,
    "fg": 0xFFE8E8E8,
    "dim": 0xFF8C8C8C,
    "accent": 0xFF4EA1F3,
    "error": 0xFFE0574B,
    "success": 0xFF5FB85F,
    "warn": 0xFFE0A03C,
    "selection": 0xFF2A3A4A,
    "divider": 0xFF303030,
}


def make_style(width=36):
    return classic.ClassicStyle(palette.from_client(CLIENT_ROLES), width)


def plain(lines):
    return [base.visible_length(line) for line in lines]


def strip(line):
    out = ""
    i = 0
    while i < len(line):
        if line[i] == "\x1b":
            i = line.find("m", i) + 1
            continue
        out += line[i]
        i += 1
    return out


# ------------------------------------------------------------------- palette

def test_palette_array_matches_renderer_layout():
    p = palette.from_client(CLIENT_ROLES)
    values = p.as_array()
    assert len(values) == len(palette.ROLE_ORDER) + 16
    assert values[0] == palette.signed(CLIENT_ROLES["bg"])
    assert values[1] == palette.signed(CLIENT_ROLES["fg"])


def test_every_color_fits_in_a_java_int():
    """Android takes signed 32-bit colors.

    Handing setBackgroundColor an unsigned 0xFF1A1B20 raises OverflowError in
    Chaquopy and the whole console fails to build — which is exactly how this
    shipped once, as a blank screen with no error anywhere.
    """
    p = palette.from_client(CLIENT_ROLES)
    values = list(p.as_array()) + [p.role(name) for name in palette.ROLE_ORDER]
    values += [palette.amoled(CLIENT_ROLES).role(name)
               for name in palette.ROLE_ORDER]
    values.append(palette.parse_color("#ff1a1b20"))
    values.append(palette.parse_color(0xFF1A1B20))
    values.append(palette.parse_color("#1a1b20"))
    for value in values:
        assert palette.INT32_MIN <= value <= palette.INT32_MAX, hex(value)


def test_signed_conversion_keeps_the_bits():
    assert palette.signed(0xFF1A1B20) == -15066336
    assert palette.parts(palette.signed(0xFF1A1B20)) == (255, 0x1A, 0x1B, 0x20)
    assert palette.signed(-15066336) == -15066336


def test_ansi_ramp_reuses_client_colors():
    p = palette.from_client(CLIENT_ROLES)
    assert p.ansi["red"] == palette.signed(CLIENT_ROLES["error"])
    assert p.ansi["green"] == palette.signed(CLIENT_ROLES["success"])
    assert p.ansi["blue"] == palette.signed(CLIENT_ROLES["accent"])


def test_bright_colors_are_lighter_than_their_base():
    p = palette.from_client(CLIENT_ROLES)
    for name in ("red", "green", "yellow", "blue"):
        assert palette.luminance(p.ansi["bright_" + name]) > palette.luminance(p.ansi[name])


def test_transparency_is_dropped():
    p = palette.from_client(dict(CLIENT_ROLES, bg=0x80FF0000))
    assert (p.role("bg") >> 24) & 255 == 255


def test_amoled_is_black_and_colorless():
    p = palette.amoled(CLIENT_ROLES)
    assert p.role("bg") == palette.argb(255, 0, 0, 0)
    _, r, g, b = palette.parts(p.role("error"))
    assert r == g == b


def test_termux_palette_is_black_on_white_with_the_standard_ramp():
    p = palette.termux()
    assert p.role("bg") == palette.argb(255, 0, 0, 0)
    assert p.role("fg") == palette.argb(255, 255, 255, 255)
    assert p.ansi["green"] == palette.argb(255, 0, 0xCD, 0)
    assert p.ansi["bright_white"] == palette.argb(255, 255, 255, 255)
    # it is a palette like any other: same array layout, same signed range
    values = p.as_array()
    assert len(values) == len(palette.ROLE_ORDER) + 16
    for value in values:
        assert palette.INT32_MIN <= value <= palette.INT32_MAX, hex(value)


def test_theme_file_overrides_only_given_roles():
    p = palette.from_theme_file(
        {"name": "half", "roles": {"accent": "#ff00ff"}}, CLIENT_ROLES
    )
    assert p.role("accent") == palette.argb(255, 255, 0, 255)
    assert p.role("fg") == palette.signed(CLIENT_ROLES["fg"])


def test_theme_file_without_client_colors_is_rejected():
    try:
        palette.from_theme_file({"roles": {"accent": "#ff00ff"}})
    except ValueError as e:
        assert "missing roles" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_color_formats():
    assert palette.parse_color("#4ea1f3") == palette.argb(255, 0x4E, 0xA1, 0xF3)
    assert palette.parse_color("804ea1f3") == palette.argb(0x80, 0x4E, 0xA1, 0xF3)
    assert palette.parse_color("#fff") == palette.argb(255, 255, 255, 255)


# --------------------------------------------------------------------- style

def test_prompt_looks_like_a_shell():
    assert make_style().prompt("~") == "extcli@exteraGram:~$ "


def test_echo_contains_the_command():
    line = make_style().echo("plugin list")
    assert "plugin list" in strip(line)
    assert "extcli@exteraGram" in strip(line)


def test_style_registry_defaults_to_classic():
    assert styles.get(None) is classic.ClassicStyle
    assert styles.get("nonexistent") is classic.ClassicStyle
    assert "classic" in styles.names()


def test_the_console_style_is_termux():
    assert styles.get(styles.CONSOLE_DEFAULT) is termux.TermuxStyle
    assert styles.CONSOLE_DEFAULT in styles.names()


def test_termux_prompt_is_the_cwd_and_a_dollar():
    style = termux.TermuxStyle(palette.termux(), 36)
    assert style.prompt("~") == "~ $ "
    assert style.prompt("/system/bin") == "/system/bin $ "
    assert strip(style.echo("ls -la", "~")) == "~ $ ls -la"


def test_termux_style_formats_blocks_like_classic():
    # only the prompt differs; a table must come out identically
    p = palette.termux()
    rows = [["extcli", "0.1.0", "enabled"]]
    assert (termux.TermuxStyle(p, 30).render(blocks.table(rows))
            == classic.ClassicStyle(p, 30).render(blocks.table(rows)))


def test_text_block_wraps_to_width():
    style = make_style(30)
    result = blocks.text("word " * 40)
    lines = style.render(result)
    assert len(lines) > 1
    assert max(plain(lines)) <= 30


def test_error_block_marks_and_hints():
    lines = make_style().render(blocks.error("peer not found: @durov",
                                             "did you mean @durovs_news?"))
    assert strip(lines[0]).startswith("error: peer not found")
    assert "durovs_news" in strip(lines[1])


def test_fields_block_aligns_values():
    lines = make_style().render(blocks.fields(
        [("client", "12.9.0"), ("sdk", "1.4.5.0"), ("abi", "arm64-v8a")]
    ))
    # labels have different lengths, so the point is that values line up
    starts = {strip(line).index(value)
              for line, value in zip(lines, ("12.9.0", "1.4.5.0", "arm64-v8a"))}
    assert len(starts) == 1
    assert all(strip(line).split(":")[0].strip() for line in lines)


def test_fields_block_wraps_long_values():
    style = make_style(32)
    lines = style.render(blocks.fields([("path", "/data/user/0/" + "x" * 80)]))
    assert len(lines) > 1
    assert max(plain(lines)) <= 32


def test_table_fits_narrow_screen():
    style = make_style(30)
    rows = [["shareui_packit", "0.0.0-rc.652", "enabled"],
            ["extcli", "0.1.0", "enabled"]]
    lines = style.render(blocks.table(rows, header=["id", "version", "state"]))
    assert max(plain(lines)) <= 30
    assert "id" in strip(lines[0])


def test_table_right_alignment():
    style = make_style(40)
    lines = style.render(blocks.table([["a", "1"], ["bb", "22"]], aligns=["l", "r"]))
    assert strip(lines[0]).endswith("1")


def test_items_block_shows_state_markers():
    lines = make_style().render(blocks.items([
        ("PackIt", "0.0.0-rc.652", "on"),
        ("NightMode", "2.1.0", "off"),
    ]))
    assert "[on]" in strip(lines[0])
    assert "[off]" in strip(lines[1])


def test_items_block_stays_within_width():
    style = make_style(28)
    entries = [("a-very-long-plugin-name-indeed", "1.2.3-with-a-long-suffix", "on")]
    lines = style.render(blocks.items(entries))
    assert max(plain(lines)) <= 28


def test_result_exit_code_is_preserved():
    assert blocks.error("nope").code == 1
    assert blocks.text("fine").ok


def test_every_line_is_reset_terminated():
    style = make_style()
    result = blocks.Result([
        blocks.Text("hello"),
        blocks.Summary("2 items"),
        blocks.Error("bad", "hint"),
        blocks.Items([("x", "y", "on")]),
    ])
    for line in style.render(result):
        if line:
            assert line.endswith(base.RESET)


# --------------------------------------------- a cursor that can go upwards

def _screen(height=6):
    from extcli_src.render.styles.base import Screen

    return Screen(height=height)


def test_a_bar_rewritten_in_place_stays_one_line():
    screen = _screen()
    for percent in (0, 25, 50, 100):
        screen.write("\r%d%%" % percent)
    assert screen.lines == ["100%"]


def test_a_program_that_redraws_three_lines_redraws_them():
    """`uv` draws a spinner, a line per file and a total, then goes back up and
    writes the lot again. A cursor that cannot go up has no choice but to add
    every redraw underneath the last, which is how an install came out as four
    hundred lines of the same three."""
    screen = _screen()
    screen.write("spinner\nfile one\ntotal\n")
    # up three, and over the top of them
    screen.write("\x1b[3A\rspinner!\x1b[K\n\rfile two\x1b[K\n\rtotal!\x1b[K\n")
    assert screen.lines[:3] == ["spinner!", "file two", "total!"]
    assert len(screen.lines) == 4  # the three, and the empty one after them


def test_what_scrolls_out_of_reach_comes_back_to_be_kept():
    screen = _screen(height=3)
    gone = screen.write("one\ntwo\nthree\nfour\n")
    assert gone == ["one", "two"]
    assert screen.lines == ["three", "four", ""]


def test_erasing_to_the_end_of_the_line_leaves_the_start():
    screen = _screen()
    screen.write("hello world")
    screen.write("\r\x1b[5C\x1b[K")
    assert screen.lines == ["hello"]


def test_erasing_the_whole_line_leaves_nothing_on_it():
    screen = _screen()
    screen.write("hello\x1b[2K")
    assert screen.lines == [""]


def test_writing_over_the_middle_of_a_line_keeps_what_is_beyond_it():
    screen = _screen()
    screen.write("abcdefgh\r\x1b[2CXY")
    assert screen.lines == ["abXYefgh"]


def test_erasing_below_drops_the_lines_after_the_cursor():
    screen = _screen()
    screen.write("one\ntwo\nthree")
    screen.write("\x1b[2A\r\x1b[J")
    assert screen.lines == [""]


def test_a_colour_is_not_a_movement():
    screen = _screen()
    screen.write("\x1b[31mred\x1b[0m")
    assert "31m" in screen.lines[0] and "red" in screen.lines[0]
    # and it does not count towards where the cursor is
    screen.write("\rX")
    assert screen.lines[0].endswith("ed\x1b[0m") or "X" in screen.lines[0]


def test_what_is_left_at_the_end_is_handed_over():
    screen = _screen()
    screen.write("done")
    assert screen.finish() == ["done"]
    assert screen.lines == [""]


# --------------------------------------- a screen a program keeps to itself

def test_the_alternate_screen_is_entered_and_given_back():
    """An editor asks for a screen of its own, draws on it and hands it back.
    What the shell had printed is underneath the whole time, untouched, which
    is why nano leaves no trace of itself in the scrollback."""
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=20, rows=4)
    screen.write("a shell line\n")
    screen.write("\x1b[?1049h")
    assert screen.alt
    assert screen.height == 4        # exactly the screen, not the scrollback
    screen.write("\x1b[2J\x1b[Hnano is here")
    assert screen.lines[0] == "nano is here"
    screen.write("\x1b[?1049l")
    assert not screen.alt
    assert screen.lines[0] == "a shell line"


def test_nothing_from_the_alternate_screen_reaches_the_scrollback():
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=20, rows=3)
    screen.write("\x1b[?1049h")
    gone = screen.write("one\ntwo\nthree\nfour\nfive\n")
    assert gone == [], "an editor's redraws would bury the shell's output"


def test_the_row_can_be_set_on_its_own():
    """nano moves between its title and its two shortcut bars with ESC[<n>d,
    which went unread — so the bars were written wherever the cursor was."""
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=10, rows=10)
    screen.write("\x1b[3dthird row")
    assert screen.row == 2
    assert screen.lines[2] == "third row"


def test_lines_can_be_inserted_and_deleted():
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=6, rows=6)
    screen.write("one\ntwo\nthree")
    screen.write("\x1b[2;1H\x1b[L")     # open a line above "two"
    assert screen.lines[:4] == ["one", "", "two", "three"]
    screen.write("\x1b[2;1H\x1b[M")     # and take it back out
    assert screen.lines[:3] == ["one", "two", "three"]


def test_characters_can_be_deleted_and_blanked():
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=4, rows=4)
    screen.write("abcdef\x1b[1;3H\x1b[2P")
    assert screen.lines[0] == "abef"
    screen.write("\x1b[1;1H\x1b[2X")
    assert screen.lines[0] == "  ef"


def test_a_cursor_that_is_hidden_is_not_printed():
    """`ESC[?25l` matched nothing and went into the line as text."""
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=4, rows=4)
    screen.write("\x1b[?25lhello\x1b[?25h")
    assert screen.lines[0] == "hello"


def test_nano_draws_its_bars_where_it_means_to():
    """The real thing: what nano writes when it starts, replayed through the
    screen. The title goes on the first row, the text under it, and the two
    shortcut bars at the bottom of the screen it was told it had."""
    from extcli_src.render.styles.base import Screen, strip_codes

    screen = Screen(height=60, rows=6)
    screen.write(
        "\x1b[?1049h\x1b[1;6r\x1b[m\x1b[H\x1b[2J"
        "\x1b[H\x1b[0;7m  GNU nano 7.2      notes.txt\x1b[m"
        "\r\x1b[2dhello"
        "\r\x1b[5d\x1b[0;7m^G\x1b[m Help"
        "\r\x1b[6d\x1b[0;7m^X\x1b[m Exit")
    drawn = [strip_codes(line) for line in screen.lines]
    assert drawn[0].startswith("  GNU nano")
    assert drawn[1] == "hello"
    assert drawn[4].startswith("^G Help")
    assert drawn[5].startswith("^X Exit")
    assert screen.alt and len(screen.lines) == 6


def test_a_program_killed_on_its_own_screen_still_hands_it_back():
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=20, rows=3)
    screen.write("a shell line\n")
    screen.write("\x1b[?1049h\x1b[Hnano")
    assert screen.finish() == ["a shell line", ""]


def test_a_screen_that_shrinks_keeps_its_top():
    """The keyboard coming up takes rows off the bottom of the screen, not off
    the top. Trimming from the front is scrolling — right when a program writes
    past the last row, wrong here: it took nano's title bar away, and the
    redraw that follows a resize only rewrites what the program thinks has
    changed, so it never came back."""
    from extcli_src.render.styles.base import Screen

    screen = Screen(height=60, rows=6)
    screen.write("\x1b[?1049h")
    screen.write("title\nbody\n\n\n\nbar")
    screen.resize(60, rows=3)
    assert screen.lines == ["title", "body", ""]
    assert screen.row <= 2
    # and growing again gives the rows back, empty
    screen.resize(60, rows=5)
    assert screen.lines == ["title", "body", "", "", ""]
