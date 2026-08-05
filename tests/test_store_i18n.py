# SPDX-License-Identifier: Apache-2.0

"""Settings, strings and meta.yml on clients with and without `elyx`.

exteraGram 12.9.0 ships plugin SDK 1.4.5.0, which has no `elyx` module at all —
`from elyx import settings` simply fails there. SDK 1.4.5.3 does provide it.
extCLI has to work on both, so these tests cover the fallback chain rather than
assuming one SDK.
"""

from extcli_src.compat import i18n, meta, store


class FakePlugin(object):
    """Stands in for BasePlugin, whose get_setting/set_setting exist in every
    SDK version."""

    def __init__(self, values=None, broken=False):
        self.values = dict(values or {})
        self.broken = broken
        self.writes = []

    def get_setting(self, key, default=None):
        if self.broken:
            raise RuntimeError("no settings backend")
        return self.values.get(key, default)

    def set_setting(self, key, value):
        if self.broken:
            raise RuntimeError("no settings backend")
        self.writes.append((key, value))
        self.values[key] = value

    def get_all_settings(self):
        return dict(self.values)


def teardown_function(_):
    store.bind(None)
    store._memory.clear()
    i18n.reset()


# ------------------------------------------------------------------- settings

def test_reads_through_the_plugin_accessors():
    store.bind(FakePlugin({"theme": 1}))
    assert store.get("theme", 0) == 1
    assert store.backend_name() == "plugin"


def test_default_when_key_is_absent():
    store.bind(FakePlugin())
    assert store.get("missing", "fallback") == "fallback"


def test_none_is_treated_as_absent():
    store.bind(FakePlugin({"theme": None}))
    assert store.get("theme", 3) == 3


def test_writes_reach_the_plugin():
    plugin = FakePlugin()
    store.bind(plugin)
    assert store.set("theme", 1) is True
    assert ("theme", 1) in plugin.writes


def test_memory_fallback_keeps_the_console_working():
    # nothing bound and no client modules: reads must not raise
    store.bind(None)
    assert store.get("theme", 0) == 0
    assert store.set("theme", 1) is False
    assert store.get("theme", 0) == 1
    assert store.backend_name() == "memory"


def test_broken_backend_falls_through_instead_of_raising():
    store.bind(FakePlugin(broken=True))
    assert store.get("theme", 7) == 7
    assert store.set("theme", 1) is False


def test_all_settings_snapshot():
    store.bind(FakePlugin({"a": 1, "b": 2}))
    assert store.all_settings() == {"a": 1, "b": 2}


# -------------------------------------------------------------------- strings

def test_strings_come_from_the_bundled_json(tmp_path):
    (tmp_path / "locales").mkdir()
    (tmp_path / "locales" / "strings_en.json").write_text(
        '{"diag_item": "Diagnostics"}', encoding="utf-8"
    )
    assert i18n.get("diag_item", "fallback", root=str(tmp_path)) == "Diagnostics"


def test_missing_key_returns_the_fallback(tmp_path):
    (tmp_path / "locales").mkdir()
    (tmp_path / "locales" / "strings_en.json").write_text("{}", encoding="utf-8")
    assert i18n.get("nope", "fallback", root=str(tmp_path)) == "fallback"


def test_missing_locales_directory_is_survivable(tmp_path):
    assert i18n.get("anything", "fallback", root=str(tmp_path)) == "fallback"


def test_key_itself_is_the_last_resort(tmp_path):
    assert i18n.get("some_key", root=str(tmp_path)) == "some_key"


def test_real_locales_are_readable():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "extcli"
    assert i18n.get("diag_item", root=str(root)) == "Diagnostics"


def test_format_substitutes_placeholders(tmp_path):
    (tmp_path / "locales").mkdir()
    (tmp_path / "locales" / "strings_en.json").write_text(
        '{"greet": "extCLI v{version}"}', encoding="utf-8"
    )
    assert i18n.get("greet", root=str(tmp_path)) == "extCLI v{version}"
    assert i18n.format("greet", root=str(tmp_path), version="0.1.0") == "extCLI v0.1.0"


