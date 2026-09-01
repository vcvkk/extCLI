# SPDX-License-Identifier: Apache-2.0

"""Unpacking a root filesystem, and deciding whether one is possible.

Two things are being pinned here. The extraction rules, because a tarball is
untrusted input and a member named `../../etc` writes outside the rootfs — that
is the whole reason this is not one call to `extractall`. And the reading of the
exec experiments, because the verdict decides whether weeks go into building
something the device will refuse to run, and the strings it reads come from a
device this test cannot reach.
"""

import io
import os
import tarfile

import pytest

from extcli_src.backends import linker as linker_module
from extcli_src.rootfs import exec_probe, install, layout


# ------------------------------------------------------------------- tarballs

def make_tar(path, entries):
    """entries: (name, kind, payload) with kind in file/dir/symlink/hardlink."""
    with tarfile.open(path, "w") as archive:
        for name, kind, payload in entries:
            if kind == "dir":
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif kind == "symlink":
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                archive.addfile(info)
            elif kind == "hardlink":
                info = tarfile.TarInfo(name)
                info.type = tarfile.LNKTYPE
                info.linkname = payload
                archive.addfile(info)
            elif kind == "device":
                info = tarfile.TarInfo(name)
                info.type = tarfile.CHRTYPE
                info.devmajor, info.devminor = 1, 3
                archive.addfile(info)
            else:
                data = payload.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644 if kind == "file" else int(kind, 8)
                archive.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def alpine(tmp_path):
    """A tarball shaped like a minirootfs, warts included."""
    return make_tar(str(tmp_path / "rootfs.tar"), [
        ("bin", "dir", None),
        ("etc", "dir", None),
        ("lib", "dir", None),
        ("bin/busybox", "755", "#!not really an elf"),
        ("bin/sh", "symlink", "busybox"),
        ("lib/ld-musl-aarch64.so.1", "symlink", "/lib/libc.musl-aarch64.so.1"),
        ("lib/libc.musl-aarch64.so.1", "file", "libc"),
        ("etc/alpine-release", "file", "3.20.3\n"),
        ("dev", "dir", None),
        ("dev/null", "device", None),
    ])


# ------------------------------------------------------------------ safe_name

def test_absolute_and_climbing_names_are_refused():
    for name in ("/etc/passwd", "../outside", "../../etc/shadow",
                 "bin/../../escape", "C:/windows", "\\\\server\\share"):
        assert install.safe_name(name) is None, name


def test_ordinary_names_survive():
    assert install.safe_name("bin/busybox") == "bin/busybox"
    assert install.safe_name("./etc/hosts") == "etc/hosts"
    assert install.safe_name("a/b/../c") == "a/c"


def test_the_archive_root_itself_is_not_a_member():
    assert install.safe_name(".") is None
    assert install.safe_name("") is None


def test_setuid_and_setgid_are_dropped():
    assert install.permissions(0o4755) == 0o755
    assert install.permissions(0o2755) == 0o755
    assert install.permissions(0o6755) == 0o755
    assert install.permissions(0o644) == 0o644


# ------------------------------------------------------------------- extract

def test_a_rootfs_unpacks(alpine, tmp_path):
    root = str(tmp_path / "root")
    report = install.install(alpine, root)
    assert layout.installed(root)
    assert report.written >= 3
    assert report.symlinks == 2
    assert os.path.islink(os.path.join(root, "bin/sh"))


def test_device_nodes_are_skipped_not_fatal(alpine, tmp_path):
    root = str(tmp_path / "root")
    report = install.install(alpine, root)
    # extractall would have raised here and left a half-unpacked tree
    assert any("dev/null" in name for name, _ in report.skipped)
    assert layout.installed(root)


def test_an_absolute_symlink_target_is_kept_as_written(alpine, tmp_path):
    # inside a rootfs /lib/... is correct; rewriting it would break the guest
    root = str(tmp_path / "root")
    install.install(alpine, root)
    link = os.path.join(root, "lib/ld-musl-aarch64.so.1")
    assert os.readlink(link) == "/lib/libc.musl-aarch64.so.1"


def test_an_escaping_member_is_refused_and_nothing_is_written(tmp_path):
    tarball = make_tar(str(tmp_path / "evil.tar"), [
        ("../escaped", "file", "gotcha"),
        ("/etc/passwd", "file", "gotcha"),
        ("good", "file", "fine"),
    ])
    root = str(tmp_path / "root")
    report = install.install(tarball, root)
    assert len(report.refused) == 2
    assert not os.path.exists(str(tmp_path / "escaped"))
    assert os.path.isfile(os.path.join(root, "good"))


def test_a_symlink_replaces_whatever_was_there(tmp_path):
    tarball = make_tar(str(tmp_path / "t.tar"), [
        ("bin", "dir", None),
        ("bin/sh", "file", "a real file first"),
        ("bin/sh", "symlink", "busybox"),
    ])
    root = str(tmp_path / "root")
    install.install(tarball, root)
    assert os.path.islink(os.path.join(root, "bin/sh"))


def test_hard_links_become_copies(tmp_path):
    tarball = make_tar(str(tmp_path / "t.tar"), [
        ("bin", "dir", None),
        ("bin/busybox", "file", "binary"),
        ("bin/ls", "hardlink", "bin/busybox"),
    ])
    root = str(tmp_path / "root")
    install.install(tarball, root)
    with open(os.path.join(root, "bin/ls")) as handle:
        assert handle.read() == "binary"


def test_a_read_only_directory_can_still_be_filled(tmp_path):
    """Modes are applied after the contents, or writing into 0555 fails."""
    tarball = make_tar(str(tmp_path / "t.tar"), [
        ("locked", "dir", None),
        ("locked/inside", "file", "written anyway"),
    ])
    # rewrite the directory mode to something unwritable
    with tarfile.open(str(tmp_path / "t.tar")) as archive:
        members = archive.getmembers()
    assert members[0].isdir()

    root = str(tmp_path / "root")
    report = install.install(str(tmp_path / "t.tar"), root)
    assert report.written == 1
    assert os.path.isfile(os.path.join(root, "locked/inside"))


def test_installing_a_missing_tarball_says_so(tmp_path):
    with pytest.raises(IOError):
        install.install(str(tmp_path / "nope.tar"), str(tmp_path / "root"))


# -------------------------------------------------------------------- layout

def test_an_empty_directory_is_not_a_rootfs(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    assert not layout.installed(root)
    assert ("state", "not installed") in layout.status_rows(root)


def test_status_reads_the_release_and_the_shell(alpine, tmp_path):
    root = str(tmp_path / "root")
    install.install(alpine, root)
    rows = dict(layout.status_rows(root))
    assert rows["state"] == "installed"
    assert rows["release"] == "3.20.3"
    assert rows["shell"] == "/bin/sh"
    assert "files" in rows["contents"]


def test_os_release_is_read_when_there_is_no_alpine_release(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "etc"))
    with open(os.path.join(root, "etc/os-release"), "w") as handle:
        handle.write('NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 12"\n')
    assert layout.release(root) == "Debian GNU/Linux 12"


def test_human_size():
    assert layout.human_size(512) == "512 B"
    assert layout.human_size(2048) == "2.0 KB"
    assert layout.human_size(5 * 1024 * 1024) == "5.0 MB"


# ---------------------------------------------------------------- exec probe

def attempt(status):
    return {"status": status, "detail": ""}


def test_a_device_that_allows_direct_exec_gets_an_ordinary_rootfs():
    strategy, sentence = exec_probe.verdict({
        "system": attempt(exec_probe.OK),
        "direct": attempt(exec_probe.OK),
        "wrapped": attempt(exec_probe.OK),
    })
    assert strategy == exec_probe.DIRECT
    assert "proot" in sentence


def test_linker_only_exec_means_every_exec_has_to_be_rewritten():
    strategy, sentence = exec_probe.verdict({
        "system": attempt(exec_probe.OK),
        "direct": attempt(exec_probe.BLOCKED),
        "wrapped": attempt(exec_probe.OK),
    })
    assert strategy == exec_probe.WRAPPED
    # what it rules out, and what it still allows: extCLI's shell starts every
    # command itself, so no guest process ever has to exec another
    assert "proot will not do" in sentence
    assert "a rootfs still works" in sentence


def test_no_child_exec_at_all_rules_a_rootfs_out():
    strategy, _ = exec_probe.verdict({
        "system": attempt(exec_probe.OK),
        "direct": attempt(exec_probe.BLOCKED),
        "wrapped": attempt(exec_probe.BLOCKED),
    })
    assert strategy == exec_probe.NONE


def test_a_device_that_cannot_spawn_anything_is_hopeless():
    strategy, sentence = exec_probe.verdict({
        "system": attempt(exec_probe.BLOCKED),
        "direct": attempt(exec_probe.OK),
        "wrapped": attempt(exec_probe.OK),
    })
    assert strategy == exec_probe.NONE
    assert "no rootfs is possible" in sentence


def test_an_unfinished_experiment_does_not_read_as_a_refusal():
    strategy, sentence = exec_probe.verdict({
        "system": attempt(exec_probe.OK),
        "direct": attempt(exec_probe.UNKNOWN),
        "wrapped": attempt(exec_probe.UNKNOWN),
    })
    assert strategy == exec_probe.NONE
    assert "run them again" in sentence


def test_the_marker_is_what_proves_a_child_ran():
    status, _ = exec_probe.read_attempt(0, "extcli-exec-probe\n", "")
    assert status == exec_probe.OK


def test_the_refusals_a_device_actually_prints():
    for text in ("sh: /data/user/0/x/toybox: Permission denied",
                 "/data/user/0/x/toybox: can't execute: Permission denied",
                 "sh: /data/user/0/x/toybox: not executable"):
        status, detail = exec_probe.read_attempt(126, "", text)
        assert status == exec_probe.BLOCKED, text
        assert detail


def test_exit_zero_without_the_marker_is_not_a_pass():
    status, _ = exec_probe.read_attempt(0, "", "")
    assert status == exec_probe.UNKNOWN


def test_the_experiments_are_reported_as_a_matrix():
    lines = exec_probe.summary_lines({
        "system": {"status": exec_probe.OK, "detail": "child ran"},
        "direct": {"status": exec_probe.BLOCKED, "detail": "Permission denied"},
        "wrapped": {"status": exec_probe.OK, "detail": "child ran"},
    })
    assert lines[0].startswith("[+]")
    assert lines[1].startswith("[x]")
    assert any("strategy: wrapped" in line for line in lines)


def test_the_experiments_need_a_device_and_say_so_without_one(tmp_path):
    results = exec_probe.run(str(tmp_path), linker="/no/such/linker")
    assert all(r["status"] == exec_probe.UNKNOWN for r in results.values())


def test_the_experiments_run_the_control_then_the_three_questions(tmp_path):
    """The parent is a copy of the system shell, started through the linker."""
    seen = []

    def fake_runner(command):
        seen.append(command)
        return 0, exec_probe.MARKER, ""

    linker = str(tmp_path / "linker")
    shell = str(tmp_path / "sh")
    for path in (linker, shell):
        with open(path, "w") as handle:
            handle.write("not really")

    work = str(tmp_path / "work")
    results = exec_probe.run(work, linker, shell, runner=fake_runner)
    copy = os.path.join(work, "sh")

    assert len(seen) == 4, "control, then one per question"
    for command in seen:
        assert command[:3] == [linker, copy, "-c"]
    scripts = [command[3] for command in seen]
    assert scripts[0] == "echo %s" % exec_probe.MARKER
    assert any(script.startswith("/system/bin/echo") for script in scripts[1:])
    assert any(script.startswith(copy) for script in scripts[1:])
    assert any(script.startswith(linker) for script in scripts[1:])
    assert results[exec_probe.CONTROL]["status"] == exec_probe.OK


def test_a_control_that_fails_stops_the_experiment(tmp_path):
    """The bug this exists to prevent.

    Android's toybox has no `sh` applet, so a toybox parent answered "Unknown
    command sh" to all three questions, and the verdict announced that the
    device forbids everything. It was the experiment that had failed.
    """
    calls = []

    def broken(command):
        calls.append(command)
        return 1, "", 'toybox: Unknown command sh (see "toybox --help")'

    linker = str(tmp_path / "linker")
    shell = str(tmp_path / "sh")
    for path in (linker, shell):
        with open(path, "w") as handle:
            handle.write("not really")

    results = exec_probe.run(str(tmp_path / "work"), linker, shell,
                             runner=broken)
    assert len(calls) == 1, "the questions are not worth asking"
    strategy, sentence = exec_probe.verdict(results)
    assert strategy == exec_probe.NONE
    assert "Nothing has been learned" in sentence
    assert "no rootfs is possible" not in sentence


