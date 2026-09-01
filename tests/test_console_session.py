# SPDX-License-Identifier: Apache-2.0

"""The session outliving its screen.

Leaving the console with the back gesture takes the views down and keeps
everything else. That only works if output goes through the session rather than
straight into the terminal widget — the widget belongs to a fragment that can
be destroyed mid-command, and when it was the only place text lived, going back
lost the scrollback and the answer to whatever was still running.

These run without a client: the session is built for real, and a stand-in stands
where the Android terminal would be.
"""

import pytest

from extcli_src.render.styles import base
from extcli_src.ui import console


class FakeTerminal(object):
    """The parts of a terminal the session actually touches."""

    echoes_input = True

    def __init__(self):
        self.lines = []
        self.input_line = ""
        self.cursor = None
        self.released = False
        self.drawn = []

    def set_input_line(self, text, cursor=None):
        self.input_line = text
        self.cursor = cursor
        # a real terminal draws when this is called, so what has been written
        # by then is what that frame shows
        self.drawn.append(list(self.lines))


    def write_line(self, text=""):
        self.lines.append(text)

    def write_lines(self, lines):
        self.lines.extend(lines)

    def clear(self):
        self.lines = []

    def scroll_to_bottom(self, only_if_following=False):
        pass

    def append(self, text):
        self.appended = getattr(self, "appended", "") + str(text)

    def trim_trailing_blanks(self):
        return 0

    def release(self):
        self.released = True


class FakeField(object):
    """Just enough of an EditText for the session to write into.

    Without one, `_set_field` returns before it redraws — and the redraw is
    the whole reason the order of the two matters.
    """

    def __init__(self):
        self.text = ""
        self.position = 0

    def setText(self, text):
        self.text = str(text)
        self.position = len(self.text)

    def setSelection(self, position):
        self.position = int(position)

    def getSelectionStart(self):
        return self.position


@pytest.fixture(autouse=True)
def no_leftover_session():
    console.end_live_session()
    yield
    console.end_live_session()


@pytest.fixture
def session():
    live = console.resume_or_create(None, None)
    live.terminal = FakeTerminal()
    live.style = console.styles.get("termux")(live.palette, 40)
    return live


# ------------------------------------------------------------------ transcript

def test_output_goes_to_the_transcript_and_the_terminal(session):
    session.emit("hello")
    assert session.transcript == ["hello"]
    assert session.terminal.lines == ["hello"]


def test_output_arriving_while_detached_is_not_lost(session):
    session.emit("before")
    session.detach()
    # a command that was still running finishes into a session with no screen
    session.emit("after")
    assert session.transcript == ["before", "after"]


def test_a_new_terminal_replays_everything(session):
    session.emit("one")
    session.emit("two")
    session.detach()

    session.terminal = FakeTerminal()
    session.replay()
    assert session.terminal.lines == ["one", "two"]


def test_the_transcript_is_bounded():
    live = console.resume_or_create(None, None)
    live.scrollback = 40   # the setting, whatever it is set to
    for i in range(live.scrollback + 50):
        live.emit(str(i))
    assert len(live.transcript) == live.scrollback
    # the oldest lines go, the newest stay
    assert live.transcript[-1] == str(live.scrollback + 49)


def test_clear_wipes_the_transcript_too(session):
    session.emit("gone")
    session.wipe()
    assert session.transcript == []
    assert session.terminal.lines == []
    # otherwise the cleared lines would come back on the next open
    session.detach()
    session.terminal = FakeTerminal()
    session.replay()
    assert session.terminal.lines == []


# ------------------------------------------------------------------- lifecycle

def test_going_back_keeps_the_session_running(session):
    session.emit("work in progress")
    session.detach()
    assert console.live_session() is session
    assert console.resume_or_create(None, None) is session
    assert session.transcript == ["work in progress"]


def test_detaching_drops_the_views_and_releases_the_terminal(session):
    terminal = session.terminal
    session.detach()
    assert terminal.released
    assert session.terminal is None
    assert session.input_view is None
    assert session.window is None


