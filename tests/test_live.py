# SPDX-License-Identifier: Apache-2.0

"""A command's output in a message that keeps changing.

What the message says and when it is written is all here, away from anything
that needs a client — which is the point of the split: the rate limiting is
the part that is expensive to get wrong and impossible to test on a device
without getting rate limited.
"""

from extcli_src.shell import live


def _view(**kwargs):
    kwargs.setdefault("interval", 5.0)
    return live.LiveText(**kwargs)


# ------------------------------------------------------------------ the text

def test_a_progress_bar_is_one_line_not_forty():
    """apk rewrites the same line dozens of times a second. The message should
    show what it ended up saying."""
    view = _view()
    view.write("  1% #\r 50% ####\r100% ######\n")
    assert view.text().strip().endswith("100% ######")
    assert "1% #" not in view.text()


def test_the_tail_is_what_survives_the_limit():
    """While something is running, the end is the part being watched."""
    view = _view(limit=200)
    for i in range(200):
        view.write("line %d\n" % i)
    text = view.text()
    assert len(text) <= 200
    assert "line 199" in text
    assert text.startswith(live.ELLIPSIS)


def test_the_header_says_what_is_running():
    view = _view(header="$ apk add git")
    view.write("working\n")
    assert view.text().startswith("$ apk add git\n")


def test_colours_do_not_reach_the_chat():
    """A chat is not a terminal; escape codes there are noise."""
    view = _view()
    view.write("\x1b[31mred\x1b[0m\n")
    assert "\x1b" not in view.text()
    assert "red" in view.text()


def test_blank_lines_at_the_end_are_taken_back():
    view = _view()
    view.write("done\n\n\n")
    view.finish()
    assert view.text().strip() == "done"


# ------------------------------------------------------------------- the rate

def test_nothing_is_written_before_there_is_something_to_say():
    view = _view()
    assert not view.due(0.0), "an empty message is not worth sending"


def test_an_edit_waits_for_the_interval():
    view = _view(interval=5.0)
    view.write("one\n")
    assert view.due(100.0)
    view.sent(100.0)
    view.write("two\n")
    assert not view.due(104.9)
    assert view.due(105.0)


def test_unchanged_text_is_not_sent_again():
    """An edit that says what the message already says spends the account's
    allowance on nothing."""
    view = _view(interval=1.0)
    view.write("one\n")
    view.sent(100.0)
    assert not view.due(200.0)
    view.write("two\n")
    assert view.due(200.0)


def test_the_floor_cannot_be_gone_under():
    """Telegram publishes no limits, so the floor is the one number this code
    is entitled to insist on."""
    assert _view(interval=0.1).interval == live.MIN_INTERVAL
    assert _view(interval=30).interval == 30


def test_the_server_is_obeyed_when_it_says_to_wait():
    view = _view(interval=5.0)
    view.write("one\n")
    view.sent(100.0)
    view.write("two\n")
    view.defer(105.0, 60)
    assert not view.due(150.0)
    assert view.due(165.0)
    # and the interval it asked for is kept, not just this once
    assert view.interval >= 60


def test_the_wait_is_read_out_of_the_error():
    """The only number in this business that is not a guess."""
    assert live.flood_wait_seconds("FLOOD_WAIT_42") == 42
    assert live.flood_wait_seconds("rpc error: FLOOD_WAIT_7 (something)") == 7
    assert live.flood_wait_seconds("PEER_ID_INVALID") is None
    assert live.flood_wait_seconds(None) is None
    assert live.flood_wait_seconds("FLOOD_WAIT_") is None


def test_the_interval_offered_in_settings_never_goes_under_the_floor():
    from extcli_src.ui import prefs

    assert min(prefs.CHAT_INTERVALS) >= live.MIN_INTERVAL
    assert prefs.CHAT_INTERVALS[prefs.DEFAULT_CHAT_INTERVAL_INDEX] == 5