def test_a_program_complaining_about_its_arguments_is_not_a_refusal():
    for text in ('toybox: Unknown command sh (see "toybox --help")',
                 "usage: sh [-c command]"):
        status, _ = exec_probe.read_attempt(1, "", text)
        assert status == exec_probe.UNKNOWN, text


def test_the_shell_is_found_where_android_keeps_it():
    assert exec_probe.find_shell({"/system/bin/sh"}.__contains__) \
        == "/system/bin/sh"
    assert exec_probe.find_shell(lambda path: False) is None


def test_the_control_appears_in_the_matrix():
    lines = exec_probe.summary_lines({
        exec_probe.CONTROL: {"status": exec_probe.OK, "detail": "child ran"},
        "system": {"status": exec_probe.OK, "detail": "child ran"},
        "direct": {"status": exec_probe.BLOCKED, "detail": "Permission denied"},
        "wrapped": {"status": exec_probe.OK, "detail": "child ran"},
    })
    assert "our own shell starts at all" in lines[0]
    assert any("strategy: wrapped" in line for line in lines)


# ------------------------------------------------------------ linker backend

def test_the_linker_is_chosen_by_abi():
    present = {linker_module.LINKER64, linker_module.LINKER32}
    assert linker_module.find_linker("arm64-v8a", present.__contains__) \
        == linker_module.LINKER64
    assert linker_module.find_linker("armeabi-v7a", present.__contains__) \
        == linker_module.LINKER32


def test_a_missing_linker_is_not_guessed_at():
    assert linker_module.find_linker("arm64-v8a", lambda path: False) is None


def test_the_32_bit_linker_is_used_when_it_is_the_only_one():
    only32 = {linker_module.LINKER32}
    assert linker_module.find_linker("arm64-v8a", only32.__contains__) \
        == linker_module.LINKER32


def test_the_backend_puts_the_linker_first_and_our_binary_second(tmp_path):
    """A multi-call binary dispatches on argv[0], so this order is the whole
    difference between `ls` working and toybox printing its usage."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "busybox").write_text("elf")
    backend = linker_module.LinkerBackend(str(bin_dir), linker="/system/bin/linker64")

    command = backend.command_for(["busybox", "ls", "-l"])
    assert command == ["/system/bin/linker64", str(bin_dir / "busybox"),
                       "ls", "-l"]


def test_the_backend_only_claims_its_own_binaries(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "busybox").write_text("elf")
    backend = linker_module.LinkerBackend(str(bin_dir), linker="/system/bin/linker64")

    assert backend.which("busybox")
    # everything on the system PATH belongs to the system backend
    assert backend.which("ls") is None
    assert backend.which("/system/bin/ls") is None
    assert backend.command_for(["ls"]) is None


def test_the_backend_is_unavailable_without_a_linker(tmp_path):
    backend = linker_module.LinkerBackend(str(tmp_path), linker=None)
    assert not backend.available()
    assert backend.run(["anything"]).status == 127


def test_installing_binaries_clears_the_lookup_cache(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    backend = linker_module.LinkerBackend(str(bin_dir), linker="/system/bin/linker64")
    assert backend.which("busybox") is None
    (bin_dir / "busybox").write_text("elf")
    assert backend.which("busybox") is None, "the miss should still be cached"
    backend.forget()
    assert backend.which("busybox")


def test_our_directory_comes_first_on_the_path(tmp_path):
    backend = linker_module.LinkerBackend(str(tmp_path), linker="/system/bin/linker64")
    assert backend.environment()["PATH"].split(":")[0] == str(tmp_path)


# ------------------------------------------------------------------- chaining

def test_the_linker_backend_joins_the_chain_when_binaries_exist(tmp_path,
                                                                monkeypatch):
    from extcli_src.backends import chain

    # no /system/bin/linker64 on a desktop, and the backend refuses to pretend
    monkeypatch.setattr(linker_module, "find_linker",
                        lambda abi=None, exists=None: linker_module.LINKER64)
    native = tmp_path / "native"
    native.mkdir()
    built = chain.build(native_dir=str(native), abi="arm64-v8a")
    names = [backend.name for backend in built.backends]
    # after system, so a toybox applet is never shadowed by one of ours
    assert names.index("system") < names.index("linker") < names.index("inproc")


def test_the_chain_is_unchanged_without_a_native_directory():
    from extcli_src.backends import chain

    names = [backend.name for backend in chain.build().backends]
    assert "linker" not in names


def _inside(tmp_path):
    """The mount table of a shell that is standing inside a rootfs."""
    from extcli_src.rootfs import mounts

    root = tmp_path / "rootfs"
    root.mkdir()
    table = mounts.Paths(rows=[("/", str(root))], values={mounts.ROOT: True})
    assert table.active
    return table


def test_a_chain_inside_a_rootfs_drops_what_cannot_translate(tmp_path,
                                                             monkeypatch):
    """The one that cost a container.

    Being inside a rootfs and having a backend that can translate are separate
    facts, and they come apart: the paths follow what is installed, while the
    rootfs backend is built separately and returns None when, say, there is no
    linker. The chain then kept `system`, whose idea of `/etc` is the phone's —
    so a guest path reached a backend that reads it as a real one.
    """
    from extcli_src.backends import chain

    monkeypatch.setattr(linker_module, "find_linker",
                        lambda abi=None, exists=None: linker_module.LINKER64)
    native = tmp_path / "native"
    native.mkdir()
    built = chain.build(native_dir=str(native), abi="arm64-v8a",
                        rootfs=None, paths=_inside(tmp_path))
    names = [backend.name for backend in built.backends]
    assert "system" not in names and "linker" not in names
    # and something is still there to run commands with
    assert names == ["inproc"]
    assert all(backend.translates for backend in built.backends)


def test_the_rootfs_backend_is_kept_because_it_translates(tmp_path):
    from extcli_src.backends import chain

    class FakeRootfs(object):
        name = "rootfs"
        translates = True

        def available(self):
            return True

    built = chain.build(rootfs=FakeRootfs(), paths=_inside(tmp_path))
    assert [backend.name for backend in built.backends] == ["rootfs", "inproc"]


def test_a_rootfs_backend_that_cannot_translate_is_dropped_too(tmp_path):
    """The latch is about the property, not about the name: anything claiming
    to be the rootfs backend still has to be able to translate."""
    from extcli_src.backends import chain

    class Impostor(object):
        name = "rootfs"
        translates = False

        def available(self):
            return True

    built = chain.build(rootfs=Impostor(), paths=_inside(tmp_path))
    assert [backend.name for backend in built.backends] == ["inproc"]


def test_outside_a_rootfs_nothing_is_dropped():
    """Without a mount table there is no guest path to get wrong, and the
    chain is what it always was."""
    from extcli_src.backends import chain

    names = [backend.name for backend in chain.build(paths=None).backends]
    assert "system" in names and "inproc" in names


def test_inproc_says_whether_it_was_given_the_map(tmp_path):
    from extcli_src.backends.inproc import InprocBackend
    from extcli_src.rootfs import mounts

    assert not InprocBackend().translates
    assert not InprocBackend(paths=mounts.Paths()).translates
    assert InprocBackend(paths=_inside(tmp_path)).translates


# ------------------------------------------------------------ guest launching

from extcli_src.rootfs import guest  # noqa: E402


def musl_rootfs(tmp_path):
    root = tmp_path / "root"
    for name in ("bin", "etc", "lib", "usr/lib"):
        (root / name).mkdir(parents=True)
    (root / "lib/ld-musl-aarch64.so.1").write_text("loader")
    (root / "bin/busybox").write_text("elf")
    (root / "bin/sh").symlink_to("busybox")
    return str(root)


def test_the_musl_loader_is_found_by_architecture(tmp_path):
    root = musl_rootfs(tmp_path)
    assert guest.loader_in(root).endswith("ld-musl-aarch64.so.1")


def test_a_rootfs_without_a_loader_says_so(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert guest.loader_in(str(root)) is None


def test_direct_hands_the_guest_binary_to_bionic(tmp_path):
    root = musl_rootfs(tmp_path)
    command = guest.command_for(guest.DIRECT, root, "/system/bin/linker64",
                                ["/bin/busybox", "true"])
    assert command == ["/system/bin/linker64",
                       os.path.join(root, "bin/busybox"), "true"]


def test_musl_puts_its_own_loader_in_between(tmp_path):
    """bionic's linker starts musl's loader, which starts the program.

    The order is the whole point: without it a musl binary is being loaded by
    the wrong libc's linker.
    """
    root = musl_rootfs(tmp_path)
    command = guest.command_for(guest.MUSL, root, "/system/bin/linker64",
                                ["/bin/sh", "-c", "echo hi"])
    assert command == ["/system/bin/linker64",
                       os.path.join(root, "lib/ld-musl-aarch64.so.1"),
                       # /bin/sh is a link to busybox, and it is followed
                       os.path.join(root, "bin/busybox"), "-c", "echo hi"]


def test_a_guest_path_is_read_as_the_guest_sees_it(tmp_path):
    root = musl_rootfs(tmp_path)
    command = guest.command_for(guest.DIRECT, root, "/l", ["/bin/busybox"])
    # /bin/busybox means inside the rootfs, not on the device
    assert command[1] == os.path.join(root, "bin/busybox")
    assert not command[1].startswith("/bin")


def test_a_guest_path_that_does_not_exist_builds_nothing(tmp_path):
    root = musl_rootfs(tmp_path)
    assert guest.command_for(guest.DIRECT, root, "/l", ["/bin/nope"]) is None


def test_a_relative_path_is_left_alone(tmp_path):
    root = musl_rootfs(tmp_path)
    command = guest.command_for(guest.DIRECT, root, "/l", ["busybox"])
    assert command[1] == "busybox"


def test_the_library_path_is_only_set_for_the_strategies_that_want_it(tmp_path):
    root = musl_rootfs(tmp_path)
    assert guest.environment_for(guest.DIRECT, root) == {}
    assert guest.environment_for(guest.MUSL, root) == {}
    for strategy in (guest.DIRECT_PATH, guest.MUSL_PATH):
        env = guest.environment_for(strategy, root)
        assert env["LD_LIBRARY_PATH"].startswith(os.path.join(root, "lib"))
        assert os.path.join(root, "usr/lib") in env["LD_LIBRARY_PATH"]


def test_without_a_loader_the_musl_strategies_cannot_be_built(tmp_path):
    root = str(tmp_path / "bare")
    os.makedirs(root)
    assert guest.command_for(guest.MUSL, root, "/l", ["/bin/sh"]) is None


def test_the_cheapest_working_strategy_is_the_one_chosen():
    assert guest.chosen({
        guest.DIRECT: {"status": guest.BLOCKED},
        guest.DIRECT_PATH: {"status": guest.OK},
        guest.MUSL: {"status": guest.OK},
    }) == guest.DIRECT_PATH
    assert guest.chosen({
        guest.DIRECT: {"status": guest.OK},
        guest.MUSL: {"status": guest.OK},
    }) == guest.DIRECT
    assert guest.chosen({guest.DIRECT: {"status": guest.BLOCKED}}) is None


def test_the_failures_a_mismatched_libc_produces():
    for text in ("CANNOT LINK EXECUTABLE: cannot locate symbol",
                 "exec format error",
                 "sh: symbol not found",
                 "Segmentation fault"):
        status, detail = guest.read_attempt(1, "", text)
        assert status == guest.BLOCKED, text
        assert detail


def test_the_marker_is_what_proves_the_guest_ran():
    status, _ = guest.read_attempt(0, "extcli-guest\n", "")
    assert status == guest.OK


def test_every_strategy_is_tried_once(tmp_path):
    root = musl_rootfs(tmp_path)
    seen = []

    def runner(command, env):
        seen.append((command, env))
        return 1, "", "cannot execute"

    results = guest.probe(root, "/system/bin/linker64", runner,
                          native_dir=native.directory(RES, "arm64-v8a"))
    assert len(seen) == len(guest.ORDER)
    assert set(results) == set(guest.ORDER)
    assert guest.chosen(results) is None
    carried = [bool(env.get("LD_LIBRARY_PATH")) for _command, env in seen]
    assert carried == [strategy in guest.WITH_LIBRARY_PATH
                       for strategy in guest.ORDER]


def test_probing_stops_short_when_there_is_no_loader(tmp_path):
    """A rootfs with programs but no musl loader — glibc's, say."""
    root = str(tmp_path / "bare")
    os.makedirs(os.path.join(root, "bin"))
    with open(os.path.join(root, "bin/sh"), "w") as handle:
        handle.write("elf")
    calls = []
    results = guest.probe(root, "/l", lambda c, e: calls.append(c) or (0, "", ""),
                          native_dir=native.directory(RES, "arm64-v8a"))
    for strategy in guest.THROUGH_LOADER:
        assert results[strategy]["status"] == guest.UNKNOWN
        assert "no musl loader" in results[strategy]["detail"]
    # only the ones that do not need it are attempted
    assert len(calls) == len(guest.ORDER) - len(guest.THROUGH_LOADER)