def test_exit_ends_the_session(session):
    session.close(end_session=True)
    assert console.live_session() is None
    assert console.resume_or_create(None, None) is not session


def test_a_half_typed_line_survives_going_back(session):
    session._last_input = "plugin lis"
    session.detach()
    assert console.resume_or_create(None, None)._last_input == "plugin lis"


def test_the_greeting_is_written_once(session):
    session.greet()
    first = len(session.transcript)
    assert first > 0
    session.detach()
    session.terminal = FakeTerminal()
    session.start()
    # start() replays rather than greeting again
    assert len(session.transcript) == first


def test_the_greeting_names_the_way_out(session):
    session.greet()
    text = "\n".join(base.strip_codes(line) for line in session.transcript)
    assert "exit" in text
    assert "help" in text


# --------------------------------------------------------------- the prompt

def test_no_prompt_while_a_command_runs(session):
    """The output of a command must not arrive after its own next prompt.

    Drawing the prompt as soon as the line was submitted put the command, then
    an empty prompt, then the output — so the console looked like it was
    answering the wrong question.
    """
    session._busy = True
    session.refresh_input_line()
    assert session.terminal.input_line == ""


def test_the_prompt_comes_back_when_it_is_done(session):
    session._busy = False
    session.refresh_input_line()
    shown = base.strip_codes(session.terminal.input_line)
    assert shown.startswith("~ $")


def test_the_cursor_is_a_column_and_not_a_character(session):
    """A block character in the line would be copied along with the text.

    The terminal paints the cell instead, so the line holds only what the user
    typed.
    """
    session._last_input = "plugin list"
    session.refresh_input_line()
    shown = base.strip_codes(session.terminal.input_line)
    assert shown == "~ $ plugin list"
    assert "\u2588" not in shown
    # the column is past the prompt, at the end of what was typed
    assert session.terminal.cursor == len("~ $ ") + len("plugin list")


def test_the_cursor_column_follows_the_caret(session):
    session._last_input = "abcdef"
    session._selection = lambda: 2
    session.refresh_input_line()
    assert session.terminal.cursor == len("~ $ ") + 2


def test_the_line_is_on_screen_before_the_field_is_cleared(session):
    """Clearing the field redraws the terminal at once; output is drawn a frame
    later. Cleared first, the typed line was gone from the field and not yet
    echoed above it, and the prompt blinked out on every command."""
    session.input_view = FakeField()
    session.terminal.drawn = []
    session.submit("echo hi")
    assert session.terminal.drawn, "the terminal was never drawn"
    # the very first draw after submitting already has the echo in it
    first = session.terminal.drawn[0]
    assert first, "a frame with nothing on it: the line had vanished"
    assert base.strip_codes(first[0]) == "~ $ echo hi"


def test_a_command_is_echoed_then_answered(session):
    session.submit("echo hi")
    lines = [base.strip_codes(line) for line in session.transcript]
    assert lines[0] == "~ $ echo hi"
    assert lines[1] == "hi"
    # and the prompt is waiting again, below all of it
    assert session._busy is False
    assert base.strip_codes(session.terminal.input_line).startswith("~ $")


# ----------------------------------------------------------------- escape codes

def test_stripping_codes_leaves_the_text():
    line = base.colored("output", -1)
    assert base.strip_codes(line) == "output"
    assert base.visible_length(line) == len("output")


def test_stripping_an_unterminated_sequence_does_not_hang():
    assert base.strip_codes("text\x1b[38;2") == "text"


# --------------------------------------------------------- selecting text

class SelectableTerminal(FakeTerminal):
    """A terminal with a highlight in it, as the real one has while selecting."""

    def __init__(self):
        FakeTerminal.__init__(self)
        self.highlighted = False
        self.cleared = 0

    def has_selection(self):
        return self.highlighted

    def clear_selection(self):
        self.highlighted = False
        self.cleared += 1