def test_format_leaves_the_text_alone_when_placeholders_do_not_match(tmp_path):
    (tmp_path / "locales").mkdir()
    (tmp_path / "locales" / "strings_en.json").write_text(
        '{"greet": "extCLI v{version}"}', encoding="utf-8"
    )
    assert i18n.format("greet", root=str(tmp_path), wrong="x") == "extCLI v{version}"


def test_a_cached_miss_for_one_root_does_not_answer_for_another(tmp_path):
    empty = tmp_path / "empty"
    (empty / "locales").mkdir(parents=True)
    (empty / "locales" / "strings_en.json").write_text("{}", encoding="utf-8")
    filled = tmp_path / "filled"
    (filled / "locales").mkdir(parents=True)
    (filled / "locales" / "strings_en.json").write_text(
        '{"greet": "hello"}', encoding="utf-8"
    )
    assert i18n.get("greet", "fallback", root=str(empty)) == "fallback"
    assert i18n.get("greet", "fallback", root=str(filled)) == "hello"


def test_backend_name_without_elyx():
    assert i18n.backend_name() == "bundled json"


# --------------------------------------------------------------------- plurals

def test_english_has_two_forms():
    assert i18n.plural_form(1, "en") == "one"
    assert i18n.plural_form(0, "en") == "many"
    assert i18n.plural_form(21, "en") == "many"


def test_russian_counts_by_the_last_digit():
    # 1 пакет, 2 пакета, 5 пакетов — and 11-14 are the exception to all of it
    assert i18n.plural_form(1, "ru") == "one"
    assert i18n.plural_form(21, "ru") == "one"
    assert i18n.plural_form(3, "ru") == "few"
    assert i18n.plural_form(24, "ru") == "few"
    assert i18n.plural_form(5, "ru") == "many"
    assert i18n.plural_form(11, "ru") == "many"
    assert i18n.plural_form(12, "ru") == "many"
    assert i18n.plural_form(112, "ru") == "many"
    assert i18n.plural_form(0, "ru") == "many"


def test_plural_form_survives_nonsense():
    assert i18n.plural_form(None, "en") == "many"


def test_plural_picks_the_form(tmp_path, monkeypatch):
    import json

    directory = tmp_path / "locales"
    directory.mkdir()
    (directory / "strings_ru.json").write_text(json.dumps({
        "tools_ask_install_one": "one",
        "tools_ask_install_few": "few",
        "tools_ask_install_many": "many",
    }), encoding="utf-8")
    i18n.reset()
    monkeypatch.setattr(i18n, "language", lambda: "ru")
    root = str(tmp_path)
    assert i18n.plural("tools_ask_install", 1, root=root) == "one"
    assert i18n.plural("tools_ask_install", 3, root=root) == "few"
    assert i18n.plural("tools_ask_install", 9, root=root) == "many"
    # a family with no entry at all falls back to what the caller passed
    assert i18n.plural("nothing_here", 2, "fallback", root=root) == "fallback"
    i18n.reset()


# ------------------------------------------------------------------- meta.yml

def test_meta_parses_flat_keys():
    data = meta.parse('name: extCLI\nversion: "0.1.0"\n# comment\nid: extcli\n')
    assert data["name"] == "extCLI"
    assert data["version"] == "0.1.0"
    assert data["id"] == "extcli"


def test_meta_ignores_trailing_comments():
    data = meta.parse("sourceHash: abc123 # Sha256\nbuildNum: 4\n")
    assert data["sourceHash"] == "abc123"
    assert data["buildNum"] == "4"


def test_meta_reads_the_real_file():
    from pathlib import Path

    root = str(Path(__file__).resolve().parent.parent / "extcli")
    data = meta.load(root, refresh=True)
    assert data["id"] == "extcli"
    assert data["version"]


def test_meta_missing_file_is_empty():
    assert meta.load("/definitely/not/here", refresh=True) == {}


def test_build_info_rows():
    from pathlib import Path

    root = str(Path(__file__).resolve().parent.parent / "extcli")
    meta.load(root, refresh=True)
    labels = [label for label, _ in meta.build_info()]
    assert "version" in labels