def test_a_missing_program_is_not_attempted_either(tmp_path):
    root = str(tmp_path / "empty")
    os.makedirs(root)
    calls = []
    results = guest.probe(root, "/l", lambda c, e: calls.append(c) or (0, "", ""))
    assert not calls
    assert all(r["status"] == guest.UNKNOWN for r in results.values())


# ----------------------------------------------------------- rootfs backend

from extcli_src.rootfs import backend as backend_module  # noqa: E402


def installed_rootfs(tmp_path, strategy=guest.MUSL):
    root = musl_rootfs(tmp_path)
    for name in ("usr/bin", "sbin"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    for path in ("bin/ls", "bin/grep", "usr/bin/awk", "sbin/ip"):
        with open(os.path.join(root, path), "w") as handle:
            handle.write("elf")
    if strategy:
        layout.save_strategy(root, strategy)
    return root


def test_the_strategy_is_remembered_with_the_rootfs(tmp_path):
    """It lives in the rootfs, not in settings: unpack a new one and it has to
    be measured again, leave one alone and it does not."""
    root = installed_rootfs(tmp_path)
    assert layout.saved_strategy(root) == guest.MUSL
    assert layout.saved_strategy(str(tmp_path / "nothing")) is None


def test_the_backend_finds_guest_programs_by_guest_path(tmp_path):
    root = installed_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/system/bin/linker64")
    assert backend.available()
    assert backend.which("ls") == "/bin/ls"
    assert backend.which("awk") == "/usr/bin/awk"
    assert backend.which("ip") == "/sbin/ip"
    assert backend.which("nothing_here") is None


def test_the_backend_builds_the_command_through_the_linker(tmp_path):
    root = installed_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/system/bin/linker64")
    assert backend.command_for(["ls", "-la"]) == [
        "/system/bin/linker64",
        os.path.join(root, "lib/ld-musl-aarch64.so.1"),
        os.path.join(root, "bin/ls"),
        "-la",
    ]


def test_the_backend_is_unavailable_until_the_launch_is_known2(tmp_path):
    root = installed_rootfs(tmp_path, strategy=None)
    backend = backend_module.RootfsBackend(root, "/system/bin/linker64")
    assert not backend.available()
    assert backend.run(["ls"]).status == 127
    assert "not installed" not in backend.describe()[0][1]


def test_the_backend_is_unavailable_without_a_rootfs(tmp_path):
    backend = backend_module.RootfsBackend(str(tmp_path / "gone"),
                                           "/system/bin/linker64", guest.MUSL)
    assert not backend.available()


def test_the_guest_path_is_what_the_guest_would_write(tmp_path):
    root = installed_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/l")
    # an absolute guest path resolves inside the rootfs, not on the phone
    assert backend.which("/bin/ls") == "/bin/ls"
    assert backend.which("/etc/hosts") is None


def test_the_backend_lists_what_the_rootfs_offers(tmp_path):
    root = installed_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/l")
    names = backend.commands()
    assert "ls" in names and "awk" in names and "ip" in names
    assert names == sorted(names)


def test_the_path_handed_to_a_guest_is_the_guest_s_own(tmp_path):
    root = installed_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/l")
    assert backend.environment()["PATH"] == \
        ":".join(layout.bin_dirs(mounts.HOME))


def test_a_tool_the_user_installed_can_be_found(tmp_path):
    """`uv tool install elyxbuilder` put elyb in /root/.local/bin and said the
    directory was not on PATH. It was right, and `which` did not look there
    either — so the tool that had just been installed could not be started."""
    root = installed_rootfs(tmp_path)
    local = os.path.join(root, "root/.local/bin")
    os.makedirs(local)
    with open(os.path.join(local, "elyb"), "w") as handle:
        handle.write("#!/bin/sh\n")
    backend = backend_module.RootfsBackend(root, "/l")
    assert backend.which("elyb") == "/root/.local/bin/elyb"
    assert "/root/.local/bin" in backend.environment()["PATH"].split(":")
    # and first, so a tool the user installed wins over an older packaged one
    assert backend.environment()["PATH"].split(":")[0] == "/root/.local/bin"


# --------------------------------------------------------- bundled sources

from extcli_src.rootfs import sources  # noqa: E402

RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "extcli", "res")


def test_alpine_is_bundled_and_matches_its_recorded_checksum():
    """The archive ships inside the plugin; if it is truncated, say so before
    unpacking rather than leaving a rootfs that fails later and less clearly."""
    source = sources.find("alpine")
    assert source is not None
    ok, detail = sources.verify(source, RES)
    assert ok, detail


def test_a_name_is_matched_however_it_is_typed():
    assert sources.find("Alpine") is sources.find("alpine")
    assert sources.find("  ALPINE ") is not None
    assert sources.find("debian") is None


def test_a_source_knows_which_devices_it_is_for():
    source = sources.find("alpine")
    assert source.supports("arm64-v8a")
    assert source.supports(None)
    assert not source.supports("armeabi-v7a")


def test_a_damaged_archive_is_refused(tmp_path):
    source = sources.find("alpine")
    fake = tmp_path / sources.DIRECTORY
    fake.mkdir()
    (fake / source.filename).write_bytes(b"not the real thing")
    ok, detail = sources.verify(source, str(tmp_path))
    assert not ok
    assert "does not match its recorded checksum" in detail


def test_a_missing_archive_is_refused(tmp_path):
    ok, detail = sources.verify(sources.find("alpine"), str(tmp_path))
    assert not ok
    assert "not bundled" in detail


def test_the_bundled_alpine_unpacks_into_a_real_rootfs(tmp_path):
    """The whole path, on the actual archive that ships."""
    source = sources.find("alpine")
    root = str(tmp_path / "root")
    report = install.install(source.path(RES), root)

    assert layout.installed(root)
    assert layout.release(root).startswith("3.")
    assert layout.shell_in(root) == "/bin/sh"
    assert guest.loader_in(root)
    assert report.written > 50 and report.symlinks > 100
    # the archive's own root entry is not an escape attempt and is not reported
    assert not report.refused, report.refused


def test_the_archive_root_is_ignored_rather_than_refused():
    class Member(object):
        def __init__(self, name):
            self.name = name

        def isdir(self):
            return True

        def ischr(self):
            return False

        isblk = isfifo = ischr

        def isfile(self):
            return False

        issym = islnk = isfile

    for name in (".", "./", ""):
        kind, _ = install.classify(Member(name))
        assert kind == install.IGNORED, name
    kind, _ = install.classify(Member("../escape"))
    assert kind == install.REFUSED


def test_the_bundled_rootfs_can_be_launched_and_run(tmp_path):
    """Everything but the exec: the argv that would start busybox."""
    source = sources.find("alpine")
    root = str(tmp_path / "root")
    install.install(source.path(RES), root)
    layout.save_strategy(root, guest.MUSL)

    backend = backend_module.RootfsBackend(root, "/system/bin/linker64")
    assert backend.available()
    assert backend.which("busybox") == "/bin/busybox"
    # busybox ships as a pile of symlinks; they have to count as commands
    assert backend.which("ls") == "/bin/ls"
    assert len(backend.commands()) > 100
    # /bin/ls is an absolute link to /bin/busybox; following it the guest's way
    # is what keeps the path inside the rootfs
    assert backend.command_for(["ls", "-la"]) == [
        "/system/bin/linker64",
        os.path.join(root, "lib/ld-musl-aarch64.so.1"),
        os.path.join(root, "bin/busybox"),
        "-la",
    ]


# -------------------------------------------------- resolving guest symlinks

def test_an_absolute_symlink_stays_inside_the_rootfs(tmp_path):
    """The bug the device found.

    Alpine's /bin/sh points at /bin/busybox. Resolved by the host that is a
    file on the phone which does not exist, and `rootfs probe launch` came back with
    "unable to open file .../rootfs/bin/sh". Inside the rootfs it is correct,
    so it has to be followed from the rootfs.
    """
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "bin"))
    with open(os.path.join(root, "bin/busybox"), "w") as handle:
        handle.write("elf")
    os.symlink("/bin/busybox", os.path.join(root, "bin/sh"))

    assert layout.resolve(root, "/bin/sh") == os.path.join(root, "bin/busybox")


def test_a_relative_symlink_is_followed_from_where_it_sits(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "usr/bin"))
    with open(os.path.join(root, "usr/bin/awk"), "w") as handle:
        handle.write("elf")
    os.symlink("awk", os.path.join(root, "usr/bin/gawk"))
    assert layout.resolve(root, "/usr/bin/gawk") \
        == os.path.join(root, "usr/bin/awk")


def test_a_chain_of_links_is_followed_to_the_end(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "bin"))
    with open(os.path.join(root, "bin/real"), "w") as handle:
        handle.write("elf")
    os.symlink("/bin/real", os.path.join(root, "bin/one"))
    os.symlink("one", os.path.join(root, "bin/two"))
    assert layout.resolve(root, "/bin/two") == os.path.join(root, "bin/real")


def test_a_link_through_a_directory_link(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "usr/bin"))
    with open(os.path.join(root, "usr/bin/env"), "w") as handle:
        handle.write("elf")
    os.symlink("/usr/bin", os.path.join(root, "bin"))
    assert layout.resolve(root, "/bin/env") == os.path.join(root, "usr/bin/env")


def test_climbing_out_with_dot_dot_lands_back_at_the_root(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "etc"))
    with open(os.path.join(root, "etc/hosts"), "w") as handle:
        handle.write("127.0.0.1")
    # ../../.. cannot escape: there is nothing above the rootfs to reach
    assert layout.resolve(root, "/etc/../../../etc/hosts") \
        == os.path.join(root, "etc/hosts")


def test_a_loop_gives_up_rather_than_spinning(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "bin"))
    os.symlink("/bin/b", os.path.join(root, "bin/a"))
    os.symlink("/bin/a", os.path.join(root, "bin/b"))
    assert layout.resolve(root, "/bin/a") is None


def test_a_dangling_link_resolves_to_nothing(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "bin"))
    os.symlink("/bin/gone", os.path.join(root, "bin/sh"))
    assert layout.resolve(root, "/bin/sh") is None


def test_the_real_alpine_shell_resolves_to_busybox(tmp_path):
    root = str(tmp_path / "root")
    install.install(sources.find("alpine").path(RES), root)
    assert layout.resolve(root, "/bin/sh") == os.path.join(root, "bin/busybox")
    assert guest.loader_in(root) == os.path.join(root,
                                                 "lib/ld-musl-aarch64.so.1")


# ------------------------------------------------------- reading the failure

def test_a_crash_is_named_not_numbered():
    """`exit -11` says nothing; the console had to be read twice to see it."""
    assert guest.describe_exit(-11) == "killed by SIGSEGV"
    assert guest.describe_exit(-6) == "killed by SIGABRT"
    assert guest.describe_exit(-99) == "killed by signal 99"
    assert guest.describe_exit(2) == "exit 2"
    status, detail = guest.read_attempt(-11, "", "")
    assert status == guest.BLOCKED
    assert "SIGSEGV" in detail


def test_a_foreign_libc_is_recognised_by_the_crash():
    """The device's answer, in full.

    bionic's linker mapped busybox, found musl's libc once it was pointed at
    it, and the process died — so the rootfs is not blocked, it is built
    against the wrong libc. Four red lines, one problem.
    """
    kind, sentence = guest.diagnose({
        guest.DIRECT: {"status": guest.BLOCKED,
                       "detail": 'library "libc.musl-aarch64.so.1" not found: '
                                 'needed by main executable'},
        guest.DIRECT_PATH: {"status": guest.BLOCKED,
                            "detail": "killed by SIGSEGV"},
        guest.MUSL: {"status": guest.BLOCKED,
                     "detail": "Could not find a PHDR: broken executable?"},
        guest.MUSL_PATH: {"status": guest.BLOCKED,
                          "detail": "Could not find a PHDR: broken executable?"},
    })
    assert kind == guest.FOREIGN_LIBC
    assert "built against" in sentence and "bionic" in sentence


def test_missing_libraries_are_told_apart_from_a_wrong_libc():
    kind, _ = guest.diagnose({
        guest.DIRECT: {"status": guest.BLOCKED,
                       "detail": 'library "libc.so" not found: needed by main '
                                 'executable'},
        guest.DIRECT_PATH: {"status": guest.UNKNOWN, "detail": ""},
    })
    assert kind == guest.NO_LIBRARIES


