# SPDX-License-Identifier: Apache-2.0

"""Patching the client rather than a plugin.

The client is Java, and Java cannot be edited on a phone and put back — that
would mean repacking and re-signing the APK, which means a client that has to
be reinstalled, loses its data directory and stops updating. So a patch of the
client is a *plugin* that hooks it as it loads, and the workspace holds hooks
rather than edited code.

An APK is a zip with `.dex` files in it, so the tests build one out of the
real dex in this repository. That makes the whole path — index, search,
listing, skeleton, archive — testable with no phone and no 160 MB download,
while still running against bytecode a compiler actually produced.
"""

import os
import zipfile
from pathlib import Path

import pytest

from extcli_src.patch import client as client_module
from extcli_src.patch import hooks as hooks_module
from extcli_src.patch import store

DEX = Path(__file__).resolve().parent.parent / "extcli" / "dex" / "terminal.dex"

NATIVE = "dev.vcvkk.extcli.terminal.TerminalNative"


@pytest.fixture()
def apk(tmp_path):
    """An APK with two dex files in it, both real."""
    path = tmp_path / "client.apk"
    body = DEX.read_bytes()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("classes.dex", body)
        archive.writestr("classes2.dex", body)
        archive.writestr("AndroidManifest.xml", b"\x00\x00")
        archive.writestr("res/drawable/x.png", b"\x89PNG")
    return client_module.Client(str(path))


@pytest.fixture()
def opened(tmp_path, apk):
    """A client workspace, laid out."""
    work_root = str(tmp_path / "patch")
    state_root = str(tmp_path / "state")
    os.makedirs(work_root, exist_ok=True)
    os.makedirs(state_root, exist_ok=True)
    ok, work = store.create_client(work_root, state_root, "client", apk)
    assert ok, work
    return work_root, state_root, work


# --------------------------------------------------------------- the client

def test_the_dex_files_are_found_and_put_in_the_order_they_load(apk):
    assert apk.dex_names() == ["classes.dex", "classes2.dex"]


def test_the_dex_files_sort_by_number_and_not_by_spelling(tmp_path):
    """`classes10.dex` comes after `classes2.dex`, not before it."""
    path = tmp_path / "many.apk"
    body = DEX.read_bytes()
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("classes.dex", "classes10.dex", "classes2.dex"):
            archive.writestr(name, body)
    assert client_module.Client(str(path)).dex_names() == [
        "classes.dex", "classes2.dex", "classes10.dex"]


def test_an_apk_that_is_not_there_says_so(tmp_path):
    missing = client_module.Client(str(tmp_path / "nothing.apk"))
    assert not missing.exists()
    assert missing.dex_names() == []


def test_the_index_covers_every_dex(apk):
    entries = apk.index()
    names = {name for name, _dex in entries}
    dexes = {dex for _name, dex in entries}
    assert NATIVE in names
    assert dexes == {"classes.dex", "classes2.dex"}
    # 54 classes, in both files
    assert len(entries) == 108


def test_the_index_is_sorted_so_two_runs_are_the_same_list(apk):
    assert apk.index() == sorted(apk.index())


def test_which_dex_holds_a_class(apk):
    assert apk.where(NATIVE) == "classes.dex"
    assert apk.where("Ldev/vcvkk/extcli/terminal/TerminalNative;") == \
        "classes.dex"
    assert apk.where("org.telegram.NotHere") is None


def test_searching_reaches_classes_methods_and_strings(apk):
    assert apk.search("TerminalNative", kind="classes")
    assert apk.search("append", kind="methods")
    assert apk.search("TerminalView", kind="strings")
    assert apk.search("nothing like this", kind="classes") == []


def test_a_search_stops_at_the_limit(apk):
    """Two dex files with the same content, so without a limit it would go on
    finding the same thing twice."""
    assert len(apk.search("Terminal", kind="classes", limit=3)) == 3


def test_progress_is_reported_because_this_takes_a_while(apk):
    said = []
    apk.index(on_progress=said.append)
    assert said and any("classes.dex" in one for one in said)


def test_a_progress_callback_that_throws_does_not_stop_the_work(apk):
    def bad(_text):
        raise RuntimeError("no")

    assert len(apk.index(on_progress=bad)) == 108


# ------------------------------------------------------------ the workspace

def test_what_a_client_workspace_holds(opened):
    _work_root, _state_root, work = opened
    assert os.path.isfile(os.path.join(work, client_module.INDEX))
    assert os.path.isfile(os.path.join(work, client_module.NOTES))
    assert os.path.isdir(os.path.join(work, client_module.HOOKS))
    assert os.path.isdir(os.path.join(work, client_module.LISTINGS))


