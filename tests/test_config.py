# SPDX-License-Identifier: Apache-2.0

"""`config` — reading and writing the client's own settings.

The dangerous part is types. exteraGram reads each key with a typed getter, so
writing `squareAvatars` as the string "true" instead of a boolean makes the
client throw ClassCastException on its next start — a broken settings screen,
not a wrong value. These tests pin that the stored type wins over whatever the
user typed.
"""

import json

import pytest

from extcli_src import policy as policy_module
from extcli_src.render import plain
from extcli_src.shell import dispatch
from extcli_src.shell.builtins import build_registry
from extcli_src.shell.context import Context, Services
from extcli_src.shell.env import Env


class FakeSettings(object):
    """Stands in for SharedPreferences, keeping Python types like the real
    conversion does."""

    def __init__(self):
        self.stores = {
            "exteraconfig": {
                "squareAvatars": True,
                "avatarCorners": 16,
                "drawerBlur": False,
                "downloadPath": "/sdcard/Download",
                "fontSize": 1.5,
            },
            "mainconfig": {"lastUpdateVersion": 70079},
        }
        self.writes = []

    def all_values(self, store="exteraconfig"):
        return dict(self.stores.get(store, {}))

    def type_name(self, value):
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        return "string"

    def set_value(self, key, value, store="exteraconfig", existing=None):
        target = existing if existing is not None else value
        if isinstance(target, bool):
            if isinstance(value, bool):
                stored = value
            elif str(value).lower() in ("true", "1", "yes", "on"):
                stored = True
            elif str(value).lower() in ("false", "0", "no", "off"):
                stored = False
            else:
                return False, "%s is not a boolean" % value
        elif isinstance(target, int):
            try:
                stored = int(value)
            except (TypeError, ValueError):
                return False, "%s is not a number" % value
        elif isinstance(target, float):
            stored = float(value)
        else:
            stored = str(value)
        self.stores.setdefault(store, {})[key] = stored
        self.writes.append((store, key, stored))
        return True, "%s = %s" % (key, stored)

    def remove(self, key, store="exteraconfig"):
        self.stores.get(store, {}).pop(key, None)
        return True, "%s removed" % key

    def search(self, query, store="exteraconfig"):
        needle = query.lower()
        return {k: v for k, v in self.stores.get(store, {}).items()
                if needle in k.lower() or needle in str(v).lower()}

    def describe(self):
        return [(name, "%d keys" % len(values))
                for name, values in sorted(self.stores.items())]


@pytest.fixture
def shell(tmp_path):
    home = str(tmp_path)
    settings = FakeSettings()
    ctx = Context(
        services=Services(settings=settings, policy=policy_module),
        registry=build_registry(),
        env=Env(cwd=home, home=home),
        width=70,
    )

    def run(line):
        return dispatch.run_line(line, ctx)

    run.settings = settings
    run.ctx = ctx
    run.out = lambda line: plain.text(run(line))
    return run


# ------------------------------------------------------------------ reading

def test_list_shows_values_with_types_preserved(shell):
    text = shell.out("config list")
    assert "squareAvatars" in text and "true" in text
    assert "avatarCorners" in text and "16" in text


def test_list_can_be_narrowed_by_prefix(shell):
    text = shell.out("config list avatar")
    assert "avatarCorners" in text
    assert "drawerBlur" not in text


def test_list_reads_another_store(shell):
    text = shell.out("config list --store mainconfig")
    assert "lastUpdateVersion" in text


def test_get_reports_the_type(shell):
    text = shell.out("config get squareAvatars")
    assert "squareAvatars = true" in text
    assert "bool" in text


def test_get_unknown_key_suggests(shell):
    result = shell("config get squareAvatar")
    assert result.code != 0
    assert "squareAvatars" in (result.blocks[0].hint or "")


def test_search_matches_names_and_values(shell):
    assert "downloadPath" in shell.out("config search sdcard")
    assert "drawerBlur" in shell.out("config search drawer")


def test_stores_lists_preference_files(shell):
    text = shell.out("config stores")
    assert "exteraconfig" in text and "mainconfig" in text


# ------------------------------------------------------------------ writing

def test_set_keeps_the_stored_type(shell):
    shell("config set squareAvatars false")
    assert shell.settings.stores["exteraconfig"]["squareAvatars"] is False


def test_set_accepts_shell_style_booleans(shell):
    shell("config set drawerBlur on")
    assert shell.settings.stores["exteraconfig"]["drawerBlur"] is True


def test_set_does_not_turn_an_int_into_a_string(shell):
    shell("config set avatarCorners 8")
    stored = shell.settings.stores["exteraconfig"]["avatarCorners"]
    assert stored == 8 and isinstance(stored, int)


def test_set_rejects_a_value_of_the_wrong_type(shell):
    result = shell("config set avatarCorners nonsense")
    assert result.code != 0
    assert shell.settings.stores["exteraconfig"]["avatarCorners"] == 16


def test_set_refuses_unknown_keys_by_default(shell):
    result = shell("config set madeUpKey 1")
    assert result.code != 0
    assert "madeUpKey" not in shell.settings.stores["exteraconfig"]


def test_set_can_create_a_key_on_request(shell):
    assert shell("config set madeUpKey hello --new").ok
    assert shell.settings.stores["exteraconfig"]["madeUpKey"] == "hello"


def test_set_mentions_the_restart(shell):
    assert "restart" in shell.out("config set squareAvatars false")


def test_unset_removes(shell):
    shell("config unset drawerBlur")
    assert "drawerBlur" not in shell.settings.stores["exteraconfig"]


def test_unset_unknown_key_is_refused(shell):
    assert shell("config unset nothingHere").code != 0


def test_writes_go_through_policy(shell):
    seen = []
    original = policy_module.check

    def spy(action, detail="", assume_yes=False):
        seen.append(action)
        return original(action, detail, assume_yes)

    policy_module.check = spy
    try:
        shell("config set squareAvatars false")
        shell("config unset drawerBlur")
    finally:
        policy_module.check = original
    assert seen.count(policy_module.CLIENT_CONFIG) == 2


# ---------------------------------------------------------------- transfer

def test_export_then_import_round_trip(shell, tmp_path):
    path = str(tmp_path / "settings.json")
    assert shell("config export %s" % path).ok
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["store"] == "exteraconfig"
    assert data["values"]["squareAvatars"] is True

    shell("config set squareAvatars false")
    assert shell("config import %s" % path).ok
    assert shell.settings.stores["exteraconfig"]["squareAvatars"] is True


def test_import_skips_keys_the_client_does_not_have(shell, tmp_path):
    path = str(tmp_path / "extra.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"store": "exteraconfig",
                   "values": {"squareAvatars": False, "unknownKey": 1}}, handle)
    text = shell.out("config import %s" % path)
    assert "1 applied, 1 skipped" in text
    assert "unknownKey" not in shell.settings.stores["exteraconfig"]


def test_import_rejects_a_file_that_is_not_an_export(shell, tmp_path):
    path = str(tmp_path / "junk.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([1, 2, 3], handle)
    result = shell("config import %s" % path)
    assert result.code != 0
    assert "not a settings export" in result.blocks[0].message


def test_export_is_pipeable_through_the_shell(shell, tmp_path):
    path = str(tmp_path / "out.json")
    shell("config export %s" % path)
    assert shell("test -s %s" % path).ok


# ------------------------------------------------------------- availability

def test_without_the_client_the_command_explains_itself():
    ctx = Context(registry=build_registry(), env=Env(cwd="/", home="/"), width=60)
    result = dispatch.run_line("config list", ctx)
    assert "settings is not available here" in result.blocks[0].message