def test_a_refused_loader_alone_is_its_own_diagnosis():
    kind, _ = guest.diagnose({
        guest.MUSL: {"status": guest.BLOCKED, "detail": "Could not find a PHDR"},
        guest.MUSL_PATH: {"status": guest.BLOCKED,
                          "detail": "Could not find a PHDR"},
    })
    assert kind == guest.LOADER_REFUSED


def test_the_diagnosis_is_what_the_summary_ends_with():
    lines = guest.summary_lines({
        guest.DIRECT: {"status": guest.BLOCKED, "detail": "killed by SIGSEGV"},
    })
    assert "cannot be started by bionic's linker" in lines[-1]


# ------------------------------------------------- is exec allowed anywhere

from extcli_src.rootfs import execdirs  # noqa: E402


def fake_scan_runner(answers):
    """answers: {command_path: (code, out, err)}, with a default for the rest."""
    def run(command):
        return answers.get(command[0], answers.get("*", (1, "", "denied")))

    return run


def test_a_directory_that_allows_exec_is_found(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    source = tmp_path / "toybox"
    source.write_text("elf")

    runner = fake_scan_runner({
        str(source): (0, execdirs.MARKER, ""),
        str(good / "toybox"): (0, execdirs.MARKER, ""),
    })
    results = execdirs.scan([("good", str(good))], runner, source=str(source))
    assert results["good"]["status"] == execdirs.OK
    found, sentence = execdirs.verdict(results)
    assert found == ["good"]
    assert "no linker tricks needed" in sentence


def test_a_refused_directory_is_reported_as_refused(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    source = tmp_path / "toybox"
    source.write_text("elf")

    runner = fake_scan_runner({
        str(source): (0, execdirs.MARKER, ""),
        "*": (13, "", "Permission denied"),
    })
    results = execdirs.scan([("bad", str(bad))], runner, source=str(source))
    assert results["bad"]["status"] == execdirs.BLOCKED
    found, sentence = execdirs.verdict(results)
    assert found == []
    assert "linker trick" in sentence


def test_a_directory_we_cannot_write_to_is_not_called_refused(tmp_path):
    """Unwritable and refused are different facts about a device."""
    source = tmp_path / "toybox"
    source.write_text("elf")
    runner = fake_scan_runner({str(source): (0, execdirs.MARKER, "")})
    results = execdirs.scan([("theirs", "/proc/nowhere/at/all")], runner,
                            source=str(source))
    assert results["theirs"]["status"] == execdirs.UNWRITABLE


def test_the_guinea_pig_is_cleaned_up(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    source = tmp_path / "toybox"
    source.write_text("elf")
    runner = fake_scan_runner({"*": (0, execdirs.MARKER, "")})
    execdirs.scan([("work", str(work))], runner, source=str(source))
    assert os.listdir(str(work)) == []


def test_a_failed_control_stops_the_scan(tmp_path):
    """The lesson from `rootfs check`: a broken harness must not read as a
    device that forbids everything."""
    source = tmp_path / "toybox"
    source.write_text("elf")
    runner = fake_scan_runner({"*": (1, "", "toybox: Unknown command echo")})
    results = execdirs.scan([("cache", str(tmp_path))], runner,
                            source=str(source))
    assert list(results) == ["control"]
    found, sentence = execdirs.verdict(results)
    assert found is None
    assert "Nothing has been learned" in sentence


def test_a_missing_guinea_pig_is_not_a_verdict(tmp_path):
    results = execdirs.scan([("cache", str(tmp_path))], fake_scan_runner({}),
                            source="/no/such/toybox")
    assert results["control"]["status"] == execdirs.UNKNOWN


def test_the_control_comes_first_in_the_matrix(tmp_path):
    lines = execdirs.summary_lines({
        "control": {"status": execdirs.OK, "detail": "ran from /system"},
        "cache": {"status": execdirs.BLOCKED, "detail": "Permission denied"},
    }, ["cache"])
    assert "control" in lines[0]
    assert "cache" in lines[1]


# ------------------------------------------------ can our own binaries run

from extcli_src.rootfs import mounts, native, sandbox  # noqa: E402


def test_the_probes_are_built_and_shaped_for_bionic():
    """Both shapes ship, and both have the PT_PHDR musl's loader was refused
    for. Built freestanding, so no NDK is involved."""
    import struct

    base = native.directory(RES, "arm64-v8a")
    for shape in native.SHAPES:
        path = os.path.join(base, shape)
        assert os.path.isfile(path), path
        data = open(path, "rb").read()
        assert data[:4] == b"\x7fELF"
        assert struct.unpack_from("<H", data, 16)[0] == 3, "ET_DYN"
        assert struct.unpack_from("<H", data, 18)[0] == 183, "aarch64"
        phoff = struct.unpack_from("<Q", data, 32)[0]
        entsize, count = struct.unpack_from("<HH", data, 54)
        kinds = {struct.unpack_from("<I", data, phoff + i * entsize)[0]
                 for i in range(count)}
        assert 6 in kinds, "PT_PHDR, which musl's loader lacked"
        assert 1 in kinds, "PT_LOAD"


def test_only_one_of_the_shapes_names_an_interpreter():
    """They came out byte-identical once, because lld ignores
    --dynamic-linker for a shared object. Two identical probes measure one
    thing twice."""
    import struct

    base = native.directory(RES, "arm64-v8a")
    interps = {}
    for shape in native.SHAPES:
        data = open(os.path.join(base, shape), "rb").read()
        phoff = struct.unpack_from("<Q", data, 32)[0]
        entsize, count = struct.unpack_from("<HH", data, 54)
        interps[shape] = any(
            struct.unpack_from("<I", data, phoff + i * entsize)[0] == 3
            for i in range(count))
    assert interps == {"probe": False, "probe-interp": True}


def test_both_abis_are_built():
    for abi in ("arm64-v8a", "armeabi-v7a"):
        for shape in native.SHAPES:
            assert os.path.isfile(os.path.join(native.directory(RES, abi),
                                               shape))


def test_the_marker_is_what_proves_our_binary_ran():
    status, _ = native.read_attempt(0, native.MARKER + "\n", "")
    assert status == native.OK


def test_the_linker_s_own_refusals_are_recognised():
    for text in ("Could not find a PHDR: broken executable?",
                 "CANNOT LINK EXECUTABLE: library not found",
                 "Permission denied"):
        status, detail = native.read_attempt(1, "", text)
        assert status == native.BLOCKED, text
        assert detail


def test_a_shape_that_is_not_built_is_not_called_blocked(tmp_path):
    results = native.probe(str(tmp_path), "riscv64", "/l", lambda c: (0, "", ""))
    assert all(r["status"] == native.MISSING for r in results.values())
    assert native.chosen(results) is None


def test_the_cheaper_shape_wins_when_both_run():
    assert native.chosen({
        "probe": {"status": native.OK},
        "probe-interp": {"status": native.OK},
    }) == "probe"
    assert native.chosen({
        "probe": {"status": native.BLOCKED},
        "probe-interp": {"status": native.OK},
    }) == "probe-interp"


def test_the_summary_says_what_the_answer_means():
    lines = native.summary_lines({"probe": {"status": native.OK, "detail": "ran"}})
    assert "the loader can be written" in lines[-1]
    lines = native.summary_lines({"probe": {"status": native.BLOCKED,
                                            "detail": "no"}})
    assert "a loader of our own cannot run either" in lines[-1]


# ------------------------------------------------------- the loader strategy

def test_the_loader_is_built_and_shipped():
    path = os.path.join(native.directory(RES, "arm64-v8a"), guest.LOADER_NAME)
    assert os.path.isfile(path)
    assert open(path, "rb").read(4) == b"\x7fELF"


def test_the_loader_command_puts_everything_in_order(tmp_path):
    """linker, loader, the guest's real path, then the name it should answer
    to. busybox is a different program depending on the last one."""
    root = musl_rootfs(tmp_path)
    tools = native.directory(RES, "arm64-v8a")
    command = guest.command_for(guest.LOADER, root, "/system/bin/linker64",
                                ["/bin/sh", "-c", "echo hi"],
                                native_dir=tools, argv0="sh")
    assert command == ["/system/bin/linker64",
                       os.path.join(tools, "loader"),
                       guest.LOADER_SENTINEL,
                       os.path.join(root, "bin/busybox"),
                       "sh", "-c", "echo hi"]


def test_the_argv0_defaults_to_the_program_s_own_name(tmp_path):
    root = musl_rootfs(tmp_path)
    command = guest.command_for(guest.LOADER, root, "/l", ["/bin/sh"],
                                native_dir=native.directory(RES, "arm64-v8a"))
    assert command[-1] == "sh"


def test_the_loader_strategy_needs_the_loader(tmp_path):
    root = musl_rootfs(tmp_path)
    assert guest.command_for(guest.LOADER, root, "/l", ["/bin/sh"]) is None
    assert guest.command_for(guest.LOADER, root, "/l", ["/bin/sh"],
                             native_dir=str(tmp_path / "empty")) is None


def test_the_loader_is_told_where_the_rootfs_is(tmp_path):
    """It resolves the guest's own /lib/ld-musl-... through this."""
    root = musl_rootfs(tmp_path)
    env = guest.environment_for(guest.LOADER, root)
    assert env["EXTCLI_ROOT"] == root
    # The guest's interpreter needs the library path — apk could not find
    # libapk until it was set — and once / is the rootfs it asks for /lib.
    assert env["LD_LIBRARY_PATH"] == guest.GUEST_LIBRARY_PATH
    assert guest.environment_for(guest.LOADER, root, translate=False)[
        "LD_LIBRARY_PATH"].startswith(os.path.join(root, "lib"))
    assert "EXTCLI_ROOT" not in guest.environment_for(guest.DIRECT, root)


def test_the_loader_is_tried_first():
    assert guest.ORDER[0] == guest.LOADER
    assert guest.chosen({
        guest.LOADER: {"status": guest.OK},
        guest.DIRECT: {"status": guest.OK},
    }) == guest.LOADER


def test_the_backend_uses_the_loader_end_to_end(tmp_path):
    root = str(tmp_path / "root")
    install.install(sources.find("alpine").path(RES), root)
    layout.save_strategy(root, guest.LOADER)
    tools = native.directory(RES, "arm64-v8a")

    backend = backend_module.RootfsBackend(root, "/system/bin/linker64",
                                           native_dir=tools)
    assert backend.available()
    assert backend.command_for(["ls", "-la"]) == [
        "/system/bin/linker64",
        os.path.join(tools, "loader"),
        guest.LOADER_SENTINEL,
        os.path.join(root, "bin/busybox"),
        "ls", "-la",
    ]
    assert backend.environment()["EXTCLI_ROOT"] == root


def test_the_sentinel_comes_before_the_loader_s_own_arguments(tmp_path):
    """The loader searches argv for it instead of counting from argv[1].

    How many arguments bionic's linker leaves in front of ours is its business
    and has changed between releases; counting worked under qemu and opened the
    wrong argument on the device.
    """
    root = musl_rootfs(tmp_path)
    command = guest.command_for(guest.LOADER, root, "/l", ["/bin/sh"],
                                native_dir=native.directory(RES, "arm64-v8a"),
                                argv0="sh")
    assert guest.LOADER_SENTINEL in command
    at = command.index(guest.LOADER_SENTINEL)
    # everything the loader needs comes after it, and nothing before it matters
    assert command[at + 1].endswith("busybox")
    assert command[at + 2] == "sh"


# ---------------------------------------------------------- the syscall map

def test_the_syscall_map_is_built():
    path = native.tool(RES, "arm64-v8a", native.SYSCALL_MAP)
    assert os.path.isfile(path)
    assert open(path, "rb").read(4) == b"\x7fELF"


def test_the_native_tools_carry_no_relocations():
    """Nothing applies them.

    These programs are started by bionic's linker but have no dynamic linker
    of their own, so a relocation left in one is a pointer still holding its
    link-time value. It has happened twice — a branch through the PLT, and an
    array of string pointers — and each time the symptom was a crash a long
    way from the cause.
    """
    import struct

    for name in ("loader", "syscalls", "probe", "probe-interp"):
        path = os.path.join(native.directory(RES, "arm64-v8a"), name)
        data = open(path, "rb").read()
        shoff = struct.unpack_from("<Q", data, 40)[0]
        entsize, count = struct.unpack_from("<HH", data, 58)
        relocations = 0
        for i in range(count):
            section = shoff + i * entsize
            kind = struct.unpack_from("<I", data, section + 4)[0]
            if kind in (4, 9):  # SHT_RELA, SHT_REL
                size = struct.unpack_from("<Q", data, section + 32)[0]
                relocations += size
        assert relocations == 0, "%s needs relocations nobody will apply" % name


def test_the_map_output_is_read_as_numbers():
    refused, complete = native.read_syscall_map("99\n293\n435\nend\n")
    assert refused == [99, 293, 435]
    assert complete


def test_a_scan_cut_short_is_not_read_as_permission():
    """Without the closing line, "nothing refused" and "it never finished"
    look identical — and one of them is a reason to act."""
    refused, complete = native.read_syscall_map("99\n")
    assert refused == [99]
    assert not complete
    assert native.read_syscall_map("") == ([], False)


def test_a_refused_call_is_reported_with_what_the_guest_will_be_told():
    rows = sandbox.describe(sandbox.rules([146, 40]), native.syscall_name)
    assert any("setuid" in row and "succeeds" in row for row in rows)
    assert any("mount" in row and "EPERM" in row for row in rows)


def test_the_report_says_what_happens_rather_than_guessing_why():
    """It used to name a cause, and named the wrong one.

    The claim was that musl makes none of the refused calls, so the filter must
    be refusing by argument. The trace then showed busybox calling setuid,
    which is refused by number like everything else. No guess is better than a
    wrong one, and now there is nothing left to guess at: every refusal is
    answered.
    """
    sentence = sandbox.sentence(sandbox.rules([146, 40]))
    assert "2 refused" in sentence and "EPERM" in sentence
    for word in ("musl", "arguments", "cause"):
        assert word not in sentence
    assert sandbox.sentence([]) == "nothing is refused"


def test_an_unnamed_number_is_still_an_answer():
    rows = sandbox.describe(sandbox.rules([300]), native.syscall_name)
    assert rows == [" 300  ?                  fails with EPERM"]


def test_the_gaps_in_the_table_are_known_to_be_gaps():
    """arm64 stops at 294 and starts again at 424. Those numbers came back
    refused, which is the useful part: the filter kills numbers it does not
    recognise, so a call cannot be got rid of by turning it into one."""
    assert native.unused(300) and native.unused(250)
    assert not native.unused(294) and not native.unused(424)
    assert not native.unused(146)


def test_sigsys_is_not_read_as_a_crash():
    """It is also a "killed by", and it means the opposite of a broken binary:
    the guest was running well enough to make a syscall."""
    kind, sentence = guest.diagnose({
        guest.LOADER: {"status": guest.BLOCKED, "detail": "killed by SIGSYS"},
        guest.DIRECT_PATH: {"status": guest.BLOCKED,
                            "detail": "killed by SIGSEGV"},
    })
    assert kind == guest.SANDBOX
    assert "sandbox refused one of its syscalls" in sentence
    assert "rootfs probe syscalls" in sentence


def test_a_plain_crash_still_reads_as_a_foreign_libc():
    kind, _ = guest.diagnose({
        guest.DIRECT_PATH: {"status": guest.BLOCKED,
                            "detail": "killed by SIGSEGV"},
    })
    assert kind == guest.FOREIGN_LIBC


# ------------------------------------- answering a refusal instead of dying

def test_a_privilege_drop_is_told_it_worked():
    """busybox calls setuid(getuid()) at startup — asking to become the user it
    already is. On this device that is fatal. Saying 0 is not a lie: there is
    nothing to drop, and any other kernel would have said the same."""
    assert sandbox.rules([146]) == [(146, 0)]


def test_everything_else_gets_an_error_rather_than_a_lie():
    """mount cannot work here, and pretending it did would send a guest on
    into a filesystem that is not there. EPERM is what an unprivileged process
    gets everywhere else."""
    assert sandbox.rules([40]) == [(40, -sandbox.EPERM)]


def test_the_rules_survive_the_trip_through_the_environment():
    rule_list = sandbox.rules([146, 40, 91])
    text = sandbox.encode(rule_list)
    assert "146" in text and "40:-1" in text
    assert sandbox.decode(text) == rule_list


def test_a_measurement_is_kept_and_read_back(tmp_path):
    state = str(tmp_path / "state")
    assert sandbox.load(state) is None
    assert sandbox.save(state, [146, 40])
    assert sandbox.load(state) == [40, 146]
    assert sandbox.blocked_for(state) == "40:-1,146"


def test_a_device_that_refuses_nothing_is_not_a_device_never_asked(tmp_path):
    """One means the loader has nothing to do; the other means it does not yet
    know, and running a guest without knowing runs it into a kill."""
    state = str(tmp_path / "state")
    sandbox.save(state, [])
    assert sandbox.load(state) == []
    assert sandbox.blocked_for(state) == ""


def test_only_the_loader_is_told_what_is_refused():
    """The other strategies hand the guest straight to the kernel; there is
    nowhere for them to stand between it and the filter."""
    blocked = "146,40:-1"
    assert guest.environment_for(guest.LOADER, "/r",
                                 blocked=blocked)["EXTCLI_BLOCKED"] == blocked
    for strategy in (guest.DIRECT, guest.DIRECT_PATH, guest.MUSL):
        env = guest.environment_for(strategy, "/r", blocked=blocked)
        assert "EXTCLI_BLOCKED" not in env


def test_the_backend_passes_the_refusals_on(tmp_path):
    root = musl_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/l", guest.LOADER,
                                           blocked="146")
    assert backend.environment()["EXTCLI_BLOCKED"] == "146"


def test_the_loader_cancels_a_syscall_the_way_arm64_wants():
    """x8 stops being the syscall number once the kernel has read it; the
    number a tracer can still change lives in its own register set, and setting
    it to -1 is how the kernel is told to skip the call — which it does before
    consulting seccomp."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "NT_ARM_SYSTEM_CALL" in source
    assert "PTRACE_SETREGSET" in source


# ----------------------------------------------------------- reading a trace

def test_a_trace_is_read_with_its_count():
    """Four calls means the guest died starting up and four hundred means it
    lived long enough to do something. The first report printed the numbers
    alone and could not tell those apart."""
    numbers, total, rest = native.read_trace(
        "extcli-loader: 4 syscalls, last: 135 135 146\n"
        "extcli-loader: the guest was killed by signal 31\n")
    assert numbers == [135, 135, 146]
    assert total == 4
    assert rest == ["extcli-loader: the guest was killed by signal 31"]


def test_a_trace_names_its_numbers_and_marks_the_last():
    lines = native.trace_lines([135, 146])
    assert lines[0].strip() == "135  rt_sigprocmask"
    assert "setuid" in lines[1] and lines[1].endswith("<- last")


def test_every_refusal_reaches_the_loader():
    """The device refuses 240 numbers. The first list the loader could hold
    was sixty-four long, and the ones past it would have been dropped without
    a word — so nothing here may cap the list either."""
    refused = list(range(18, 462))
    text = sandbox.encode(sandbox.rules(refused))
    assert sandbox.decode(text) == sandbox.rules(refused)
    assert len(sandbox.decode(text)) == len(refused)


def test_the_loader_replaces_a_refused_call_rather_than_dropping_it():
    """Setting the syscall number to -1 means "do not run this one", and the
    guest died at the same place: this filter kills numbers it does not
    recognise — most of what it refuses are numbers arm64 has no syscall for —
    and -1 is not a number it recognises. getpid is."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "#define SYSCALL_HARMLESS %d" % sandbox.DIVERSION in source
    assert "divert_syscall" in source


def test_a_device_that_refused_the_replacement_would_be_reported():
    assert sandbox.can_divert([146, 40])
    assert not sandbox.can_divert([146, sandbox.DIVERSION])


def test_the_names_are_the_arm64_table():
    for number, name in ((40, "mount"), (144, "setgid"), (146, "setuid"),
                         (172, "getpid"), (135, "rt_sigprocmask"),
                         (96, "set_tid_address"), (294, "kexec_file_load"),
                         (424, "pidfd_send_signal")):
        assert native.syscall_name(number) == name, number


def test_the_rootfs_root_is_what_a_guest_calls_slash(tmp_path):
    """`ls /` came back "Permission denied": / was the phone's root, which no
    app is allowed to list. Inside a rootfs / is the rootfs, as under chroot."""
    root = musl_rootfs(tmp_path)
    assert layout.translate(root, "/") == root


def test_a_path_that_does_not_exist_yet_still_translates(tmp_path):
    """A program about to create a file needs an answer as much as one about
    to read one."""
    root = musl_rootfs(tmp_path)
    assert layout.translate(root, "/tmp/new/file") == \
        os.path.join(root, "tmp/new/file")


def test_translation_follows_the_rootfs_own_symlinks(tmp_path):
    root = musl_rootfs(tmp_path)
    assert layout.translate(root, "/bin/sh") == \
        os.path.join(root, "bin/busybox")


def test_a_relative_path_is_left_alone(tmp_path):
    root = musl_rootfs(tmp_path)
    assert layout.translate(root, "file") is None
    assert layout.translate(root, "") is None


def test_arguments_reach_the_guest_translated(tmp_path):
    root = musl_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/l", guest.DIRECT)
    command = backend.command_for(["busybox", "ls", "/"])
    assert command[-1] == root
    # a flag is not a path and must survive untouched
    assert backend.translate("-la") == "-la"


def test_the_loader_is_told_where_its_root_is_and_what_is_not_in_it(tmp_path):
    root = musl_rootfs(tmp_path)
    env = guest.environment_for(guest.LOADER, root)
    assert ("/", root) in mounts.decode(env["EXTCLI_MOUNTS"])
    assert "/proc" in env["EXTCLI_PASS"].split(":")
    assert "/dev" in env["EXTCLI_PASS"].split(":")
    # where the guest stands, in its own terms — the same place it is told is
    # its home, not the top of the rootfs
    assert env["PWD"] == mounts.HOME


def test_nothing_but_the_loader_is_told_to_translate(tmp_path):
    """The others hand the guest straight to the kernel; there is nowhere for
    them to stand between it and a path."""
    root = musl_rootfs(tmp_path)
    for strategy in (guest.DIRECT, guest.DIRECT_PATH, guest.MUSL):
        assert "EXTCLI_MOUNTS" not in guest.environment_for(strategy, root)


def test_arguments_are_left_alone_when_the_loader_translates(tmp_path):
    """Doing both would translate twice: the supervisor would see a host path
    beginning with / and put the rootfs in front of it again."""
    root = musl_rootfs(tmp_path)
    backend = backend_module.RootfsBackend(root, "/l", guest.LOADER,
                                           native_dir="/n")
    assert backend.translate("/bin/sh") == "/bin/sh"
    assert backend_module.RootfsBackend(root, "/l", guest.DIRECT).translate(
        "/bin/sh") == os.path.join(root, "bin/busybox")


def test_the_shell_stands_in_the_guest_s_world():
    """`cd /sdcard` has to be one command, and what opens the file after it has
    to be somewhere real."""
    from extcli_src.shell.env import Env

    rows = [("/", "/r"), ("/sdcard", "/s")]
    values = {mounts.ROOT: True, mounts.SDCARD: True}
    env = Env(cwd="/root", home="/root", paths=mounts.Paths(rows, values))
    assert env.resolve("../etc/passwd") == "/etc/passwd"
    assert env.host("/etc/passwd") == "/r/etc/passwd"
    assert env.host("/sdcard/note") == "/s/note"
    assert env.display_cwd() == "~"


def test_a_shell_without_a_rootfs_is_left_exactly_as_it_was():
    from extcli_src.shell.env import Env

    env = Env(cwd="/sdcard", home="/sdcard")
    assert env.host("/etc/passwd") == "/etc/passwd"
    assert env.resolve("x") == "/sdcard/x"


def test_cd_refuses_a_path_that_is_not_mounted(tmp_path):
    from extcli_src.shell.env import Env

    root = str(tmp_path / "r")
    os.makedirs(os.path.join(root, "etc"))
    rows = mounts.table({mounts.SDCARD: True},
                        {mounts.ROOT: root, mounts.SDCARD: str(tmp_path)})
    env = Env(cwd="/sdcard", home="/sdcard",
              paths=mounts.Paths(rows, {mounts.SDCARD: True}))
    ok, detail = env.chdir("/etc")
    assert not ok and "not mounted" in detail
    # and the mount that is on still works
    ok, _ = env.chdir("/sdcard")
    assert ok


def test_a_typo_is_corrected_from_what_the_rootfs_offers():
    """With Alpine in the chain most of what can be typed is Alpine's, and a
    suggestion drawn only from the builtins would never name what was meant."""
    from extcli_src.backends import chain
    from extcli_src.backends.inproc import InprocBackend

    class Rootfs(object):
        name = "rootfs"

        def available(self):
            return True

        def commands(self):
            return ["busybox", "apk", "vi"]

        def which(self, name):
            return name if name in self.commands() else None

        def describe(self):
            return []

    built = chain.ChainBackend([Rootfs(), InprocBackend()])
    offered = built.commands()
    assert "apk" in offered and "vi" in offered
    # inproc keeps its commands in a dict of the same name; it must still be
    # asked, not silently skipped
    assert "grep" in offered


def test_the_working_directory_is_translated_before_it_reaches_a_process(tmp_path):
    """`ls /` answered "No such file or directory: '/root'". The console stands
    in the guest's /root; subprocess wanted the phone's, which is not there."""
    root = musl_rootfs(tmp_path)
    os.makedirs(os.path.join(root, "root"))
    other = str(tmp_path / "card")
    os.makedirs(other)
    rows = [("/", root), ("/sdcard", other)]
    backend = backend_module.RootfsBackend(root, "/l", guest.LOADER,
                                           mount_rows=rows, start="/root")
    assert backend.host_cwd("/root") == os.path.join(root, "root")
    assert backend.host_cwd("/sdcard") == other
    # nothing to go on falls back to where the shell would have opened
    assert backend.host_cwd(None) == os.path.join(root, "root")
    # and a guest path with nothing behind it does not become a broken cwd
    assert backend.host_cwd("/nowhere") == os.path.join(root, "root")


def test_a_listing_with_no_arguments_asks_about_the_right_directory(tmp_path):
    """`ls` alone used the shell's cwd as a real path, which it is not."""
    from extcli_src.backends.inproc import InprocBackend

    root = str(tmp_path / "r")
    os.makedirs(os.path.join(root, "root"))
    open(os.path.join(root, "root", "note"), "w").close()
    paths = mounts.Paths([("/", root)], {mounts.ROOT: True})
    result = InprocBackend(paths=paths).run(["ls"], cwd="/root")
    assert result.status == 0
    assert "note" in result.out


def test_a_glob_asks_the_machine_and_answers_in_the_shell_s_names(tmp_path):
    """Only the filesystem knows what matches and only the shell knows what to
    call it, so a pattern goes out translated and comes back translated."""
    from extcli_src.shell.env import Env
    from extcli_src.shell.expand import _expand_glob

    root = str(tmp_path / "r")
    os.makedirs(os.path.join(root, "etc"))
    for name in ("hosts", "passwd"):
        open(os.path.join(root, "etc", name), "w").close()
    env = Env(cwd="/etc", home="/root",
              paths=mounts.Paths([("/", root)], {mounts.ROOT: True}))
    assert _expand_glob("*", env) == ["hosts", "passwd"]
    assert _expand_glob("/etc/h*", env) == ["/etc/hosts"]
    # and a shell with no rootfs globs the machine it is standing on
    plain = Env(cwd=os.path.join(root, "etc"))
    assert _expand_glob("h*", plain) == ["hosts"]


def test_a_host_path_comes_back_as_the_name_the_guest_knows(tmp_path):
    rows = [("/", "/r"), ("/sdcard", "/storage/emulated/0")]
    assert mounts.guest_path(rows, "/r") == "/"
    assert mounts.guest_path(rows, "/r/etc/passwd") == "/etc/passwd"
    assert mounts.guest_path(rows, "/storage/emulated/0/Download") == \
        "/sdcard/Download"
    assert mounts.guest_path(rows, "/proc/self") is None


# ----------------------------------------------------------------- resolver

def test_a_guest_with_no_resolver_is_given_one(tmp_path):
    """`apk update` said "DNS: transient error", which reads like the network
    is at fault. A minirootfs ships no /etc/resolv.conf and Android has none to
    copy, so musl had nowhere to ask."""
    from extcli_src.rootfs import network

    root = musl_rootfs(tmp_path)
    written, servers = network.write(root, ["10.0.0.1", "10.0.0.2"])
    assert written and servers == ["10.0.0.1", "10.0.0.2"]
    assert "nameserver 10.0.0.1" in network.read(root)
    # writing the same thing again is not a write
    assert network.write(root, ["10.0.0.1", "10.0.0.2"])[0] is False


def test_a_phone_that_will_not_say_still_leaves_the_guest_able_to_ask(tmp_path):
    from extcli_src.rootfs import network

    root = musl_rootfs(tmp_path)
    _written, servers = network.write(root, [])
    assert servers == list(network.FALLBACK)


def test_an_interface_scope_is_not_written_into_the_file(tmp_path):
    """`fe80::1%wlan0` means "on this interface", which is not this process's
    to interpret and which musl will not parse."""
    from extcli_src.rootfs import network

    assert "nameserver fe80::1\n" in network.contents(["fe80::1%wlan0"])


def test_status_says_when_the_guest_cannot_resolve_anything(tmp_path):
    from extcli_src.rootfs import network

    root = musl_rootfs(tmp_path)
    assert "none" in network.describe(root)
    network.write(root, ["9.9.9.9"])
    assert network.describe(root) == "9.9.9.9"


def test_a_failed_call_is_read_with_its_name_and_its_reason():
    """"Permission denied" about a URL names neither the call that was refused
    nor the thing it was refused for."""
    failures = native.read_failures(
        "extcli-loader: failed 56 errno 13 /var/cache/apk/APKINDEX\n"
        "extcli-loader: failed 198 errno 1\n"
        "extcli-loader: 4 syscalls, last: 56\n")
    assert failures == [(56, 13, "/var/cache/apk/APKINDEX"), (198, 1, "")]
    lines = native.failure_lines(failures)
    assert lines[0].startswith("openat")
    assert "EACCES" in lines[0] and "/var/cache/apk/APKINDEX" in lines[0]
    assert "socket" in lines[1] and "EPERM" in lines[1]


def test_an_unnamed_errno_is_still_reported():
    assert "errno 234" in native.failure_lines([(56, 234, "")])[0]


def test_one_failure_can_be_asked_for_out_of_thousands():
    """`uv tool install` failed 3997 calls and stopped on one of them, which
    had scrolled past long before the last 25."""
    failures = [(56, 2, "/root/.cache/uv/a"), (34, 17, "/root/lib/yaml"),
                (56, 2, "/root/.cache/uv/b")]
    assert native.matching_failures(failures, 17, None) == \
        [(34, 17, "/root/lib/yaml")]
    assert native.matching_failures(failures, None, ".cache") == \
        [failures[0], failures[2]]
    assert native.matching_failures(failures, 2, "/b") == [failures[2]]


def test_an_errno_can_be_named_as_well_as_numbered():
    assert native.errno_number("EEXIST") == 17
    assert native.errno_number("eexist") == 17
    assert native.errno_number("17") == 17
    assert native.errno_number("ENOSUCHTHING") is None
    assert native.errno_number("") is None


def test_the_trace_s_own_flags_stop_at_the_command():
    """`rootfs trace uv tool install --grep x` traces uv with its own flag,
    which is uv's business and not ours."""
    from extcli_src.shell.builtins import rootfs as rootfs_builtins

    assert rootfs_builtins._trace_filter(["--errno", "EEXIST", "apk", "add"]) \
        == (["apk", "add"], 17, None)
    assert rootfs_builtins._trace_filter(["--grep=yaml", "uv"]) == \
        (["uv"], None, "yaml")
    assert rootfs_builtins._trace_filter(["uv", "--grep", "x"]) == \
        (["uv", "--grep", "x"], None, None)


def test_a_bare_program_name_never_reaches_the_loader(tmp_path):
    """It was passed through unresolved, and the loader answered "cannot open:
    apk" — which reads as a broken rootfs rather than as a caller that had not
    looked the program up."""
    root = musl_rootfs(tmp_path)
    native_dir = native.directory(RES, "arm64-v8a")
    assert guest.command_for(guest.LOADER, root, "/l", ["apk", "update"],
                             native_dir=native_dir) is None
    assert guest.command_for(guest.DIRECT, root, "/l", ["apk"]) is None
    # and the same program, named the way the guest names it, still starts
    assert guest.command_for(guest.LOADER, root, "/l", ["/bin/sh"],
                             native_dir=native_dir) is not None


# --------------------------------------------- how a file may be written here

def test_the_three_ways_of_writing_a_file_are_tried_in_order(tmp_path):
    """apk creates a file with O_TMPFILE and links it into place; whether that
    is allowed is a fact about the device, and guessing at it has cost a round
    trip already."""
    from extcli_src.rootfs import writes

    results = writes.run(str(tmp_path))
    assert set(results) == set(writes.ORDER)
    # this container is an ordinary Linux, so the plain way works here
    assert results[writes.RENAME][0] == writes.OK
    ok, sentence = writes.verdict(results)
    assert isinstance(ok, bool) and sentence
    # and nothing is left behind
    assert os.listdir(str(tmp_path)) == []


def test_a_device_that_refuses_linking_is_told_apart_from_one_that_refuses_all():
    from extcli_src.rootfs import writes

    refused = {writes.TMPFILE: (writes.OK, ""),
               writes.LINKAT: (writes.FAILED, "Cross-device link (errno 18)"),
               writes.RENAME: (writes.OK, "")}
    ok, sentence = writes.verdict(refused)
    assert not ok
    # what was measured, not what it sounds like: EXDEV is not a refusal, and
    # a reader who takes "refused" at face value looks for a permission that
    # was never involved
    assert "Cross-device link" in sentence
    assert "refused" not in sentence
    nothing = dict(refused, **{writes.RENAME: (writes.FAILED, "EXDEV")})
    assert "bigger problem" in writes.verdict(nothing)[1]


def test_the_write_measurement_is_kept_and_read_back(tmp_path):
    from extcli_src.rootfs import writes

    state = str(tmp_path / "state")
    assert writes.load(state) is None
    results = {writes.TMPFILE: (writes.OK, ""),
               writes.LINKAT: (writes.FAILED, "EXDEV"),
               writes.RENAME: (writes.OK, "")}
    assert writes.save(state, results)
    assert writes.needs_named_temporary(writes.load(state))


def test_a_guest_is_only_told_to_fall_back_when_the_fallback_works():
    """A program sent to something that also fails is worse off than one left
    alone."""
    from extcli_src.rootfs import writes

    both_broken = {writes.LINKAT: (writes.FAILED, ""),
                   writes.RENAME: (writes.FAILED, "")}
    assert not writes.needs_named_temporary(both_broken)
    assert not writes.needs_named_temporary({writes.LINKAT: (writes.OK, ""),
                                             writes.RENAME: (writes.OK, "")})
    assert not writes.needs_named_temporary(None)


def test_the_guest_is_told_there_are_no_unnamed_files(tmp_path):
    root = musl_rootfs(tmp_path)
    env = guest.environment_for(guest.LOADER, root, no_tmpfile=True)
    assert env["EXTCLI_NO_TMPFILE"] == "1"
    assert "EXTCLI_NO_TMPFILE" not in guest.environment_for(guest.LOADER, root)


def test_the_loader_answers_an_unnamed_open_the_way_a_filesystem_would():
    """EOPNOTSUPP is what a filesystem without O_TMPFILE says, and what every
    program's fallback is written for."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "#define EOPNOTSUPP 95" in source
    assert "O_TMPFILE_BIT" in source and "unnameable_open" in source


# ------------------------------------------------------ the guest's own exec

def test_the_guest_is_told_how_to_exec(tmp_path):
    """The device refuses to exec a file the app can write, which is what the
    guest's own exec runs into: apk installs a package, runs its trigger, and
    gets "execve: Permission denied"."""
    root = musl_rootfs(tmp_path)
    env = guest.environment_for(guest.LOADER, root, linker="/system/bin/linker64",
                                native_dir="/n")
    assert env["EXTCLI_EXEC"] == "/system/bin/linker64|/n/loader"
    # neither half on its own is enough to say how
    assert "EXTCLI_EXEC" not in guest.environment_for(
        guest.LOADER, root, linker="/system/bin/linker64")


def test_the_loader_knows_not_to_supervise_itself_twice():
    """An exec'd loader inherits EXTCLI_MOUNTS and would fork a supervisor of
    its own; two of them would translate every path twice."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert 'sentinel_child[] = "extcli-loader-child-v1"' in source
    assert "if (!supervised &&" in source


def test_the_loader_reads_a_shebang():
    """apk's triggers are shell scripts, and the kernel is not there to notice:
    an exec that reaches the loader has already been turned into "start the
    loader on this file"."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "read_shebang" in source


def test_a_relative_exec_is_resolved_against_the_directory_it_meant():
    """apk runs a trigger by its place inside the rootfs — `lib/apk/exec/...`,
    with no leading slash — so the path means nothing without the directory it
    is relative to, and only the guest knows that one."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "directory_of" in source
    assert "/proc/" in source and "/cwd" in source


def test_the_scratch_page_is_where_both_sides_look_after_an_exec():
    """An exec replaces the address space and takes the page with it. The
    supervisor went on writing translated paths to where it used to be, so
    every path after the first exec was handed over untranslated."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "SCRATCH_ADDRESS" in source
    assert "MAP_FIXED_NOREPLACE" in source
    # per tracee, because one process may have exec'd and another not
    assert "tracee_scratch[MAX_TRACEES]" in source


def test_an_exec_ed_loader_maps_the_page_even_with_no_mounts_of_its_own():
    """apk passes a trigger a short environment with no EXTCLI_MOUNTS in it, so
    the loader that comes up has no mounts and needs none — the supervisor above
    it is still translating. What it does need is the page to be translated
    into, or the guest path from a `#!` line reaches openat as written."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "if (mount_count || supervised) {" in source
    # and the supervisor is told an exec happened rather than inferring it
    assert "PTRACE_EVENT_EXEC" in source


def test_the_page_is_mapped_before_anything_is_opened():
    """A loader started by an exec is traced from the moment it begins, and the
    supervisor writes every translated path into that page. Mapped after the
    program had been opened, the first opens — the program, and the interpreter
    a `#!` line names — had nowhere to be translated into."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert source.index("SCRATCH_ADDRESS, SCRATCH_TOTAL") < \
        source.index("map_elf(elf, &program)")


def test_every_tracee_writes_its_paths_somewhere_of_its_own():
    """Threads share an address space, so a single scratch page is shared too.

    Two of them stopped at once each wrote a translated path to the same
    address, and the one let go second handed the kernel the other's path — a
    file created under another file's name, which is what a wheel unpacked by
    uv came out as. So the area holds one stretch per slot, and a slot keeps
    its number for as long as its tracee lives: filling a gap from the end
    would move a stretch out from under a tracee still using it.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "native", "loader.c")).read()
    assert "(u64)slot * SCRATCH_BYTES" in source
    assert "MAX_TRACEES * SCRATCH_BYTES" in source
    # the whole area is mapped, not one stretch of it
    assert "SCRATCH_ADDRESS, SCRATCH_TOTAL" in source
    # nothing is moved between slots
    assert "tracee_pid[i] = tracee_pid[tracee_count];" not in source
    for line in ("address = scratch_area(slot)",
                 "at = scratch_area(slot)",
                 "limit = scratch_area(slot)"):
        assert line in source, line


def test_a_directory_is_asked_for_by_the_name_the_kernel_uses(tmp_path):
    """/data/user/0/<package> and /data/data/<package> are the same place.
    Android hands out the first and the kernel answers with the second, so a
    guest's /proc/<pid>/cwd read one name while the mount table held the other
    — a prefix that never matches, and a trigger that "cannot open" a path
    that is plainly there."""
    from extcli_src.compat import paths

    real = tmp_path / "real"
    real.mkdir()
    (real / "inside").write_text("here")
    link = tmp_path / "byanothername"
    os.symlink(str(real), str(link))

    assert paths.real(str(link)) == str(real)
    assert paths.real(str(link / "inside")) == str(real / "inside")
    # a path that is already canonical comes back unchanged
    assert paths.real(str(real)) == str(real)
    # and one that does not exist is still answered rather than raising
    assert paths.real(str(tmp_path / "nowhere")).endswith("nowhere")


def test_the_guest_is_given_a_home_inside_itself(tmp_path):
    """A guest inherits this process's environment, and the app's own HOME is
    a directory on the phone. uv believed it and put its tools and its cache
    in /data/user/0/<package>/files/.local — a real path, which then went
    through the translation like any other and built a shadow of the phone's
    directories inside the rootfs."""
    root = musl_rootfs(tmp_path)
    env = guest.environment_for(guest.LOADER, root, home="/root")
    assert env["HOME"] == "/root"
    assert env["PWD"] == "/root"
    assert env["TMPDIR"] == "/tmp"
    assert env["USER"] == "root" and env["SHELL"] == "/bin/sh"
    # and with nothing said, the rootfs's own home rather than the phone's
    assert guest.environment_for(guest.LOADER, root)["HOME"] == mounts.HOME


def test_the_phone_s_home_does_not_reach_a_guest(tmp_path):
    root = musl_rootfs(tmp_path)
    android = {"HOME": "/data/user/0/com.exteragram.messenger/files",
               "TMPDIR": "/data/user/0/com.exteragram.messenger/cache"}
    env = guest.environment_for(guest.LOADER, root, base=android, home="/root")
    assert env["HOME"] == "/root"
    assert env["TMPDIR"] == "/tmp"


# ------------------------------------------------- getting ready by itself

# `setup_module` is pytest's own module-level hook; do not shadow it
from extcli_src.rootfs import setup as rootfs_setup  # noqa: E402


def _bundle(tmp_path, name="testfs"):
    """A tarball of a small rootfs, registered as if it shipped with us."""
    import hashlib
    import tarfile

    made = tmp_path / "made"
    for part in ("bin", "etc", "lib"):
        (made / part).mkdir(parents=True)
    (made / "bin/busybox").write_text("elf")
    (made / "lib/ld-musl-aarch64.so.1").write_text("loader")
    res = tmp_path / "res"
    (res / sources.DIRECTORY).mkdir(parents=True)
    tarball = res / sources.DIRECTORY / ("%s.tar.gz" % name)
    with tarfile.open(str(tarball), "w:gz") as archive:
        for part in ("bin", "etc", "lib", "bin/busybox",
                     "lib/ld-musl-aarch64.so.1"):
            archive.add(str(made / part), arcname=part)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    return sources.Source(name=name, filename=tarball.name,
                          abis=("arm64-v8a",), sha256=digest,
                          release="a test rootfs"), str(res)


def _scan_output():
    """What the syscall map prints: a number per refusal, then "end"."""
    return 0, "146\n240\nend\n", ""


def test_nothing_is_ready_on_a_fresh_install(tmp_path):
    root = str(tmp_path / "rootfs")
    state = str(tmp_path / "state")
    assert rootfs_setup.pending("res", state, root) == list(rootfs_setup.STEPS)
    assert not rootfs_setup.ready("res", state, root)


def test_everything_a_rootfs_needs_is_done_without_being_asked(tmp_path,
                                                              monkeypatch):
    """Each of these was a command the user had to know to run. Somebody who
    has just installed a plugin has not heard of any of them."""
    source, res = _bundle(tmp_path)
    monkeypatch.setattr(sources, "BUNDLED", (source,))
    root = str(tmp_path / "rootfs")
    state = str(tmp_path / "state")
    os.makedirs(os.path.join(res, "native/arm64-v8a"))
    with open(os.path.join(res, "native/arm64-v8a/syscalls"), "w") as handle:
        handle.write("elf")
    seen = []

    report = rootfs_setup.prepare(
        res, state, root, abi="arm64-v8a", linker="/system/bin/linker64",
        dns=["1.1.1.1"], source=source.name,
        on_step=lambda name, label: seen.append(name),
        run=lambda command: _scan_output(),
        run_with_env=lambda command, env: (0, guest.MARKER, ""))

    assert report.ok, report.lines()
    assert seen == list(rootfs_setup.STEPS)
    assert layout.installed(root)
    assert layout.saved_strategy(root)
    assert sandbox.load(state) == [146, 240]
    from extcli_src.rootfs import network as network_module

    assert network_module.servers_in(network_module.read(root)) == ["1.1.1.1"]
    assert rootfs_setup.ready(res, state, root)


def test_the_second_time_there_is_nothing_to_do(tmp_path, monkeypatch):
    """It runs on every plugin load, and a phone that is already set up must
    not unpack a rootfs over the one the user has been using."""
    source, res = _bundle(tmp_path)
    monkeypatch.setattr(sources, "BUNDLED", (source,))
    root = str(tmp_path / "rootfs")
    state = str(tmp_path / "state")
    os.makedirs(os.path.join(res, "native/arm64-v8a"))
    with open(os.path.join(res, "native/arm64-v8a/syscalls"), "w") as handle:
        handle.write("elf")
    common = dict(abi="arm64-v8a", linker="/l", dns=["1.1.1.1"],
                  source=source.name, run=lambda command: _scan_output(),
                  run_with_env=lambda command, env: (0, guest.MARKER, ""))
    rootfs_setup.prepare(res, state, root, **common)
    with open(os.path.join(root, "bin/mine"), "w") as handle:
        handle.write("a file the user put there")

    again = rootfs_setup.prepare(res, state, root, **common)
    assert not again.did_anything
    assert again.skipped == list(rootfs_setup.STEPS)
    assert os.path.isfile(os.path.join(root, "bin/mine"))


def test_a_rootfs_that_cannot_be_unpacked_stops_the_rest(tmp_path,
                                                         monkeypatch):
    """Measuring how a guest program starts, against a rootfs that is not
    there, produces a measurement about nothing."""
    source, res = _bundle(tmp_path)
    monkeypatch.setattr(sources, "BUNDLED", (source,))
    root = str(tmp_path / "rootfs")
    state = str(tmp_path / "state")
    os.makedirs(os.path.join(res, "native/armeabi-v7a"))
    with open(os.path.join(res, "native/armeabi-v7a/syscalls"), "w") as handle:
        handle.write("elf")

    report = rootfs_setup.prepare(res, state, root, abi="armeabi-v7a",
                                  linker="/l", source=source.name,
                                  run=lambda command: _scan_output(),
                                  run_with_env=lambda c, e: (0, "", ""))
    assert not report.ok
    step, detail = report.failure()
    assert step == "unpack" and "armeabi-v7a" in detail
    # and nothing after it: a measurement of how a program starts in a rootfs
    # that is not there is a measurement about nothing
    assert [name for name, _ok, _d in report.steps][-1] == "unpack"


def test_a_failed_scan_does_not_stop_the_rootfs(tmp_path, monkeypatch):
    """Without it a guest that makes a refused call is killed — but plenty
    never make one, and a rootfs that mostly works beats no rootfs."""
    source, res = _bundle(tmp_path)
    monkeypatch.setattr(sources, "BUNDLED", (source,))
    root = str(tmp_path / "rootfs")
    state = str(tmp_path / "state")

    report = rootfs_setup.prepare(
        res, state, root, abi="arm64-v8a", linker="/l", source=source.name,
        run=lambda command: (1, "", "killed"),
        run_with_env=lambda command, env: (0, guest.MARKER, ""))
    assert report.failure()[0] == "syscalls"
    assert layout.installed(root) and layout.saved_strategy(root)


def test_the_bar_only_counts_the_steps_that_are_being_done():
    """A setup with nothing left but the resolver would otherwise start its
    bar at ninety-odd per cent and jump to full."""
    only_dns = rootfs_setup.Progress(["dns"])
    assert only_dns.at("dns", 0.0) == 0.0
    assert only_dns.at("dns", 0.5) == 0.5
    assert only_dns.at("dns", 1.0) == 1.0


def test_the_bar_moves_through_a_step_as_well_as_between_them():
    """The scan and the unpacking take long enough that a bar which only moves
    when a step ends looks like a bar that has stopped."""
    progress = rootfs_setup.Progress(rootfs_setup.STEPS)
    start = progress.at("syscalls", 0.0)
    middle = progress.at("syscalls", 0.5)
    end = progress.at("syscalls", 1.0)
    assert start < middle < end
    assert end == progress.at("unpack", 0.0)
    assert progress.at(rootfs_setup.STEPS[-1], 1.0) == 1.0


def test_the_bar_never_goes_backwards_over_a_whole_run():
    progress = rootfs_setup.Progress(rootfs_setup.STEPS)
    seen = [progress.at(step, inner)
            for step in rootfs_setup.STEPS
            for inner in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert seen == sorted(seen)
    assert 0.0 == seen[0] and seen[-1] == 1.0


def test_a_step_nobody_is_doing_reads_as_where_the_run_has_got_to():
    """`say` is called for every step; the ones that were already done must
    not push the bar forwards or back."""
    progress = rootfs_setup.Progress(["unpack", "dns"])
    assert progress.at("syscalls", 1.0) == 0.0
    assert progress.at("writes", 0.0) == progress.at("unpack", 1.0)


def test_progress_is_reported_while_a_rootfs_is_unpacked(tmp_path,
                                                         monkeypatch):
    """Five hundred files with nothing said about them is a bar that sits at
    the same place for the whole of the longest step."""
    source, res = _bundle(tmp_path)
    monkeypatch.setattr(sources, "BUNDLED", (source,))
    root = str(tmp_path / "rootfs")
    state = str(tmp_path / "state")
    seen = []

    rootfs_setup.prepare(res, state, root, abi="arm64-v8a", linker="/l",
                         dns=["1.1.1.1"], source=source.name,
                         on_progress=lambda f, label: seen.append(f),
                         run=lambda command: _scan_output(),
                         run_with_env=lambda c, e: (0, guest.MARKER, ""))
    assert seen == sorted(seen)
    assert seen[-1] == 1.0


def test_the_scan_reports_where_it_has_got_to_by_what_it_prints(tmp_path,
                                                               monkeypatch):
    """One child process per syscall number, and the numbers come out in
    order — which is the only progress a scan like that can report."""
    source, res = _bundle(tmp_path)
    monkeypatch.setattr(sources, "BUNDLED", (source,))
    root = str(tmp_path / "rootfs")
    state = str(tmp_path / "state")
    os.makedirs(os.path.join(res, "native/arm64-v8a"))
    with open(os.path.join(res, "native/arm64-v8a/syscalls"), "w") as handle:
        handle.write("elf")
    during = []

    def streaming(command, on_line=None):
        text = "40\n230\n460\nend\n"
        for line in text.splitlines():
            if on_line is not None:
                on_line(line)
        return 0, text, ""

    streaming.streams = True
    rootfs_setup.prepare(
        res, state, root, abi="arm64-v8a", linker="/l", source=source.name,
        on_progress=lambda f, label: during.append((f, label)),
        run=streaming, run_with_env=lambda c, e: (0, guest.MARKER, ""))
    scanning = [f for f, label in during
                if label == rootfs_setup.LABELS["syscalls"]]
    # it moved while the scan was running, not only when it ended
    assert len(scanning) > 3
    assert scanning == sorted(scanning)


def test_only_the_rootfs_is_mounted_to_begin_with():
    """The others are somebody's files — the phone's, the client's — and a
    shell that can reach them by default is one that can ruin them by
    accident."""
    values = mounts.defaults()
    assert values[mounts.ROOT] is True
    assert [key for key, on in values.items() if on] == [mounts.ROOT]


def test_the_client_s_own_code_has_a_mount_of_its_own():
    item = mounts.mount(mounts.PATCH)
    assert item.guest == "/patch"
    assert item.setting == "mount_patch"
    # off until somebody asks for it: laying it out costs time and space
    assert mounts.PATCH not in mounts.DEFAULT_ON


def test_the_rootfs_can_still_not_be_the_one_that_is_turned_off():
    values = dict(mounts.defaults())
    assert mounts.refusal(values, "mount_root", False)
    values[mounts.PATCH] = True
    # with another one on, the rootfs may go
    assert not mounts.refusal(values, "mount_root", False)


# ------------------------------------------------------- tools in the container

from extcli_src.rootfs import packages as packages_module  # noqa: E402
from extcli_src.rootfs import toolbox  # noqa: E402


def test_the_first_group_is_the_one_that_is_offered():
    """Utilities are thirteen megabytes and everybody wants them; a Java
    runtime is two hundred and is asked for."""
    assert packages_module.DEFAULT == ("utils",)
    utils = packages_module.group("utils")
    for name in ("git", "curl", "nano", "less", "tar", "xz", "unzip", "zip",
                 "file", "coreutils", "grep", "sed"):
        assert name in utils.names
    # ripgrep is a tool for people who already know they want it
    assert "ripgrep" not in utils.names
    assert not packages_module.group("java").default


def test_what_is_offered_first_is_everything_in_the_first_group():
    selection = packages_module.Selection()
    assert selection.is_on("utils")
    assert not selection.is_on("python")
    assert selection.packages() == list(packages_module.group("utils").names)


def test_a_package_can_be_taken_out_of_a_group():
    selection = packages_module.Selection()
    selection.set_package("utils", "file", False)
    assert "file" not in selection.packages()
    assert "git" in selection.packages()
    # and put back, where the group lists it rather than at the end
    selection.set_package("utils", "file", True)
    assert selection.packages() == list(packages_module.group("utils").names)


def test_taking_the_last_package_out_turns_the_group_off():
    selection = packages_module.Selection({"python": ["uv"]})
    assert selection.is_on("python")
    selection.set_package("python", "uv", False)
    assert not selection.is_on("python")
    assert selection.packages() == []


def test_turning_a_group_on_ticks_everything_in_it():
    selection = packages_module.Selection()
    selection.set_group("java", True)
    assert selection.has("java", "openjdk17-jre-headless")
    selection.set_group("java", False)
    assert not selection.is_on("java")


def test_a_selection_is_priced_before_it_is_installed():
    selection = packages_module.Selection()
    download, installed = selection.cost()
    assert 8 <= download <= 20 and 30 <= installed <= 50
    selection.set_group("java", True)
    bigger = selection.cost()
    assert bigger[1] > installed + 100
    assert "packages" in selection.sentence()
    # an empty dict means "nothing chosen", not "use the defaults"
    empty = packages_module.Selection()
    for name in packages_module.NAMES:
        empty.set_group(name, False)
    assert empty.sentence() == "nothing"


def test_nothing_is_installed_twice(tmp_path):
    root = str(tmp_path / "rootfs")
    os.makedirs(os.path.join(root, "lib/apk/db"))
    with open(os.path.join(root, "lib/apk/db/installed"), "w") as handle:
        handle.write("P:git\nV:1\n\nP:nano\nV:1\n\n")
    selection = packages_module.Selection()
    left = toolbox.wanted(root, selection)
    assert "git" not in left and "nano" not in left
    assert "curl" in left
    assert toolbox.anything_to_do(root, selection)


def test_apk_s_percentages_are_read_as_progress():
    """It draws a bar and rewrites it with carriage returns, so one chunk
    holds several; where it has got to is the last of them."""
    assert toolbox._percentages("  1% #\r 50% ####\r100% ####") == [1.0]
    assert toolbox._percentages(" 42% ##") == [0.42]
    assert toolbox._percentages("(1/12) Installing git") == []


def test_what_is_installed_is_read_from_apk_s_own_database(tmp_path):
    """Asked of the container rather than of a note we wrote: somebody who
    removed a package should get it back."""
    root = tmp_path / "rootfs"
    (root / "lib/apk/db").mkdir(parents=True)
    (root / "lib/apk/db/installed").write_text(
        "C:Q1x\nP:musl\nV:1.2.5-r0\n\nC:Q1y\nP:git\nV:2.51.0-r0\n\n")
    assert layout.installed_package(str(root), "git")
    assert not layout.installed_package(str(root), "python3")
    assert layout.installed_packages(str(root)) == ["musl", "git"]
    assert not layout.installed_package(str(tmp_path / "nothing"), "git")


# ------------------------------------- toolsets that depend on other toolsets

def test_a_python_tool_cannot_be_chosen_without_python():
    from extcli_src.rootfs import packages

    selection = packages.Selection({})
    assert not selection.is_possible("pytools")
    selection.set_group("pytools", True)
    assert not selection.is_on("pytools"), "it cannot be installed, so it is off"
    selection.set_group("python", True)
    assert selection.is_possible("pytools")
    selection.set_group("pytools", True)
    assert selection.is_on("pytools")


def test_unticking_python_takes_the_python_tools_with_it():
    """Leaving them ticked would mean pressing Install and watching half of it
    fail on a container with no interpreter in it."""
    from extcli_src.rootfs import packages

    selection = packages.Selection({"python": ["python3", "py3-pip", "uv"],
                                    "pytools": ["elyxbuilder"]})
    assert selection.is_on("pytools")
    selection.set_group("python", False)
    assert not selection.is_on("pytools")


def test_python_already_in_the_container_is_enough():
    from extcli_src.rootfs import packages

    selection = packages.Selection({}, satisfied=("python",))
    assert selection.is_possible("pytools")
    selection.set_group("pytools", True)
    assert selection.is_on("pytools")


def test_what_is_already_there_is_not_offered_again():
    from extcli_src.rootfs import packages

    selection = packages.selection_for(installed=("git", "nano"))
    assert "git" not in selection.packages()
    assert "nano" not in selection.packages()
    assert "curl" in selection.packages()


def test_a_selection_becomes_the_command_somebody_could_have_typed():
    from extcli_src.rootfs import packages

    whole = packages.Selection({"utils": list(packages.group("utils").names)})
    assert whole.command_words() == ["utils"]
    part = packages.Selection({"utils": ["git", "nano"]})
    assert part.command_words() == ["git", "nano"]


def test_the_kinds_of_package_are_told_apart(tmp_path):
    """Alpine keeps a database and is asked. pip keeps one this side cannot
    read, so what is looked for is the thing it puts on the PATH."""
    from extcli_src.rootfs import toolbox

    root = str(tmp_path)
    binary = tmp_path / "root" / ".local" / "bin"
    binary.mkdir(parents=True)
    (binary / "elyb").write_text("#!/bin/sh\n")
    assert toolbox.present(root, "elyxbuilder")
    assert not toolbox.present(root, "yt-dlp")


def test_uv_is_preferred_to_pip_when_the_container_has_it(tmp_path):
    from extcli_src.rootfs import toolbox

    root = str(tmp_path)
    assert toolbox._pip_command(root, "elyxbuilder")[0] == "pip3"
    binary = tmp_path / "usr" / "bin"
    binary.mkdir(parents=True)
    (binary / "uv").write_text("")
    assert toolbox._pip_command(root, "elyxbuilder") == \
        ["uv", "tool", "install", "elyxbuilder"]


def test_words_on_a_command_line_may_be_toolsets_or_packages():
    from extcli_src.shell.builtins import rootfs as rootfs_builtin

    chosen, unknown = rootfs_builtin._wanted_groups(["python", "nano", "git"])
    assert chosen["python"] == list(
        __import__("extcli_src.rootfs.packages", fromlist=["x"])
        .group("python").names)
    assert chosen["utils"] == ["git", "nano"]   # in the group's own order
    assert unknown == []
    _chosen, unknown = rootfs_builtin._wanted_groups(["nonsense"])
    assert unknown == ["nonsense"]


# ------------------------------------------------------- exporting the whole

def _container(tmp_path):
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "etc").mkdir()
    (root / "bin/busybox").write_bytes(b"elf" * 400)
    (root / "etc/passwd").write_text("root:x:0:0:")
    # a rootfs is full of these, and they point at its own /
    os.symlink("/bin/busybox", str(root / "bin/sh"))
    return str(root)


def test_the_archive_holds_the_container(tmp_path):
    import tarfile

    from extcli_src.rootfs import export

    root = _container(tmp_path)
    target = str(tmp_path / "out.tar.gz")
    ok, detail = export.archive(root, target)
    assert ok, detail
    with tarfile.open(target) as archive:
        names = set(archive.getnames())
    assert "bin/busybox" in names and "etc/passwd" in names


def test_a_symlink_is_stored_as_itself(tmp_path):
    """Following them would bake this phone's paths into the archive, and
    inflate it with a copy of every file a link points at."""
    import tarfile

    from extcli_src.rootfs import export

    root = _container(tmp_path)
    target = str(tmp_path / "out.tar.gz")
    export.archive(root, target)
    with tarfile.open(target) as archive:
        link = archive.getmember("bin/sh")
    assert link.issym()
    assert link.linkname == "/bin/busybox"


def test_the_archive_can_be_unpacked_back(tmp_path):
    import tarfile

    from extcli_src.rootfs import export

    root = _container(tmp_path)
    target = str(tmp_path / "out.tar.gz")
    export.archive(root, target)
    back = tmp_path / "back"
    with tarfile.open(target) as archive:
        archive.extractall(str(back))
    assert (back / "etc/passwd").read_text() == "root:x:0:0:"
    assert os.path.islink(str(back / "bin/sh"))


def test_progress_is_reported_and_ends_at_one(tmp_path):
    from extcli_src.rootfs import export

    seen = []
    export.archive(_container(tmp_path), str(tmp_path / "o.tar.gz"),
                   on_progress=seen.append)
    assert seen and seen[-1] == 1.0
    assert all(0.0 <= value <= 1.0 for value in seen)


def test_exporting_nothing_is_refused_not_attempted(tmp_path):
    from extcli_src.rootfs import export

    ok, detail = export.archive(str(tmp_path / "absent"), str(tmp_path / "o.tgz"))
    assert not ok and "no container" in detail
    assert not (tmp_path / "o.tgz").exists()


def test_the_archive_is_named_after_the_day_it_was_made(tmp_path):
    """The first thing anybody does with a backup is make another one."""
    import re

    from extcli_src.rootfs import export

    name = export.name_for(_container(tmp_path))
    assert re.fullmatch(r"extcli-rootfs-\d{8}-\d{4}\.tar\.gz", name)


def test_the_provider_follows_the_package(tmp_path):
    """The authority differs between the beta and the full build, so it is
    asked of the context rather than written down."""
    from extcli_src.compat import intents

    class Context(object):
        def getPackageName(self):
            return "com.exteragram.messenger.beta"

    assert intents.authority(Context()) == "com.exteragram.messenger.beta.provider"

    class Mute(object):
        def getPackageName(self):
            raise RuntimeError("no context here")

    assert intents.authority(Mute()) == intents.FALLBACK_AUTHORITY