def test_the_client_code_is_not_copied_into_the_workspace(opened):
    """It is sixty megabytes and it is already on the phone."""
    _work_root, _state_root, work = opened
    for base, _dirs, files in os.walk(work):
        for name in files:
            assert not name.endswith(".dex"), os.path.join(base, name)
            assert not name.endswith(".apk"), os.path.join(base, name)


def test_the_workspace_knows_it_is_the_client(opened):
    _work_root, state_root, _work = opened
    assert store.kind(state_root, "client") == store.CLIENT
    assert store.note(state_root, "client")["source"].endswith(".apk")


def test_a_plugin_workspace_is_not_a_client_one(tmp_path):
    from test_patch import plugin_tree, roots

    work_root, state_root = roots(tmp_path)
    store.create(work_root, state_root, "p", plugin_tree(tmp_path / "s"))
    assert store.kind(state_root, "p") == store.PLUGIN


def test_the_index_can_be_read_back(opened):
    _work_root, _state_root, work = opened
    entries = client_module.read_index(work)
    assert len(entries) == 108
    assert (NATIVE, "classes.dex") in entries


def test_a_fresh_client_workspace_has_changed_nothing(opened):
    work_root, state_root, _work = opened
    assert store.changes(work_root, state_root, "client").empty()


def test_a_hook_written_into_it_is_a_change(opened):
    work_root, state_root, work = opened
    with open(os.path.join(work, "hooks", "mine.py"), "w") as handle:
        handle.write("def apply(plugin):\n    pass\n")
    changed = store.changes(work_root, state_root, "client")
    assert [entry.path for entry in changed] == ["hooks/mine.py"]


def test_the_notes_say_the_apk_is_never_rewritten(opened):
    _work_root, _state_root, work = opened
    text = open(os.path.join(work, client_module.NOTES)).read()
    assert "never rewritten" in text
    # and that editing the listings does nothing, which is the thing somebody
    # would otherwise spend an evening finding out
    assert "cannot be assembled back" in text


def test_the_notes_say_to_search_for_the_key_and_not_the_words(opened):
    """The client's visible text is in its resources and is translated; the
    code holds only the name it looks the text up by."""
    _work_root, _state_root, work = opened
    text = open(os.path.join(work, client_module.NOTES)).read()
    assert "DeleteForAll" in text


# ------------------------------------------------------------- the skeleton

def test_a_module_name_is_one_python_will_import():
    assert hooks_module.module_name("ChatActivity onResume") == \
        "chatactivity_onresume"
    assert hooks_module.module_name("9lives") == "hook_9lives"
    assert hooks_module.module_name("!!!") == "hook"
    assert hooks_module.module_name("a" * 90) == "a" * 48


def test_a_skeleton_is_valid_python(apk):
    """A generated file that does not run is worse than no generator."""
    one = apk.dex("classes.dex")
    method = hooks_module.method_of(one, NATIVE, "append")
    text = hooks_module.skeleton(NATIVE, method, "classes.dex")
    compile(text, "hook.py", "exec")


def test_a_skeleton_carries_the_types_read_out_of_the_dex(apk):
    """The part nobody gets right from memory: an overloaded method needs the
    right one named, and `Landroid/view/View;` is not something anybody
    recalls."""
    one = apk.dex("classes.dex")
    method = hooks_module.method_of(one, NATIVE, "append")
    text = hooks_module.skeleton(NATIVE, method, "classes.dex")
    assert 'TYPES = ("Landroid/view/View;", "Ljava/lang/String;")' in text
    assert 'CLASS = "%s"' % NATIVE in text
    assert 'METHOD = "append"' in text


def test_a_method_with_one_parameter_gets_a_tuple_and_not_a_string():
    """`("Ljava/lang/String;")` is a string, and iterating it would compare
    the method's parameters against single characters."""
    assert hooks_module._types_literal(["Ljava/lang/String;"]) == \
        '("Ljava/lang/String;",)'
    assert hooks_module._types_literal([]) == "()"


def test_the_overload_with_the_most_parameters_is_the_one_meant(apk):
    """The short ones are almost always convenience wrappers around it."""
    one = apk.dex("classes.dex")
    method = hooks_module.method_of(one, NATIVE, "append")
    assert len(method.parameters) == 2


def test_asking_for_a_method_that_is_not_there_says_what_is(apk):
    from extcli_src.patch import dex as dex_module

    one = apk.dex("classes.dex")
    with pytest.raises(dex_module.DexError) as caught:
        hooks_module.method_of(one, NATIVE, "noSuchMethod")
    assert "append" in str(caught.value)


