# SPDX-License-Identifier: Apache-2.0

"""Settings are stored as selector indices, and two modules read them.

If prefs and the settings page ever disagree about what index 1 means, the user
picks Amoled and gets the client theme. These tests pin the mapping, and check
that garbage in storage falls back instead of raising.
"""

import json
from pathlib import Path

from extcli_src.compat import i18n
from extcli_src.rootfs import packages
from extcli_src.ui import prefs

LOCALES = Path(__file__).resolve().parent.parent / "extcli" / "locales"
SRC = Path(__file__).resolve().parent.parent / "extcli" / "src"


def test_defaults_without_a_client():
    # elyx is absent here, so every getter must fall back
    assert prefs.theme_name() == "termux"
    assert prefs.text_size() == 12
    assert prefs.style_name() == "termux"
    assert prefs.console_surface() == "screen"
    assert prefs.entry_enabled("drawer") is True
    assert prefs.debug_logs() is False


def test_index_mapping(monkeypatch):
    stored = {}
    monkeypatch.setattr(prefs, "_get", lambda key, default: stored.get(key, default))

    stored["theme"] = 0
    assert prefs.theme_name() == "termux"
    stored["theme"] = 1
    assert prefs.theme_name() == "default"
    stored["theme"] = 2
    assert prefs.theme_name() == "amoled"

    # the console opens full screen unless the sheet is picked explicitly
    stored["console_surface"] = 1
    assert prefs.console_surface() == "sheet"

    stored["text_size_index"] = 0
    assert prefs.text_size() == 10
    stored["text_size_index"] = len(prefs.TEXT_SIZES) - 1
    assert prefs.text_size() == 16


def test_out_of_range_and_garbage_fall_back(monkeypatch):
    stored = {}
    monkeypatch.setattr(prefs, "_get", lambda key, default: stored.get(key, default))

    for bad in (99, -1, "nonsense", None, 1.5):
        stored["theme"] = bad
        assert prefs.theme_name() in prefs.THEMES
        stored["text_size_index"] = bad
        assert prefs.text_size() in prefs.TEXT_SIZES


def test_unknown_style_falls_back(monkeypatch):
    monkeypatch.setattr(prefs, "_get", lambda key, default: "panels-that-do-not-exist")
    assert prefs.style_name() == "termux"


def test_settings_page_labels_cover_every_option():
    # the page renders one label per TEXT_SIZES entry and per THEMES entry
    assert len(prefs.TEXT_SIZES) == len(set(prefs.TEXT_SIZES))
    assert prefs.DEFAULT_TEXT_SIZE_INDEX < len(prefs.TEXT_SIZES)
    assert prefs.DEFAULT_THEME_INDEX < len(prefs.THEMES)


def test_locales_have_the_same_keys():
    keys = {}
    for path in sorted(LOCALES.glob("strings_*.json")):
        with open(path, encoding="utf-8") as f:
            keys[path.name] = set(json.load(f))
    assert len(keys) >= 2
    reference = keys["strings_en.json"]
    for name, entries in keys.items():
        assert entries == reference, "%s differs: %s" % (name, entries ^ reference)


def test_every_string_the_page_asks_for_is_translated():
    """A fallback written into the code is English for everybody. The settings
    page is where the plugin talks to somebody who has never opened a console,
    so nothing there may rely on one."""
    import re

    source = ""
    for name in ("settings_page.py", "progress.py", "toolsheet.py"):
        with open(SRC / "ui" / name, encoding="utf-8") as f:
            source += f.read()
    asked = set(re.findall(r'_s\(\s*"([^"]+)"', source))
    asked |= set(re.findall(r'i18n\.get\(\s*"([^"]+)"', source))
    # a key built from a name is checked against the names themselves below
    asked = {key for key in asked if "%" not in key}
    for group in packages.GROUPS:
        asked.add("tools_%s_label" % group.name)
        asked.add("tools_%s_desc" % group.name)
    # every form of a phrase that changes with a number, since the number
    # decides which one is looked up
    for family in re.findall(r'i18n\.plural\(\s*"([^"]+)"', source):
        for form in i18n.PLURAL_FORMS:
            asked.add("%s_%s" % (family, form))
    with open(LOCALES / "strings_en.json", encoding="utf-8") as f:
        entries = json.load(f)
    missing = sorted(key for key in asked if key not in entries)
    assert not missing, "no string for: %s" % ", ".join(missing)
    assert asked, "the page asks for no strings at all, which cannot be right"