def test_the_terminal_gives_focus_back_unless_it_is_selecting(session):
    """A selectable TextView takes focus in touch mode, and the input needs it.

    Without handing it straight back, typing stopped working after any tap on
    the terminal — which is exactly how it shipped.
    """
    session.terminal = SelectableTerminal()
    focused = []
    session.focus_input = lambda: focused.append(True)

    session.on_terminal_focus(True)
    assert focused, "focus was not returned to the input"

    del focused[:]
    session.terminal.highlighted = True
    session.on_terminal_focus(True)
    assert not focused, "focus was taken away mid-selection"


def test_a_long_press_is_left_to_the_view(session):
    # False, or the platform's own selection never runs
    assert session.on_terminal_long_press() is False


def test_the_input_line_is_not_redrawn_over_a_selection(session):
    session.terminal = SelectableTerminal()
    session.refresh_input_line()
    drawn = session.terminal.input_line
    session.terminal.highlighted = True
    session._last_input = "typed while selecting"
    session.refresh_input_line()
    assert session.terminal.input_line == drawn


def test_a_tap_always_returns_to_typing(session):
    session.terminal = SelectableTerminal()
    session.terminal.highlighted = True
    session._selection_open = True
    session.on_terminal_tap()
    assert not session.selecting()
    assert session.terminal.cleared == 1


def test_any_soft_key_also_returns_to_typing(session):
    session.terminal = SelectableTerminal()
    session._selection_open = True
    session.on_softkey("history_prev")
    assert not session.selecting()


def test_the_prompt_returns_when_the_toolbar_closes(session):
    session.terminal = SelectableTerminal()
    session._selection_open = True
    session.on_selection_ended()
    assert not session.selecting()
    assert base.strip_codes(session.terminal.input_line).startswith("~ $")


# ------------------------------------------------- a program's own output

def test_raw_output_reaches_the_terminal_and_the_transcript(session):
    """It goes to the terminal as it was written, and the transcript is kept
    in step by hand — output can arrive while the console is closed, and it
    has to be waiting when the screen comes back."""
    session._live_text("one\ntwo")
    assert session.terminal.appended == "one\ntwo"
    # both lines are on the screen; the second can still be written over, so
    # it is not scrollback yet
    assert session.lines() == ["one", "two"]
    session._settle_output()
    assert session.transcript == ["one", "two"]


def test_a_carriage_return_replaces_the_line_in_the_transcript(session):
    """A progress bar is one line sent over and over; the transcript should
    hold what it ended up saying, not every step of the way."""
    session._live_text("  1% #\r 50% ####\r100% #######\n")
    assert session.lines() == ["100% #######"]


def test_blank_lines_at_the_end_are_taken_back(session):
    session._live_text("done\n\n\x1b[K\n")
    session._settle_output()
    assert session.transcript == ["done"]


def test_the_transcript_is_kept_by_the_rules_the_screen_draws_by(session):
    """The same function, not a second reading of it: what a reopened screen
    replays should be what the screen showed."""
    session._live_text("\x1b[1G  1% #\x1b[1G 50% ####\x1b[1G100% ######\n")
    assert session.lines() == ["100% ######"]


# ------------------------------------------------- typing at a running program

def test_a_line_typed_while_a_command_runs_goes_to_the_command(session):
    """Pressing send during a run printed a new prompt. No terminal does that:
    what is typed then belongs to whatever is running."""
    typed = []
    session._busy = True
    session._attach_input(lambda text: typed.append(text) or True)
    session.submit("yes")
    assert typed == ["yes\n"]
    # and nothing that looks like a prompt was drawn
    assert not [line for line in session.transcript if "$" in line]


def test_an_empty_line_typed_at_a_running_command_is_still_a_line(session):
    """It is how you answer a program that is waiting for one."""
    typed = []
    session._busy = True
    session._attach_input(lambda text: typed.append(text) or True)
    session.submit("")
    assert typed == ["\n"]


