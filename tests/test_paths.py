# SPDX-License-Identifier: Apache-2.0

"""Locating the plugin's own directory.

On device this failed with `NameError: name '__file__' is not defined`: the
client's loader execs plugin modules without setting `__file__`, so deriving the
install path from it worked everywhere except where it mattered. Everything
downstream depended on it — the dex renderer, the locales, meta.yml — so the
console came up broken and diagnostics could not even report why.
"""

import os

from extcli_src.compat import paths

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED = os.path.join(REPO_ROOT, "extcli")


def setup_function(_):
    paths.reset_plugin_root()


def teardown_function(_):
    paths.reset_plugin_root()


def test_finds_the_directory_that_holds_meta_yml():
    root = paths.plugin_root()
    assert root == EXPECTED
    assert os.path.isfile(os.path.join(root, "meta.yml"))


def test_works_without_dunder_file(monkeypatch):
    # exactly the device condition
    monkeypatch.delattr(paths, "__file__", raising=False)
    monkeypatch.setattr(paths, "__spec__", None, raising=False)
    paths.reset_plugin_root()
    assert paths.plugin_root() == EXPECTED


def test_this_file_falls_back_to_the_frame(monkeypatch):
    monkeypatch.delattr(paths, "__file__", raising=False)
    monkeypatch.setattr(paths, "__spec__", None, raising=False)
    source = paths._this_file()
    assert source is not None
    assert source.endswith(os.path.join("compat", "paths.py"))


def test_result_is_cached():
    first = paths.plugin_root()
    calls = []
    original = paths._root_candidates

    def counting():
        calls.append(True)
        return original()

    paths._root_candidates = counting
    try:
        assert paths.plugin_root() == first
        assert not calls, "a resolved root must not be searched for again"
    finally:
        paths._root_candidates = original


def test_unverified_guess_is_not_cached(monkeypatch):
    monkeypatch.setattr(paths, "_root_candidates", lambda: ["/nowhere/at/all"])
    paths.reset_plugin_root()
    assert paths.plugin_root() == "/nowhere/at/all"
    # nothing was verified, so a later call must search again
    assert paths._plugin_root is None


def test_no_candidates_at_all_does_not_raise(monkeypatch):
    monkeypatch.setattr(paths, "_root_candidates", lambda: [])
    monkeypatch.setattr(paths, "files_dir", lambda: "/data/files")
    paths.reset_plugin_root()
    assert paths.plugin_root() == "/data/files"


def test_derived_directories_hang_off_the_root():
    root = paths.plugin_root()
    assert paths.res_dir() == os.path.join(root, "res")
    assert paths.dex_dir() == os.path.join(root, "dex")


def test_the_renderer_dex_is_where_the_bridge_looks():
    from extcli_src.term import bridge

    assert bridge.dex_path() == os.path.join(EXPECTED, "dex", "terminal.dex")
    assert os.path.isfile(bridge.dex_path()), "terminal.dex must ship in the repo"


def test_locales_and_meta_resolve_from_the_root():
    from extcli_src.compat import i18n, meta

    assert i18n.locales_dir() == os.path.join(EXPECTED, "locales")
    assert meta.meta_path() == os.path.join(EXPECTED, "meta.yml")
    assert meta.load(refresh=True)["id"] == "extcli"


def test_candidates_include_the_installed_layouts(monkeypatch):
    monkeypatch.setattr(paths, "files_dir", lambda: "/data/user/0/pkg/files")
    candidates = paths._root_candidates()
    joined = " ".join(candidates)
    # the layout a real install uses: files/plugins/ElyxPlugins/<id>/<dir>
    assert os.path.join("plugins", "ElyxPlugins", "extcli", "extcli") in joined
    assert os.path.join("plugins", "extcli", "extcli") in joined
