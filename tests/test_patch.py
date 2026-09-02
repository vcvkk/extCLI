# SPDX-License-Identifier: Apache-2.0

"""Taking a plugin apart and building the change back into one.

Everything below runs on two temporary directories. The workspace machinery
was written that way on purpose: what changed in a tree, what the result is
called and what goes in the archive are all questions about files, and a
question about files should not need a phone to answer.
"""

import os
import zipfile

import pytest

from extcli_src.patch import pack, store, workspace

REFMAP = """metainfo: plug/meta.yml
main: plug/src/BasePlugin.py
"""

META = """name: Sample
description: "A plugin: for testing"
id: sample
version: "1.2.0"
author: someone
"""


def plugin_tree(root):
    """A minimal but real plugin tree: refmap, metadata, a little source."""
    root = str(root)
    os.makedirs(os.path.join(root, "plug", "src"), exist_ok=True)
    _write(os.path.join(root, "refmap.yml"), REFMAP)
    _write(os.path.join(root, "plug", "meta.yml"), META)
    _write(os.path.join(root, "plug", "src", "BasePlugin.py"),
           "class Plugin:\n    def on_load(self):\n        pass\n")
    _write(os.path.join(root, "plug", "src", "helper.py"),
           "\n".join("line %d" % n for n in range(1, 11)) + "\n")
    return root


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


# --------------------------------------------------------------- what moved

def test_an_untouched_tree_has_nothing_to_say(tmp_path):
    plugin_tree(tmp_path / "a")
    plugin_tree(tmp_path / "b")
    changed = workspace.compare(tmp_path / "a", tmp_path / "b")
    assert changed.empty()
    assert changed.sentence() == "nothing changed"