def test_with_nothing_to_type_at_a_busy_console_says_so(session):
    """A builtin is not a program; there is no terminal behind it."""
    session._busy = True
    session._attach_input(None)
    session.submit("ls")
    assert any("still running" in line for line in session.transcript)


def test_the_channel_is_dropped_when_the_command_ends(session):
    session._busy = True
    session._attach_input(lambda text: True)
    session._finish(session.make_context(), None)
    assert session._input_channel is None


def test_ctrl_d_ends_a_running_program_s_input(session):
    """A terminal has no other way to say "that is all"; without it `cat` runs
    until the timeout."""
    typed = []
    session._busy = True
    session._attach_input(lambda text: typed.append(text) or True)
    session.on_ctrl("d")
    assert typed == ["\x04"]


def test_ctrl_d_on_an_idle_console_still_exits(session):
    session._busy = False
    session.submit = lambda line: session.transcript.append("submitted:%s" % line)
    session.on_ctrl("d")
    assert session.transcript[-1] == "submitted:exit"


class FakeChannel(object):
    """What the console is handed while a program runs."""

    def __init__(self, interruptible=True):
        self.typed = []
        self.signals = []
        self.interruptible = interruptible

    def __call__(self, text):
        self.typed.append(text)
        return True

    def interrupt(self):
        self.signals.append("int")
        return self.interruptible

    def stop(self):
        self.signals.append("kill")
        return True


def test_ctrl_c_interrupts_the_running_program(session):
    """It did nothing here: the child has no controlling terminal, so the tty
    driver had no foreground group to signal."""
    channel = FakeChannel()
    session._busy = True
    session._attach_input(channel)
    session.on_ctrl("c")
    assert channel.signals == ["int"]
    # and not typed at: ^C is a signal, not a character the program reads
    assert channel.typed == []


def test_a_second_ctrl_c_is_not_refusable(session):
    """A program that ignores SIGINT would otherwise leave the console waiting
    out the timeout."""
    channel = FakeChannel()
    session._busy = True
    session._attach_input(channel)
    session.on_ctrl("c")
    session.on_ctrl("c")
    assert channel.signals == ["int", "kill"]


def test_ctrl_c_with_nothing_running_still_clears_the_line(session):
    session._busy = False
    session._set_field("half typed")
    session.on_ctrl("c")
    assert session._last_input == ""
    assert any("^C" in line for line in session.transcript)


def test_the_next_command_starts_with_a_fresh_count(session):
    """Otherwise one ^C in an earlier command would make the next command's
    first ^C the unrefusable one."""
    first = FakeChannel()
    session._busy = True
    session._attach_input(first)
    session.on_ctrl("c")
    session._attach_input(None)
    second = FakeChannel()
    session._attach_input(second)
    session.on_ctrl("c")
    assert second.signals == ["int"]


# ---------------------------------------------- a rootfs arriving underneath

def test_a_session_picks_up_a_rootfs_that_appeared(session, monkeypatch):
    """The first setup runs in the background. A console opened while it was
    going was built without a rootfs, and would answer "not found" to `apk`
    until it was closed and opened again."""
    session.shell_env.set("MINE", "kept")
    session.shell_paths = None

    class Paths(object):
        active = True

        def home(self):
            return "/root"

        def host(self, path):
            return "/tmp"

    def arrived():
        session.shell_paths = Paths()
        return "a rootfs backend"

    monkeypatch.setattr(session, "_build_backends", arrived)
    assert session.rebuild_backends()
    assert session.backend == "a rootfs backend"
    # the shell moved into the rootfs, and nothing the user had set was lost
    assert session.shell_env.display_cwd() == "~"
    assert session.shell_env.get("MINE") == "kept"


def test_a_session_with_nothing_new_keeps_the_shell_it_had(session,
                                                           monkeypatch):
    """Rebuilding runs on every load; a console that is standing somewhere
    must not be moved because a measurement was repeated."""
    session.shell_env.cwd = "/root/work"
    monkeypatch.setattr(session, "_build_backends", lambda: "same")
    env = session.shell_env
    assert session.rebuild_backends()
    assert session.shell_env is env
    assert session.shell_env.cwd == "/root/work"