def test_the_shipped_helper_and_example_are_valid_python():
    compile(hooks_module.api(), "_api.py", "exec")
    compile(hooks_module.example(), "example.py", "exec")


def test_a_fresh_workspace_comes_with_a_hook_that_works(opened):
    """The shape of a hook is three things nobody guesses on the first try."""
    _work_root, _state_root, work = opened
    for name in ("_api.py", "example.py"):
        path = os.path.join(work, "hooks", name)
        assert os.path.isfile(path)
        compile(open(path).read(), name, "exec")


def test_the_helper_is_not_mistaken_for_a_hook(opened):
    """`_api.py` is imported by the hooks, not applied as one."""
    _work_root, _state_root, work = opened
    assert [name for name, _path in hooks_module.hook_files(work)] == \
        ["example"]


# ---------------------------------------------------------------- the build

def built(work, tmp_path, mark="62Yg28"):
    target = str(tmp_path / "patch.eaf")
    ok, detail = hooks_module.build(work, target, mark, source="client.apk",
                                    when=1_700_000_000)
    assert ok, detail
    return target, detail


def test_a_client_patch_is_an_installable_plugin(opened, tmp_path):
    from extcli_src.compat import plugins

    _work_root, _state_root, work = opened
    target, name = built(work, tmp_path)
    data = plugins.read_archive(target)
    assert data is not None
    assert data["id"] == "extcli_patch_62yg28"
    assert data["name"] == name == "extCLI patch-62Yg28"


def test_the_generated_loader_is_valid_python(opened, tmp_path):
    _work_root, _state_root, work = opened
    target, _name = built(work, tmp_path)
    with zipfile.ZipFile(target) as archive:
        compile(archive.read(hooks_module.MAIN).decode(), "BasePlugin.py",
                "exec")


def test_only_the_hooks_go_in(opened, tmp_path):
    """The index and the listings are what you read while deciding what to
    write; shipping them would be two megabytes of class names doing nothing.
    """
    _work_root, _state_root, work = opened
    target, _name = built(work, tmp_path)
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    assert client_module.INDEX not in names
    assert not any(name.endswith(".smali") for name in names)
    assert "src/hooks/example.py" in names
    assert "src/hooks/_api.py" in names


def test_the_loader_applies_every_hook_and_survives_a_broken_one(opened,
                                                                  tmp_path):
    """One hook aimed at a method a client update has moved must not take the
    other four down with it."""
    _work_root, _state_root, work = opened
    with open(os.path.join(work, "hooks", "second.py"), "w") as handle:
        handle.write("def apply(plugin):\n    pass\n")
    target, _name = built(work, tmp_path)
    with zipfile.ZipFile(target) as archive:
        text = archive.read(hooks_module.MAIN).decode()
    assert "from .hooks import example as _hook_0" in text
    assert "from .hooks import second as _hook_1" in text
    assert "except Exception as e:" in text


def test_a_workspace_with_no_hooks_is_refused(tmp_path, opened):
    _work_root, _state_root, work = opened
    os.remove(os.path.join(work, "hooks", "example.py"))
    ok, detail = hooks_module.build(work, str(tmp_path / "x.eaf"), "62Yg28")
    assert not ok and "no hooks" in detail
    assert not os.path.exists(str(tmp_path / "x.eaf"))


def test_the_description_names_the_hooks(opened, tmp_path):
    from extcli_src.compat import plugins

    _work_root, _state_root, work = opened
    target, _name = built(work, tmp_path)
    assert "example" in plugins.read_archive(target)["description"]


def test_the_archive_says_what_a_patch_of_the_client_is(opened, tmp_path):
    _work_root, _state_root, work = opened
    target, _name = built(work, tmp_path)
    with zipfile.ZipFile(target) as archive:
        text = archive.read("PATCH.md").decode()
    assert "does not change the APK" in text
    assert "example" in text


def test_an_edited_helper_is_the_one_that_ships(opened, tmp_path):
    """It is a generated file, but it is in the workspace and somebody may
    have had a reason."""
    _work_root, _state_root, work = opened
    with open(os.path.join(work, "hooks", "_api.py"), "w") as handle:
        handle.write("MINE = True\n")
    target, _name = built(work, tmp_path)
    with zipfile.ZipFile(target) as archive:
        assert archive.read("src/hooks/_api.py").decode() == "MINE = True\n"


def test_building_the_same_hooks_twice_gives_the_same_bytes(opened, tmp_path):
    _work_root, _state_root, work = opened
    one = str(tmp_path / "1.eaf")
    two = str(tmp_path / "2.eaf")
    for target in (one, two):
        assert hooks_module.build(work, target, "62Yg28", source="c.apk",
                                  when=1_700_000_000)[0]
    assert open(one, "rb").read() == open(two, "rb").read()