def test_the_russian_is_russian():
    """Copying the English across counts as a missing translation, not a
    finished one. Names and words that are the same in both are short."""
    with open(LOCALES / "strings_en.json", encoding="utf-8") as f:
        english = json.load(f)
    with open(LOCALES / "strings_ru.json", encoding="utf-8") as f:
        russian = json.load(f)
    same = [key for key, value in english.items()
            if russian.get(key) == value and len(value) > 12]
    assert not same, "still in English: %s" % ", ".join(same)


def test_locales_have_a_plugin_description():
    with open(LOCALES / "strings_en.json", encoding="utf-8") as f:
        entries = json.load(f)
    # meta.yml interpolates {plugin_description}
    assert entries.get("plugin_description")


def test_the_fastfetch_config_is_shipped_and_is_json():
    """fastfetch reads it out of the container, and the plugin puts it there —
    a phone is forty columns wide and the logo every desktop shows is a third
    of that."""
    import json
    import re

    path = SRC.parent / "res" / "config" / "fastfetch.jsonc"
    assert path.exists(), "the config the look toolset installs is missing"
    text = path.read_text(encoding="utf-8")
    # jsonc: the comments at the top are for whoever opens it, not for the
    # parser, and the parser is what has to be kept happy
    without_comments = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    config = json.loads(without_comments)
    assert config["logo"]["type"] == "small"
    assert config["modules"]


# ---------------------------------------------------------------- key rows

def test_the_default_rows_are_what_they_always_were():
    from extcli_src.ui import softkeys

    assert len(softkeys.DEFAULT_ROWS) == 2
    assert softkeys.DEFAULT_ROWS[0][0] == ("ESC", "cancel")


def test_rows_survive_a_round_trip():
    from extcli_src.ui import softkeys

    assert softkeys.parse(softkeys.serialise(softkeys.DEFAULT_ROWS)) == \
        softkeys.DEFAULT_ROWS


def test_only_the_action_is_stored():
    """A key's caption is not the user's to change, and storing it would let
    the caption and the behaviour drift apart."""
    from extcli_src.ui import softkeys

    stored = softkeys.serialise(((("X", "cancel"),),))
    assert "X" not in stored and "cancel" in stored


def test_a_key_this_build_does_not_have_is_dropped():
    """It would be a key that silently does nothing when pressed."""
    from extcli_src.ui import softkeys

    rows = softkeys.parse("cancel,teleport,complete")
    assert [action for _label, action in rows[0]] == ["cancel", "complete"]


def test_nothing_stored_means_the_defaults():
    from extcli_src.ui import softkeys

    assert softkeys.parse("") is None
    assert softkeys.parse(None) is None
    assert softkeys.parse("nothing,real,here") is None


def test_a_row_cannot_be_made_unhittable():
    from extcli_src.ui import softkeys

    crowded = ",".join(["cancel"] * 30)
    assert len(softkeys.parse(crowded)[0]) == softkeys.MAX_PER_ROW


def test_every_catalogue_key_is_one_the_console_understands():
    """A key on the row that the console has no branch for is a key that does
    nothing, which is worse than not offering it."""
    from extcli_src.ui import console, softkeys

    handled = set(console.RAW_KEYS)
    handled.update({"complete", "history_prev", "history_next", "clear",
                    "cancel", "home", "end", "left", "right",
                    "page_up", "page_down", "ctrl", "alt"})
    for action, _label, _about in softkeys.CATALOGUE:
        assert action in handled or action.startswith("insert:"), action


def test_every_default_key_is_in_the_catalogue():
    from extcli_src.ui import softkeys

    for row in softkeys.DEFAULT_ROWS:
        for _label, action in row:
            assert action in softkeys.LABELS, action