# ------------------------------------------------------- the setup bulletin

def test_the_bar_is_not_redrawn_hundreds_of_times_a_second():
    """The unpacking calls back every twenty-five files and the scan on every
    refused number, which is far more often than a bar six pixels tall can
    show."""
    from extcli_src.ui import progress

    # the first update always goes through
    assert progress.worth_drawing(0.0, "a", 0.0, None, 100.0, None)
    # too soon and too small a change
    assert not progress.worth_drawing(0.201, "a", 0.2, "a", 100.0, 99.99)
    # far enough apart in time and in distance
    assert progress.worth_drawing(0.3, "a", 0.2, "a", 100.0, 99.0)
    # a change of wording describes a different step and cannot wait
    assert progress.worth_drawing(0.2001, "b", 0.2, "a", 100.0, 99.99)
    # and the frame that says it is finished always arrives
    assert progress.worth_drawing(1.0, "a", 0.999, "a", 100.0, 99.999)


def test_the_bulletin_says_nothing_when_there_is_no_screen_to_say_it_on():
    """It is built on the load path, where the app may still be starting and
    there is no activity at all."""
    from extcli_src.ui import progress

    bulletin = progress.SetupBulletin()
    assert not bulletin.show()
    # and every other call is harmless afterwards
    bulletin.update(0.5, "unpacking")
    bulletin.finish(ok=True)
    bulletin.close()


def test_ours_waits_for_the_client_s_own_bulletin(monkeypatch):
    """The plugin is installed, the client says so, and the setup starts on
    the same breath. Two cards in the same place is one card nobody can
    read."""
    from extcli_src.ui import progress

    order = []

    class TheirBulletin(object):
        def hide(self):
            order.append("theirs hidden")

    bulletin = progress.SetupBulletin(activity="an activity")
    monkeypatch.setattr(progress, "_visible_bulletin",
                        lambda: TheirBulletin())
    monkeypatch.setattr(bulletin, "_build_safely",
                        lambda: order.append("ours shown"))
    # both timers run at once here; the order is what is being pinned
    monkeypatch.setattr(bulletin, "_later",
                        lambda function, delay: function())
    bulletin._after_the_other_one()
    assert order == ["theirs hidden", "ours shown"]


def test_with_nothing_else_on_screen_ours_goes_straight_up(monkeypatch):
    from extcli_src.ui import progress

    shown = []
    bulletin = progress.SetupBulletin(activity="an activity")
    monkeypatch.setattr(progress, "_visible_bulletin", lambda: None)
    monkeypatch.setattr(bulletin, "_build_safely",
                        lambda: shown.append(True))
    monkeypatch.setattr(bulletin, "_later",
                        lambda function, delay: shown.append("waited"))
    bulletin._after_the_other_one()
    assert shown == [True]


def test_the_card_is_taken_away_by_hiding_the_bulletin(monkeypatch):
    """It is the client's bulletin, not a view we added, so it goes the way
    every other bulletin goes rather than being pulled out of the tree."""
    from extcli_src.ui import progress

    done = []

    class OurBulletin(object):
        def setCanHide(self, value):
            done.append(("can hide", value))

        def hide(self):
            done.append(("hidden", True))

    bulletin = progress.SetupBulletin()
    bulletin.bulletin = OurBulletin()
    bulletin.card = "the layout"
    bulletin._remove()
    assert done == [("can hide", True), ("hidden", True)]
    assert bulletin.card is None and bulletin.bulletin is None