# ------------------------------------------------------- through the shell

@pytest.fixture()
def shell(tmp_path, monkeypatch, apk):
    from extcli_src.shell.builtins import patch as patch_cmd

    work_root = str(tmp_path / "patch")
    state_root = str(tmp_path / "state")
    os.makedirs(work_root, exist_ok=True)
    os.makedirs(state_root, exist_ok=True)
    monkeypatch.setattr(patch_cmd, "_roots", lambda: (work_root, state_root))

    from test_commands import make_ctx, run

    context = make_ctx()
    context.assume_yes = True
    return context, run, apk


def test_the_whole_way_through(shell, tmp_path):
    """Open the client, find a class, read it, start a hook, build it."""
    from extcli_src.render import plain

    context, run, apk = shell

    text = plain.text(run(context, "patch open client --apk %s" % apk.path))
    assert "opened" in text and "108" in text

    text = plain.text(run(context, "patch find TerminalNative"))
    assert NATIVE in text

    text = plain.text(run(context, "patch dis %s" % NATIVE))
    assert ".class public final Ldev/vcvkk/extcli/terminal/TerminalNative;" \
        in text
    assert "written to" in text

    text = plain.text(run(context, "patch hook %s append" % NATIVE))
    assert "started" in text
    assert "append(Landroid/view/View;Ljava/lang/String;)V" in text

    out = str(tmp_path / "built.eaf")
    text = plain.text(run(context, "patch build --out %s" % out))
    assert "extCLI patch-" in text
    with zipfile.ZipFile(out) as archive:
        assert "src/hooks/terminalnative_append.py" in archive.namelist()


def test_a_listing_lands_where_an_editor_can_reach_it(shell, tmp_path):
    from extcli_src.render import plain

    context, run, apk = shell
    run(context, "patch open client --apk %s" % apk.path)
    run(context, "patch dis %s --quiet" % NATIVE)
    written = (tmp_path / "patch" / "client" / "smali" / "dev" / "vcvkk"
               / "extcli" / "terminal" / "TerminalNative.smali")
    assert written.is_file()
    assert ".super Ljava/lang/Object;" in written.read_text()
    # --quiet means it was written and not printed
    assert "invoke-direct" not in plain.text(
        run(context, "patch dis %s --quiet" % NATIVE))


def test_one_method_can_be_asked_for_on_its_own(shell):
    from extcli_src.render import plain

    context, run, apk = shell
    run(context, "patch open client --apk %s" % apk.path)
    text = plain.text(run(context, "patch dis %s --method append" % NATIVE))
    assert "append(Landroid/view/View;Ljava/lang/String;)V" in text
    assert "<clinit>" not in text


def test_asking_about_a_class_the_client_does_not_have(shell):
    from extcli_src.render import plain

    context, run, apk = shell
    run(context, "patch open client --apk %s" % apk.path)
    text = plain.text(run(context, "patch dis org.telegram.NotHere"))
    assert "no class org.telegram.NotHere" in text
    assert "patch find NotHere" in text


def test_a_second_hook_for_the_same_method_does_not_overwrite_the_first(shell):
    from extcli_src.render import plain

    context, run, apk = shell
    run(context, "patch open client --apk %s" % apk.path)
    run(context, "patch hook %s append" % NATIVE)
    text = plain.text(run(context, "patch hook %s append" % NATIVE))
    assert "already there" in text and "--force" in text


def test_the_client_commands_refuse_a_plugin_workspace(shell, tmp_path):
    from extcli_src.render import plain
    from test_patch import plugin_tree

    context, run, _apk = shell
    from extcli_src.shell.builtins import patch as patch_cmd

    work_root, state_root = patch_cmd._roots()
    store.create(work_root, state_root, "p", plugin_tree(tmp_path / "s"))
    text = plain.text(run(context, "patch find p something"))
    assert "is a plugin workspace" in text


def test_a_client_workspace_that_has_lost_its_apk_says_so(shell, tmp_path):
    from extcli_src.render import plain

    context, run, apk = shell
    run(context, "patch open client --apk %s" % apk.path)
    os.remove(apk.path)
    text = plain.text(run(context, "patch dis %s" % NATIVE))
    assert "has gone" in text


def test_opening_the_client_with_no_apk_to_be_found(shell, monkeypatch):
    from extcli_src.render import plain
    from extcli_src.patch import client as module

    context, run, _apk = shell
    monkeypatch.setattr(module, "apk_paths", list)
    text = plain.text(run(context, "patch open client"))
    assert "cannot find the client's APK" in text
    assert "--apk" in text
