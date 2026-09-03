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


# ------------------------------------------ the root when only src/ is on disk

def _fake_install(tmp_path, with_meta=False, with_res=False):
    """A directory shaped like this client's install: src/ present, and the
    rest (meta.yml, res/) only if the caller asks."""
    root = tmp_path / "source" / "extcli" / "extcli"
    (root / "src" / "compat").mkdir(parents=True)
    (root / "src" / "compat" / "paths.py").write_text("# me\n")
    if with_meta:
        (root / "meta.yml").write_text("id: extcli\n")
    if with_res:
        (root / "res" / "native" / "arm64-v8a").mkdir(parents=True)
    return str(root)


def test_the_root_is_found_by_its_src_tree_when_meta_is_absent(tmp_path,
                                                               monkeypatch):
    """This client extracts only the Python it imports — no meta.yml next to
    it — so the running source tree is what the root is verified by."""
    root = _fake_install(tmp_path, with_meta=False)
    monkeypatch.setattr(paths, "_root_candidates", lambda: [root])
    paths.reset_plugin_root()
    assert paths.plugin_root() == root
    # verified, so it is cached
    assert paths._plugin_root == root


def test_meta_yml_still_wins_when_two_candidates_match(tmp_path, monkeypatch):
    """A candidate carrying the metadata is preferred over one known only by
    its src/, so a client that does lay the archive out resolves to it."""
    src_only = _fake_install(tmp_path, with_meta=False)
    full = _fake_install(tmp_path / "other", with_meta=True)
    monkeypatch.setattr(paths, "_root_candidates", lambda: [src_only, full])
    paths.reset_plugin_root()
    assert paths.plugin_root() == full


# ---------------------------------------------------- res/ served by the SDK

def test_res_is_the_obvious_place_when_the_assets_are_there(tmp_path,
                                                            monkeypatch):
    root = _fake_install(tmp_path, with_res=True)
    monkeypatch.setattr(paths, "_root_candidates", lambda: [root])
    paths.reset_plugin_root()
    assert paths.res_dir() == os.path.join(root, "res")


def test_res_is_resolved_through_the_sdk_when_the_obvious_place_is_empty(
        tmp_path, monkeypatch):
    """The real device case: src/ is on disk but res/ is not next to it. The
    SDK is asked, and answers with a file whose path we climb back from."""
    root = _fake_install(tmp_path, with_res=False)
    monkeypatch.setattr(paths, "_root_candidates", lambda: [root])
    paths.reset_plugin_root()

    # where the client actually put the assets, nothing like plugin_root/res
    real_res = tmp_path / "somewhere" / "assets"
    (real_res / "config").mkdir(parents=True)
    (real_res / "config" / "fastfetch.jsonc").write_text("{}")
    (real_res / "native").mkdir()

    anchor = str(real_res / "config" / "fastfetch.jsonc")
    monkeypatch.setattr(paths, "_asset_file",
                        lambda rel: anchor if rel == paths._RES_ANCHOR else None)
    assert paths.res_dir() == str(real_res)


def test_a_resolved_res_is_cached_but_a_missing_one_is_not(tmp_path,
                                                           monkeypatch):
    root = _fake_install(tmp_path, with_res=False)
    monkeypatch.setattr(paths, "_root_candidates", lambda: [root])
    monkeypatch.setattr(paths, "_asset_file", lambda rel: None)
    paths.reset_plugin_root()
    # nothing found: returns the guess and does NOT cache, so a later call
    # (after the client finishes materialising assets) gets to try again
    assert paths.res_dir() == os.path.join(root, "res")
    assert paths._res_dir is None


def test_has_assets_recognises_a_real_res_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not paths._has_assets(str(empty))
    (empty / "native").mkdir()
    assert paths._has_assets(str(empty))


def test_the_sdk_derivation_strips_the_anchor_off_the_real_path(monkeypatch):
    monkeypatch.setattr(paths, "_asset_file",
                        lambda rel: "/data/x/assets/config/fastfetch.jsonc")
    monkeypatch.setattr(paths, "real", lambda p: p)
    assert paths._res_via_sdk() == "/data/x/assets"