def test_a_colour_laid_over_another_is_worked_out_rather_than_blended():
    """Material draws the track and then a cap at the end of it, one over the
    other. A translucent colour drawn twice is darker where they overlap, and
    the overlap showed up as a grey half-circle after the blue."""
    from extcli_src.compat import theme

    black, white = 0xFF000000, 0xFFFFFFFF
    assert theme.mix(white, black, 0.0) == theme.signed(black)
    assert theme.mix(white, black, 1.0) == theme.signed(white)
    half = theme.mix(white, black, 0.5) & 0xFFFFFF
    assert half == 0x808080
    # and always opaque, whatever it is laid over
    assert (theme.mix(0x00FFFFFF, black, 0.5) >> 24) & 0xFF == 0xFF


def test_a_program_that_redraws_several_lines_does_not_stack_them(session):
    """`uv` draws a spinner, a line per file and a total, then moves the cursor
    up and writes the lot again. Twenty redraws were twenty times three lines
    of scrollback."""
    frame = "spinner\nfile\ntotal\n"
    session._live_text(frame)
    for _ in range(20):
        session._live_text("\x1b[3A\x1b[Jspinner\nfile\ntotal\n")
    session._settle_output()
    assert session.transcript == ["spinner", "file", "total"]


# ------------------------------------- keys, while a program owns the screen

def _running(session, raw=True):
    """A program that is running, and whether it is reading keys or lines."""
    channel = FakeChannel()
    channel.raw = lambda: raw
    session._busy = True
    session.input_view = FakeField()
    session._attach_input(channel)
    return channel


def test_keys_reach_a_program_that_reads_keys(session):
    """nano redraws on every keystroke: it cannot wait for Enter, and what it
    is given must be the key and nothing else."""
    channel = _running(session)
    session.on_input_changed("h")
    session.on_input_changed("hi")
    session.on_input_changed("hit")
    assert "".join(channel.typed) == "hit"
    # the field is not emptied between keys: clearing the region a soft
    # keyboard is still editing is what dropped keys when typing quickly
    assert session._last_input == "hit"


def test_a_keyboard_that_rewrites_a_word_is_followed(session):
    """An autocorrection replaces what was typed. To a program that is the
    same as taking a few characters back and typing a few more."""
    channel = _running(session)
    session.on_input_changed("teh")
    channel.typed[:] = []
    session.on_input_changed("the")
    assert "".join(channel.typed) == "\x7f\x7fhe"


def test_what_was_typed_at_a_program_does_not_turn_up_at_the_prompt(session):
    channel = _running(session)
    session.on_input_changed("inside nano")
    assert session._last_input == "inside nano"
    session._finish(session.make_context(), None)
    assert session._last_input == ""


def test_enter_is_a_key_too(session):
    channel = _running(session)
    session.submit("")
    assert "".join(channel.typed) == "\r"
    # nothing was echoed: the program draws its own screen
    assert session.transcript == []


def test_backspace_is_a_delete(session):
    channel = _running(session)
    session._last_input = "abc"
    session.on_input_changed("a")
    assert "".join(channel.typed) == "\x7f\x7f"


def test_a_control_combination_goes_through_as_one_byte(session):
    """^O is how nano saves. It never reached it: the console read every
    combination as one of its own."""
    channel = _running(session)
    session.on_ctrl("o")
    assert "".join(channel.typed) == "\x0f"


def test_the_key_row_sends_what_a_terminal_sends(session):
    channel = _running(session)
    for action in ("cancel", "complete", "history_prev", "left", "page_up"):
        session.on_softkey(action)
    assert "".join(channel.typed) == "\x1b\t\x1b[A\x1b[D\x1b[5~"


def test_a_third_interrupt_is_ours(session):
    """^C belongs to the program — nano asks "save?" with it — but a program
    that has stopped listening would leave the console with no way out."""
    channel = _running(session)
    for _ in range(3):
        session.on_ctrl("c")
    assert "".join(channel.typed) == "\x03\x03\x03"
    assert channel.signals == ["int"]


def test_a_program_that_reads_lines_still_gets_lines(session):
    """Everything that is not an editor: the line arrives at Enter, as before,
    and what is typed before it is not sent one character at a time."""
    channel = _running(session, raw=False)
    session.on_input_changed("yes")
    assert "".join(channel.typed) == ""
    session.submit("yes")
    assert "".join(channel.typed) == "yes\n"


