# SPDX-License-Identifier: Apache-2.0

"""Arranging the key rows, without a screen.

`ui.keyrows` draws with the client's widgets, but everything it decides —
where the add slots go, which key a tap landed on, what gets written down —
is ordinary Python that runs here. The editor is built without an activity,
which leaves `holder` unset and makes every redraw a no-op, so the state can
be poked at exactly as a finger would poke at it.
"""

import json
from pathlib import Path

from extcli_src.ui import keyrows, softkeys

LOCALES = Path(__file__).resolve().parent.parent / "extcli" / "locales"


def editor():
    """An editor with no screen behind it, starting from the defaults."""
    return keyrows._Editor(None)


def actions(editor_):
    return [[action for _label, action in row] for row in editor_.rows]


# ------------------------------------------------------------------ layout

def test_it_starts_from_what_the_console_would_draw():
    assert actions(editor()) == [[action for _label, action in row]
                                 for row in softkeys.DEFAULT_ROWS]


def test_every_row_gets_a_slot_to_add_to():
    drawn = editor()._layout()
    for row in drawn[:len(softkeys.DEFAULT_ROWS)]:
        assert row[-1] == (keyrows.ADD_LABEL, keyrows.ADD)


def test_a_full_row_is_not_offered_another_key():
    """There would be nowhere to put it, and the slot would be a lie."""
    one = editor()
    one.rows = [[("ESC", "cancel")] * softkeys.MAX_PER_ROW]
    row = one._layout()[0]
    assert len(row) == softkeys.MAX_PER_ROW
    assert keyrows.ADD not in [action for _label, action in row]


def test_there_is_a_way_to_start_a_new_row():
    drawn = editor()._layout()
    assert len(drawn) == len(softkeys.DEFAULT_ROWS) + 1
    assert drawn[-1] == ((keyrows.ADD_LABEL, keyrows.ADD),)


def test_no_more_rows_than_a_phone_can_hold():
    one = editor()
    one.rows = [[("ESC", "cancel")] for _ in range(keyrows.MAX_ROWS)]
    drawn = one._layout()
    assert len(drawn) == keyrows.MAX_ROWS


# ------------------------------------------------------------------ tapping

def test_a_tap_lands_on_the_key_that_was_tapped():
    """The same key twice on a row is a reasonable thing to want, and a tap on
    either of them has to say which one it was."""
    one = editor()
    one.rows = [[("ESC", "cancel"), ("TAB", "complete"), ("ESC", "cancel")]]
    seen = []
    one._change = lambda number, index: seen.append((number, index))
    one._tapped("cancel", 0, 2)
    assert seen == [(0, 2)]


def test_a_tap_on_the_slot_adds_rather_than_changes():
    one = editor()
    added, changed = [], []
    one._add = added.append
    one._change = lambda number, index: changed.append((number, index))
    one._tapped(keyrows.ADD, 1, len(one.rows[1]))
    assert added == [1] and changed == []


def test_a_tap_that_cannot_be_placed_changes_nothing():
    one = editor()
    before = actions(one)
    one._tapped("cancel", 9, 9)
    assert actions(one) == before


# ------------------------------------------------------------------ editing

def test_adding_a_key_puts_it_at_the_end_of_its_row():
    one = editor()
    one._insert(0, "insert:|")
    assert actions(one)[0][-1] == "insert:|"
    assert one.rows[0][-1][0] == softkeys.LABELS["insert:|"]


def test_adding_to_the_row_that_does_not_exist_yet_makes_it():
    one = editor()
    one._insert(len(one.rows), "clear")
    assert actions(one)[-1] == ["clear"]


def test_replacing_a_key_keeps_its_place():
    one = editor()
    one._replace(0, 1, "clear")
    assert actions(one)[0][:3] == ["cancel", "clear", "insert:-"]


def test_a_key_moves_one_place_and_stops_at_the_end():
    one = editor()
    one._move(0, 0, 1)
    assert actions(one)[0][:2] == ["insert:/", "cancel"]
    one._move(0, 0, -1)          # already at the start; nothing to do
    assert actions(one)[0][:2] == ["insert:/", "cancel"]