def test_added_removed_and_changed_are_told_apart(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    _write(os.path.join(work, "plug", "src", "extra.py"), "one\ntwo\n")
    os.remove(os.path.join(work, "plug", "src", "helper.py"))
    _write(os.path.join(work, "plug", "src", "BasePlugin.py"),
           "class Plugin:\n    def on_load(self):\n        print('hi')\n")

    changed = workspace.compare(origin, work)
    kinds = {entry.path: entry.kind for entry in changed}
    assert kinds["plug/src/extra.py"] == workspace.ADDED
    assert kinds["plug/src/helper.py"] == workspace.REMOVED
    assert kinds["plug/src/BasePlugin.py"] == workspace.MODIFIED


def test_the_counts_are_lines_and_not_files(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    _write(os.path.join(work, "plug", "src", "extra.py"), "one\ntwo\nthree\n")
    os.remove(os.path.join(work, "plug", "src", "helper.py"))

    changed = workspace.compare(origin, work)
    assert changed.plus() == 3      # the three new lines
    assert changed.minus() == 10    # the ten that went with helper.py


def test_one_line_edited_is_one_line_out_and_one_in(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    body = open(os.path.join(work, "plug", "src", "helper.py")).read()
    _write(os.path.join(work, "plug", "src", "helper.py"),
           body.replace("line 5", "line five"))

    changed = workspace.compare(origin, work)
    assert len(changed) == 1
    assert (changed.entries[0].plus, changed.entries[0].minus) == (1, 1)


def test_bytecode_and_git_are_not_changes(tmp_path):
    """They are made from what is next to them, or belong to something else."""
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    _write(os.path.join(work, "plug", "src", "__pycache__", "helper.pyc"), "x")
    _write(os.path.join(work, ".git", "HEAD"), "ref: refs/heads/main\n")
    # helper.py is right there, so this is made from it and says nothing new
    _write(os.path.join(work, "plug", "src", "helper.pyc"), "x")

    assert workspace.compare(origin, work).empty()


def test_a_compiled_file_with_no_source_is_the_plugin_itself(tmp_path):
    """Most published plugins ship exactly like that, and skipping those would
    make their workspace look empty and drop every change made to it."""
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    _write(os.path.join(work, "plug", "src", "secret.pyc"), "x")

    changed = workspace.compare(origin, work)
    assert [entry.path for entry in changed] == ["plug/src/secret.pyc"]


def test_a_binary_file_is_named_but_not_counted(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    with open(os.path.join(work, "plug", "icon.png"), "wb") as handle:
        handle.write(b"\x89PNG\x00\x01\x02")

    changed = workspace.compare(origin, work)
    assert len(changed) == 1
    assert changed.entries[0].binary is True
    assert changed.entries[0].counts() == "binary"
    assert changed.plus() == 0


def test_a_file_whose_bytes_moved_but_whose_lines_did_not(tmp_path):
    """A missing final newline is a real change, and not one worth a count."""
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    path = os.path.join(work, "plug", "src", "helper.py")
    _write(path, open(path).read().rstrip("\n"))

    changed = workspace.compare(origin, work)
    assert len(changed) == 1
    assert (changed.entries[0].plus, changed.entries[0].minus) == (0, 0)


def test_the_diff_of_one_file_reads_as_a_diff(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    path = os.path.join(work, "plug", "src", "helper.py")
    _write(path, open(path).read().replace("line 3", "line three"))

    lines = workspace.unified(origin, work, "plug/src/helper.py")
    assert any(line == "-line 3" for line in lines)
    assert any(line == "+line three" for line in lines)


# ------------------------------------------------------------------- naming

def test_a_mark_is_short_and_unmistakable():
    """These get read off a screen and typed back in."""
    mark = workspace.token(seed=1)
    assert len(mark) == workspace.TOKEN_LENGTH
    for char in "0O1lI":
        assert char not in workspace.ALPHABET


def test_two_builds_are_two_different_plugins():
    marks = {workspace.token() for _ in range(50)}
    assert len(marks) > 45


def test_the_name_says_what_it_is_and_the_id_can_be_stored():
    assert workspace.plugin_name("62Yg28") == "extCLI patch-62Yg28"
    identifier = workspace.plugin_id("62Yg28")
    assert identifier == "extcli_patch_62yg28"
    assert identifier == identifier.lower()
    assert " " not in identifier


def test_a_workspace_name_is_safe_to_put_in_a_path():
    assert workspace.workspace_name("../../etc") == "etc"
    assert workspace.workspace_name("my plugin!") == "my-plugin"
    assert workspace.workspace_name("") == "patch"
    assert "/" not in workspace.workspace_name("a/b/c")


# -------------------------------------------------------------- what it says

def test_the_description_fits_on_one_line(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    for index in range(40):
        _write(os.path.join(work, "plug", "src", "f%d.py" % index), "x\n")

    changed = workspace.compare(origin, work)
    text = workspace.description("Sample", "1.2.0", changed, limit=180)
    assert len(text) <= 180
    assert "\n" not in text
    assert "Sample" in text and "1.2.0" in text


def test_the_description_names_files_when_there_is_room(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    _write(os.path.join(work, "plug", "src", "extra.py"), "one\n")

    text = workspace.description("Sample", "1.2.0",
                                 workspace.compare(origin, work))
    assert "A plug/src/extra.py" in text


def test_the_report_says_where_it_came_from(tmp_path):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    _write(os.path.join(work, "plug", "src", "extra.py"), "one\n")

    lines = workspace.report("Sample", "1.2.0",
                             workspace.compare(origin, work),
                             "extCLI patch-62Yg28")
    text = "\n".join(lines)
    assert "extCLI patch-62Yg28" in text
    assert "Sample 1.2.0" in text
    assert "plug/src/extra.py" in text


# ------------------------------------------------------------- the metadata

def test_metadata_is_found_through_the_refmap(tmp_path):
    root = plugin_tree(tmp_path / "a")
    relative, data = pack.metadata(root)
    assert relative == "plug/meta.yml"
    assert data["id"] == "sample" and data["version"] == "1.2.0"


def test_a_tree_that_is_not_a_plugin_says_so(tmp_path):
    (tmp_path / "empty").mkdir()
    assert pack.metadata(tmp_path / "empty") == (None, {})


def test_metadata_survives_being_written_out_and_read_again(tmp_path):
    _relative, data = pack.metadata(plugin_tree(tmp_path / "a"))
    from extcli_src.compat import meta

    assert meta.parse(pack.render(data)) == data


def test_a_value_with_a_colon_in_it_is_quoted():
    """Unquoted it would be read back as another key."""
    from extcli_src.compat import meta

    text = pack.render({"description": "A plugin: for testing"})
    assert meta.parse(text)["description"] == "A plugin: for testing"


def test_the_version_says_it_is_off_to_one_side():
    """A patch of 1.2.0 is not further along than 1.2.0; it is beside it."""
    assert pack.version_of({"version": "1.2.0"}, "62Yg28") == "1.2.0+patch.62Yg28"
    assert pack.version_of({}, "62Yg28") == "0+patch.62Yg28"


def test_only_four_things_about_the_plugin_change(tmp_path):
    _relative, data = pack.metadata(plugin_tree(tmp_path / "a"))
    named = pack.fields(data, "62Yg28", workspace.Changes())
    assert set(named) == set(pack.OWNED)
    for key in ("author", "name"):
        assert key in data
    assert named["id"] != data["id"]


# ---------------------------------------------------------------- the build

def built(tmp_path, edit=True):
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    if edit:
        _write(os.path.join(work, "plug", "src", "extra.py"), "one\ntwo\n")
    changed = workspace.compare(origin, work)
    target = str(tmp_path / "out.eaf")
    ok, detail = pack.build(work, target, "62Yg28", changed, source="Sample")
    assert ok, detail
    return target, changed


def test_what_comes_out_is_a_plugin_archive(tmp_path):
    target, _changed = built(tmp_path)
    from extcli_src.compat import plugins

    data = plugins.read_archive(target)
    assert data is not None
    assert data["id"] == "extcli_patch_62yg28"
    assert data["name"] == "extCLI patch-62Yg28"
    assert data["version"] == "1.2.0+patch.62Yg28"


def test_the_original_is_not_what_gets_replaced(tmp_path):
    """The point of a patch being a separate plugin is that turning it off
    puts the phone back where it was."""
    target, _changed = built(tmp_path)
    from extcli_src.compat import plugins

    assert plugins.read_archive(target)["id"] != "sample"


def test_the_edit_is_in_the_archive(tmp_path):
    target, _changed = built(tmp_path)
    with zipfile.ZipFile(target) as archive:
        assert archive.read("plug/src/extra.py").decode() == "one\ntwo\n"


def test_the_archive_carries_its_own_report(tmp_path):
    target, _changed = built(tmp_path)
    with zipfile.ZipFile(target) as archive:
        text = archive.read(pack.REPORT_NAME).decode()
    assert "extCLI patch-62Yg28" in text
    assert "plug/src/extra.py" in text


def test_building_the_same_workspace_twice_gives_the_same_bytes(tmp_path):
    """So that "is the file in the chat the one I built" has an answer."""
    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    _write(os.path.join(work, "plug", "src", "extra.py"), "one\n")
    changed = workspace.compare(origin, work)
    when = 1_700_000_000
    one, two = str(tmp_path / "1.eaf"), str(tmp_path / "2.eaf")
    assert pack.build(work, one, "62Yg28", changed, when=when)[0]
    assert pack.build(work, two, "62Yg28", changed, when=when)[0]
    assert open(one, "rb").read() == open(two, "rb").read()


def test_a_tree_that_is_not_a_plugin_is_refused_rather_than_zipped(tmp_path):
    (tmp_path / "empty").mkdir()
    ok, detail = pack.build(tmp_path / "empty", str(tmp_path / "out.eaf"),
                            "62Yg28", workspace.Changes())
    assert not ok and "refmap.yml" in detail
    assert not os.path.exists(str(tmp_path / "out.eaf"))


def test_a_zip_cannot_write_outside_the_workspace(tmp_path):
    """The one time an archive names ../ it will not be by accident."""
    bad = str(tmp_path / "bad.eaf")
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../escaped.txt", "no")
    ok, detail = pack.unpack(bad, tmp_path / "into")
    assert not ok and "escapes" in detail
    assert not os.path.exists(str(tmp_path / "escaped.txt"))


def test_an_archive_makes_a_workspace_and_the_workspace_makes_it_again(tmp_path):
    work = plugin_tree(tmp_path / "b")
    first = str(tmp_path / "first.eaf")
    assert pack.build(work, first, "62Yg28", workspace.Changes())[0]

    ok, detail = pack.unpack(first, tmp_path / "again")
    assert ok, detail
    relative, data = pack.metadata(tmp_path / "again")
    assert relative == "plug/meta.yml"
    assert data["id"] == "extcli_patch_62yg28"


# ------------------------------------------------------------ the workspace

def roots(tmp_path):
    work_root = str(tmp_path / "patch")
    state_root = str(tmp_path / "state")
    os.makedirs(work_root, exist_ok=True)
    os.makedirs(state_root, exist_ok=True)
    return work_root, state_root


def test_opening_a_workspace_keeps_a_copy_nothing_can_reach(tmp_path):
    source = plugin_tree(tmp_path / "source")
    work_root, state_root = roots(tmp_path)
    ok, detail = store.create(work_root, state_root, "sample", source)
    assert ok, detail

    assert os.path.isfile(os.path.join(detail, "refmap.yml"))
    assert store.openable(state_root, "sample")
    # the copy is not under /patch, so nothing in the container can touch it
    assert not store.origin_dir(state_root, "sample").startswith(work_root)


def test_a_fresh_workspace_has_changed_nothing(tmp_path):
    source = plugin_tree(tmp_path / "source")
    work_root, state_root = roots(tmp_path)
    store.create(work_root, state_root, "sample", source)
    assert store.changes(work_root, state_root, "sample").empty()


def test_an_edit_in_the_workspace_is_seen(tmp_path):
    source = plugin_tree(tmp_path / "source")
    work_root, state_root = roots(tmp_path)
    store.create(work_root, state_root, "sample", source)
    _write(os.path.join(store.work_dir(work_root, "sample"), "plug", "src",
                        "extra.py"), "one\n")
    changed = store.changes(work_root, state_root, "sample")
    assert [entry.path for entry in changed] == ["plug/src/extra.py"]


def test_a_workspace_can_be_opened_from_an_archive(tmp_path):
    work = plugin_tree(tmp_path / "b")
    archive = str(tmp_path / "plugin.eaf")
    assert pack.build(work, archive, "62Yg28", workspace.Changes())[0]
    work_root, state_root = roots(tmp_path)
    ok, detail = store.create(work_root, state_root, "sample", archive)
    assert ok, detail
    assert os.path.isfile(os.path.join(detail, "plug", "meta.yml"))


def test_unfinished_work_is_not_thrown_away_without_being_asked(tmp_path):
    source = plugin_tree(tmp_path / "source")
    work_root, state_root = roots(tmp_path)
    store.create(work_root, state_root, "sample", source)
    _write(os.path.join(store.work_dir(work_root, "sample"), "mine.py"), "x\n")

    ok, detail = store.create(work_root, state_root, "sample", source)
    assert not ok and "already" in detail
    assert os.path.isfile(os.path.join(store.work_dir(work_root, "sample"),
                                       "mine.py"))

    ok, _detail = store.create(work_root, state_root, "sample", source,
                               replace=True)
    assert ok
    assert not os.path.exists(os.path.join(store.work_dir(work_root, "sample"),
                                           "mine.py"))


def test_a_source_that_is_not_there_is_refused(tmp_path):
    work_root, state_root = roots(tmp_path)
    ok, detail = store.create(work_root, state_root, "sample",
                              str(tmp_path / "nothing.eaf"))
    assert not ok and "no such plugin file" in detail
    assert not os.path.isdir(store.work_dir(work_root, "sample"))


def test_dropping_takes_both_copies(tmp_path):
    source = plugin_tree(tmp_path / "source")
    work_root, state_root = roots(tmp_path)
    store.create(work_root, state_root, "sample", source)
    ok, _detail = store.drop(work_root, state_root, "sample")
    assert ok
    assert not os.path.isdir(store.work_dir(work_root, "sample"))
    assert not store.openable(state_root, "sample")
    assert store.names(work_root) == []


def test_where_a_workspace_came_from_is_remembered(tmp_path):
    source = plugin_tree(tmp_path / "source")
    work_root, state_root = roots(tmp_path)
    store.create(work_root, state_root, "sample", source,
                 label="Sample", version="1.2.0")
    note = store.note(state_root, "sample")
    assert note["label"] == "Sample" and note["version"] == "1.2.0"
    assert note["source"] == source


def test_a_workspace_whose_copy_is_gone_says_so_rather_than_lying(tmp_path):
    """Everything would otherwise look new, which is worse than no answer."""
    import shutil

    source = plugin_tree(tmp_path / "source")
    work_root, state_root = roots(tmp_path)
    store.create(work_root, state_root, "sample", source)
    shutil.rmtree(store.state_dir(state_root, "sample"))
    assert not store.openable(state_root, "sample")
    assert store.note(state_root, "sample") == {}


# ---------------------------------------------------------- compiled files

SOURCE = '''
LIMIT = 40
ENDPOINT = "https://example.invalid/api"


def fetch(session):
    """Docstring."""
    if LIMIT > 10:
        return session.get(ENDPOINT, timeout=5)
    return None
'''


def compiled(tmp_path, name="mod.pyc", text=SOURCE):
    """A real .pyc, made the way the interpreter makes one."""
    import importlib.util
    import marshal

    code = compile(text, "mod.py", "exec")
    path = str(tmp_path / name)
    with open(path, "wb") as handle:
        handle.write(importlib.util.MAGIC_NUMBER)
        handle.write(b"\x00" * 12)
        handle.write(marshal.dumps(code))
    return path


def test_a_compiled_file_can_be_read(tmp_path):
    from extcli_src.patch import bytecode

    path = compiled(tmp_path)
    ok, why = bytecode.readable(path)
    assert ok, why
    _header, code = bytecode.load(path)
    assert "https://example.invalid/api" in bytecode.strings(code)


def test_strings_are_found_inside_functions_too(tmp_path):
    """A string in a function is a constant of that function, not the module."""
    from extcli_src.patch import bytecode

    _header, code = bytecode.load(
        compiled(tmp_path, text='def f():\n    return "buried"\n'))
    assert "buried" in bytecode.strings(code)


def test_what_it_calls_is_visible(tmp_path):
    from extcli_src.patch import bytecode

    _header, code = bytecode.load(compiled(tmp_path))
    assert "get" in bytecode.names(code)


def test_the_disassembly_is_the_real_thing(tmp_path):
    from extcli_src.patch import bytecode

    _header, code = bytecode.load(compiled(tmp_path))
    text = "\n".join(bytecode.listing(code))
    assert "RETURN_VALUE" in text


def test_a_pyc_from_another_python_is_refused_rather_than_misread(tmp_path):
    """marshal would not fail on it so much as produce nonsense."""
    from extcli_src.patch import bytecode

    path = str(tmp_path / "old.pyc")
    with open(path, "wb") as handle:
        handle.write(b"\x42\x0d\x0d\x0a" + b"\x00" * 12 + b"rubbish")
    ok, why = bytecode.readable(path)
    assert not ok and "another Python" in why


def test_a_constant_can_be_swapped_and_the_file_still_runs(tmp_path):
    from extcli_src.patch import bytecode

    path = compiled(tmp_path)
    count, detail = bytecode.rewrite(path, "https://example.invalid/api",
                                     "https://elsewhere.invalid/v2")
    assert count == 1, detail

    _header, code = bytecode.load(path)
    scope = {}
    exec(code, scope)
    assert scope["ENDPOINT"] == "https://elsewhere.invalid/v2"
    assert scope["LIMIT"] == 40


def test_swapping_a_constant_leaves_the_instructions_alone(tmp_path):
    """This is the whole reason it is safe and decompiling is not."""
    from extcli_src.patch import bytecode

    path = compiled(tmp_path)
    _header, before = bytecode.load(path)
    bytecode.rewrite(path, "https://example.invalid/api", "https://x.invalid")
    _header, after = bytecode.load(path)
    assert before.co_code == after.co_code
    assert before.co_names == after.co_names
    assert list(before.co_lines()) == list(after.co_lines())


def test_a_constant_that_is_not_there_changes_nothing(tmp_path):
    """A file rewritten to what it already was would still show as changed."""
    from extcli_src.patch import bytecode

    path = compiled(tmp_path)
    before = open(path, "rb").read()
    count, detail = bytecode.rewrite(path, "not in the file", "x")
    assert count == 0 and "not a constant" in detail
    assert open(path, "rb").read() == before


def test_a_number_is_not_a_boolean(tmp_path):
    """`1 == True` in Python, and a plain == here would rewrite the wrong one
    in a way that works in testing and goes wrong months later."""
    from extcli_src.patch import bytecode

    _header, code = bytecode.load(
        compiled(tmp_path, text="A = True\nB = 1\n"))
    changed, count = bytecode.replace(code, 1, 7)
    assert count == 1
    scope = {}
    exec(changed, scope)
    assert scope["A"] is True and scope["B"] == 7


def test_a_workspace_says_which_of_its_files_are_compiled(tmp_path):
    from extcli_src.patch import bytecode

    root = plugin_tree(tmp_path / "a")
    compiled(tmp_path / "a", name=os.path.join("plug", "src", "thing.pyc"))
    assert bytecode.compiled_files(root) == ["plug/src/thing.pyc"]


def test_a_rewritten_pyc_shows_up_as_a_change(tmp_path):
    """Which is what puts it in the built patch."""
    from extcli_src.patch import bytecode

    origin = plugin_tree(tmp_path / "a")
    work = plugin_tree(tmp_path / "b")
    for root in (origin, work):
        compiled(tmp_path / os.path.basename(root),
                 name=os.path.join("plug", "src", "thing.pyc"))
    assert workspace.compare(origin, work).empty()

    bytecode.rewrite(os.path.join(work, "plug", "src", "thing.pyc"),
                     "https://example.invalid/api", "https://x.invalid")
    changed = workspace.compare(origin, work)
    assert [entry.path for entry in changed] == ["plug/src/thing.pyc"]
    assert changed.entries[0].binary is True


# ------------------------------------------------------------- the commands

def test_the_command_is_registered():
    from extcli_src.shell.builtins import build_registry

    group = build_registry().get("patch")
    assert group is not None
    assert set(group.subcommands) == {"open", "list", "diff", "code", "build",
                                      "revert", "drop"}


def test_every_subcommand_says_how_to_use_it():
    from extcli_src.shell.builtins import build_registry

    for command in build_registry().get("patch").subcommands.values():
        assert command.usage.startswith("patch "), command.name
        assert command.summary


def test_the_ones_that_change_something_say_so():
    from extcli_src.shell.builtins import build_registry

    group = build_registry().get("patch")
    for name in ("open", "build", "revert", "drop", "code"):
        assert group.subcommands[name].mutating is True, name
    for name in ("list", "diff"):
        assert group.subcommands[name].mutating is False, name


@pytest.mark.parametrize("name", ["list", "diff", "code", "build", "open",
                                  "drop"])
def test_help_needs_no_device(name):
    from extcli_src.shell.builtins import build_registry

    group = build_registry().get("patch")
    assert group.subcommands[name].help_result() is not None


# ------------------------------------------------------- end to end, no phone

@pytest.fixture()
def shell(tmp_path, monkeypatch):
    """A context whose patch directories are two temporary ones."""
    from extcli_src.shell.builtins import patch as patch_cmd

    work_root, state_root = roots(tmp_path)
    monkeypatch.setattr(patch_cmd, "_roots", lambda: (work_root, state_root))

    from test_commands import make_ctx, run

    context = make_ctx()
    context.assume_yes = True
    return context, run, work_root, state_root


def test_a_workspace_goes_from_nothing_to_an_installable_plugin(shell,
                                                                tmp_path):
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    source = plugin_tree(tmp_path / "source")

    assert "no patch workspaces" in plain.text(run(context, "patch list"))

    ok, _detail = store.create(work_root, state_root, "sample", source,
                               label="Sample", version="1.2.0")
    assert ok
    assert "as it was opened" in plain.text(run(context, "patch diff"))

    _write(os.path.join(store.work_dir(work_root, "sample"), "plug", "src",
                        "extra.py"), "one\ntwo\n")
    text = plain.text(run(context, "patch diff"))
    assert "plug/src/extra.py" in text and "+2" in text

    out = str(tmp_path / "built.eaf")
    text = plain.text(run(context, "patch build --out %s" % out))
    assert "extCLI patch-" in text
    from extcli_src.compat import plugins

    assert plugins.read_archive(out)["name"].startswith("extCLI patch-")


def test_building_an_untouched_workspace_is_refused_with_a_way_out(shell,
                                                                    tmp_path):
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    store.create(work_root, state_root, "sample", plugin_tree(tmp_path / "s"))
    text = plain.text(run(context, "patch build"))
    assert "exactly as it was opened" in text and "--empty" in text


def test_one_workspace_need_not_be_named_and_three_must_be(shell, tmp_path):
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    source = plugin_tree(tmp_path / "s")
    store.create(work_root, state_root, "one", source)
    assert "as it was opened" in plain.text(run(context, "patch diff"))

    store.create(work_root, state_root, "two", source)
    text = plain.text(run(context, "patch diff"))
    assert "2 workspaces are open" in text
    assert "as it was opened" in plain.text(run(context, "patch diff two"))


def test_a_workspace_that_was_never_opened_says_so(shell):
    from extcli_src.render import plain

    context, run, _work, _state = shell
    assert "no patch workspaces" in plain.text(run(context, "patch diff"))


def test_reverting_puts_a_file_back(shell, tmp_path):
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    store.create(work_root, state_root, "sample", plugin_tree(tmp_path / "s"))
    path = os.path.join(store.work_dir(work_root, "sample"), "plug", "src",
                        "helper.py")
    _write(path, "gone\n")
    assert "1 file" in plain.text(run(context, "patch diff"))

    run(context, "patch revert")
    assert store.changes(work_root, state_root, "sample").empty()
    assert "line 1" in open(path).read()


def test_reverting_removes_a_file_that_was_not_there_before(shell, tmp_path):
    context, run, work_root, state_root = shell
    store.create(work_root, state_root, "sample", plugin_tree(tmp_path / "s"))
    added = os.path.join(store.work_dir(work_root, "sample"), "new.py")
    _write(added, "x\n")

    run(context, "patch revert sample new.py")
    assert not os.path.exists(added)


def test_code_lists_what_is_compiled_and_reads_one(shell, tmp_path):
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    source = plugin_tree(tmp_path / "s")
    compiled(tmp_path / "s", name=os.path.join("plug", "src", "thing.pyc"))
    store.create(work_root, state_root, "sample", source)

    assert "plug/src/thing.pyc" in plain.text(run(context, "patch code"))
    text = plain.text(run(context, "patch code thing.pyc"))
    assert "https://example.invalid/api" in text
    assert "RETURN_VALUE" in plain.text(run(context,
                                            "patch code thing.pyc --dis"))


def test_code_can_change_a_constant_and_the_change_is_a_change(shell,
                                                                tmp_path):
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    source = plugin_tree(tmp_path / "s")
    compiled(tmp_path / "s", name=os.path.join("plug", "src", "thing.pyc"))
    store.create(work_root, state_root, "sample", source)

    text = plain.text(run(
        context,
        "patch code thing.pyc --set https://example.invalid/api "
        "https://elsewhere.invalid"))
    assert "replaced 1" in text

    changed = store.changes(work_root, state_root, "sample")
    assert [entry.path for entry in changed] == ["plug/src/thing.pyc"]


def test_a_source_only_plugin_says_there_is_nothing_compiled(shell, tmp_path):
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    store.create(work_root, state_root, "sample", plugin_tree(tmp_path / "s"))
    assert "no compiled files" in plain.text(run(context, "patch code"))


def test_dropping_a_workspace_from_the_shell(shell, tmp_path):
    context, run, work_root, state_root = shell
    store.create(work_root, state_root, "sample", plugin_tree(tmp_path / "s"))
    run(context, "patch drop sample")
    assert store.names(work_root) == []


def test_opening_says_so_when_the_shell_cannot_see_the_workspace(shell,
                                                                  tmp_path):
    """/patch is off by default, so the first `patch open` writes a tree
    nobody can cd into unless it says so."""
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    source = plugin_tree(tmp_path / "s")

    class OnePlugin(object):
        id = "sample"
        name = "Sample"
        version = "1.2.0"
        path = source

        def get(self, query):
            return self if query == "sample" else None

        def find(self, query):
            return [self] if query in ("sample", "Sample") else []

        def list_plugins(self):
            return [self]

    context.services.plugins = OnePlugin()
    text = plain.text(run(context, "patch open sample"))
    assert "opened" in text
    # the default has no mounts on beyond the rootfs, so the notice is due
    assert "/patch is not mounted" in text
    assert "config set mount_patch true" in text
    assert store.names(work_root) == ["sample"]

    text = plain.text(run(context, "patch open sample"))
    assert "already a workspace" in text and "--force" in text


def test_the_notice_is_gone_once_the_mount_is_on(monkeypatch):
    from extcli_src.shell.builtins import patch as patch_cmd
    from extcli_src.ui import prefs

    monkeypatch.setattr(prefs, "_get", lambda key, default:
                        True if key == "mount_patch" else default)
    assert patch_cmd._mount_warning() == []


def test_a_mistyped_name_does_not_drop_the_only_workspace(shell, tmp_path):
    """Being handed the only one because a name was mistyped is fine for a
    command that prints a diff and not for one that deletes."""
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    store.create(work_root, state_root, "sample", plugin_tree(tmp_path / "s"))
    text = plain.text(run(context, "patch drop smaple"))
    assert "no workspace called smaple" in text
    assert store.names(work_root) == ["sample"]


def test_a_file_where_a_workspace_might_be_is_still_a_file(shell, tmp_path):
    """`patch diff helper.py` with one workspace open means the file."""
    from extcli_src.render import plain

    context, run, work_root, state_root = shell
    store.create(work_root, state_root, "sample", plugin_tree(tmp_path / "s"))
    path = os.path.join(store.work_dir(work_root, "sample"), "plug", "src",
                        "helper.py")
    _write(path, open(path).read().replace("line 4", "line four"))

    text = plain.text(run(context, "patch diff plug/src/helper.py"))
    assert "-line 4" in text and "+line four" in text