def test_every_repeating_key_is_a_key_that_exists():
    """A key that repeats while it is held has to be one of the keys on the
    row, and has to be one the console knows what to do with."""
    from extcli_src.ui import softkeys

    on_the_row = {action for row in softkeys.ROWS for _label, action in row}
    for action in softkeys.REPEATING:
        assert action in on_the_row, action
    # and holding one must never be a thing that runs a command
    assert "complete" not in softkeys.REPEATING
    assert "ctrl" not in softkeys.REPEATING


def test_the_arrows_repeat_and_send_the_same_bytes_each_time():
    """Held down in an editor, an arrow is the same escape sequence over and
    over — which is what moving through a line looks like."""
    from extcli_src.ui import console as console_module
    from extcli_src.ui import softkeys

    for action in ("left", "right", "history_prev", "history_next"):
        assert action in softkeys.REPEATING
        assert action in console_module.RAW_KEYS


def test_the_program_is_told_when_the_screen_changes_size(session):
    """The keyboard coming and going resizes a terminal on a phone the way
    dragging a corner does on a desktop, and a program drawing a screen has no
    other way of hearing about it."""
    channel = _running(session)
    sizes = []
    channel.resize = lambda cols, rows: sizes.append((cols, rows))
    session.make_context()
    session.on_terminal_resized(40, 12)
    assert sizes == [(40, 12)]
    # and the shell is measuring the same screen the program is
    assert session.context.screen == (40, 12)


def test_a_console_coming_back_asks_the_program_to_draw_itself(session):
    """Leaving with the back gesture takes the views down and keeps the
    program. What it had drawn went with the views, and only the program knows
    what it looked like — so it is told the size twice and redraws."""
    channel = _running(session)
    sizes = []
    channel.resize = lambda cols, rows: sizes.append((cols, rows))
    session.terminal.metrics = lambda: (40, 12, 8, 16)
    session._screen.write("\x1b[?1049h")     # a program on its own screen
    session.replay()
    assert sizes == [(40, 11), (40, 12)], "one size twice is not a change"
    # and nothing of the scrollback was put on the program's screen
    assert session.terminal.lines == []


# ------------------------------------------------------------- the rc file

def _rc(session, tmp_path, text):
    session.shell_env.home = str(tmp_path)
    session.shell_env.cwd = str(tmp_path)
    (tmp_path / console.RC_FILE).write_text(text, encoding="utf-8")


def test_the_rc_file_lands_in_this_shell(session, tmp_path):
    """Sourced, not executed: what it defines has to be here afterwards, not
    in a child that exited on the last line."""
    _rc(session, tmp_path, "greeting=fromrc\nalias hi='echo hello'\n")
    session.run_rc()
    assert session.shell_env.get("greeting") == "fromrc"


def test_no_rc_file_is_not_an_event(session, tmp_path):
    session.shell_env.home = str(tmp_path)
    session.run_rc()
    assert session.transcript == []


def test_a_working_rc_file_says_nothing(session, tmp_path):
    """A console that prints "ok" above its first prompt prints it forever."""
    _rc(session, tmp_path, "quiet=yes\n")
    session.run_rc()
    assert session.transcript == []


def test_a_broken_rc_file_does_not_take_the_console_with_it(session, tmp_path):
    _rc(session, tmp_path, "nosuchcommand_at_all\n")
    session.run_rc()
    # it reported, and the console is still usable
    assert any("not found" in line for line in session.transcript)
    session.submit("echo alive")
    assert any("alive" in line for line in session.transcript)


def test_the_rc_file_is_read_once_per_session_not_per_screen(session, tmp_path):
    """Coming back from the back gesture is not a new shell."""
    _rc(session, tmp_path, "runs=$((runs+1))\n")
    session.start()
    session.detach()
    session.terminal = FakeTerminal()
    session.start()
    assert session.shell_env.get("runs") == "1"