def test_emptying_a_row_takes_the_row_with_it():
    one = editor()
    one.rows = [[("ESC", "cancel")], [("TAB", "complete")]]
    one._remove(0, 0)
    assert actions(one) == [["complete"]]


def test_the_last_key_cannot_be_removed_into_nothing():
    """A keyboard with no keys is not an arrangement anybody meant to make,
    and there would be nothing left to tap to undo it."""
    one = editor()
    one.rows = [[("ESC", "cancel")]]
    one._remove(0, 0)
    assert actions(one) == [[action for _label, action in row]
                            for row in softkeys.DEFAULT_ROWS]


def test_reset_puts_the_defaults_back():
    one = editor()
    one._insert(0, "clear")
    one._reset()
    assert actions(one) == [[action for _label, action in row]
                            for row in softkeys.DEFAULT_ROWS]


# ------------------------------------------------------------------- saving

class _Dialog(object):
    def __init__(self):
        self.closed = False

    def dismiss(self):
        self.closed = True


def test_saving_the_defaults_stores_nothing(monkeypatch):
    """So that a build which changes the default rows changes them for
    everybody who never touched this screen."""
    written = []
    monkeypatch.setattr(keyrows.prefs, "remember_softkeys", written.append)
    monkeypatch.setattr(keyrows.dialogs, "toast", lambda *a, **k: True)

    dialog = _Dialog()
    editor()._save(dialog)
    assert written == [""] and dialog.closed


def test_saving_an_arrangement_stores_it(monkeypatch):
    written = []
    monkeypatch.setattr(keyrows.prefs, "remember_softkeys", written.append)
    monkeypatch.setattr(keyrows.dialogs, "toast", lambda *a, **k: True)

    one = editor()
    one.rows = [[("ESC", "cancel"), ("CLR", "clear")]]
    one._save(_Dialog())
    assert written == ["cancel,clear"]
    assert softkeys.parse(written[0]) == ((("ESC", "cancel"),
                                           ("CLR", "clear")),)


def test_what_is_saved_is_what_comes_back(monkeypatch):
    written = []
    monkeypatch.setattr(keyrows.prefs, "remember_softkeys", written.append)
    monkeypatch.setattr(keyrows.dialogs, "toast", lambda *a, **k: True)

    one = editor()
    one._insert(0, "insert:$")
    one._remove(1, 0)
    one._save(_Dialog())
    monkeypatch.setattr(keyrows.prefs, "softkey_layout", lambda: written[0])
    assert softkeys.rows() == tuple(tuple(row) for row in one.rows)


def test_a_store_that_refuses_says_so_rather_than_claiming_success(monkeypatch):
    said = []

    def refuse(_layout):
        raise RuntimeError("read-only")

    monkeypatch.setattr(keyrows.prefs, "remember_softkeys", refuse)
    monkeypatch.setattr(keyrows.dialogs, "toast",
                        lambda message, error=False: said.append((message,
                                                                  error)))
    editor()._save(_Dialog())
    assert said and said[0][1] is True and "read-only" in said[0][0]


# -------------------------------------------------------------- what it says

def test_the_punctuation_keys_share_one_sentence():
    """A locale entry per character would have to be written again for every
    new one, and they all do the same thing."""
    assert keyrows._about("insert:|", "unused") == "type |"
    assert keyrows._about("insert:$", "unused") == "type $"
    # anything else is looked up by name, and says what it was given when the
    # locale has nothing for it
    assert keyrows._about("no_such_key", "fallback") == "fallback"


def test_every_catalogue_key_has_something_to_say_in_both_languages():
    for name in ("strings_en.json", "strings_ru.json"):
        with open(LOCALES / name, encoding="utf-8") as handle:
            strings = json.load(handle)
        assert "keys_insert" in strings, name
        for action, _label, _about in softkeys.CATALOGUE:
            if action.startswith(keyrows.INSERT):
                continue
            assert "keys_%s" % action in strings, (name, action)


def test_the_add_slot_can_never_be_stored():
    """It is not a key; if it ever reached the store it would be dropped, and
    a row that silently loses a key is worse than one that cannot gain one."""
    assert keyrows.ADD not in softkeys.LABELS
    assert softkeys.parse(keyrows.ADD) is None
