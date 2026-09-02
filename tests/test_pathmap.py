# SPDX-License-Identifier: Apache-2.0

"""Turning a guest path into a host path.

The loader is aarch64 and the emulator here does not implement ptrace, so the
supervisor cannot be run in this container at all. This piece can: `pathmap.c`
is included by the loader rather than linked into it, and `pathmap_harness.c`
builds the same file for whatever machine the tests run on. Same file, not a
second implementation — a copy would agree with itself and prove nothing.

What is being pinned is the one rule that is easy to state and easy to get
wrong: a rootfs is full of absolute symlinks, Alpine's /bin/sh points at
/bin/busybox, and inside the rootfs that is correct. Resolved by the host it
leads out of the rootfs to a file that does not exist, which is exactly how
`rootfs probe launch` once came back with "unable to open file .../rootfs/bin/sh".
"""

import errno
import os
import re
from pathlib import Path
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.join(HERE, "..", "native")
PASS = "/proc:/sys:/dev"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    compiler = os.environ.get("CC") or shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("no C compiler to build the harness with")
    binary = str(tmp_path_factory.mktemp("native") / "pathmap-harness")
    result = subprocess.run(
        [compiler, "-Wall", "-Wextra", "-Werror", "-O1", "-o", binary,
         os.path.join(NATIVE, "pathmap_harness.c")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert result.returncode == 0, result.stdout.decode("utf-8", "replace")
    return binary


@pytest.fixture
def root(tmp_path):
    """A rootfs with the shapes that matter, warts included."""
    base = tmp_path / "root"
    for name in ("bin", "etc", "lib", "usr", "usr/bin", "proc", "dev"):
        (base / name).mkdir(parents=True)
    (base / "bin/busybox").write_text("elf")
    (base / "etc/passwd").write_text("root:x:0:0:")
    # absolute, which is correct inside a rootfs and wrong outside it
    os.symlink("/bin/busybox", str(base / "bin/sh"))
    # relative, the other half of what a rootfs contains
    os.symlink("busybox", str(base / "bin/ls"))
    # a directory that is a link, so a link has to be followed mid-path
    os.symlink("/bin", str(base / "usr/bin/host"))
    os.symlink("/etc/loop", str(base / "etc/loop"))
    return str(base)


def mounts_for(root, extra=()):
    rows = [("/", root)] + list(extra)
    return "|".join("%s=%s" % pair for pair in rows)


def mapped(harness, root, path, follow=True, extra=()):
    command = [harness, mounts_for(root, extra), PASS, path]
    if not follow:
        command.append("nofollow")
    result = subprocess.run(command, stdout=subprocess.PIPE)
    return result.stdout.decode("utf-8").strip()


def unmapped(harness, root, path, extra=()):
    """The other direction, which is what getcwd needs."""
    result = subprocess.run(
        [harness, mounts_for(root, extra), PASS, path, "back"],
        stdout=subprocess.PIPE)
    return result.stdout.decode("utf-8").strip()


def kind(harness, root, path, extra=()):
    """Guest path or host path? — asked of the same function the loader asks."""
    result = subprocess.run(
        [harness, mounts_for(root, extra), PASS, path, "host"],
        stdout=subprocess.PIPE)
    return result.stdout.decode("utf-8").strip()


def test_slash_is_the_rootfs(harness, root):
    assert mapped(harness, root, "/") == root


def test_an_absolute_link_stays_inside_the_rootfs(harness, root):
    """The one that matters. /bin/sh -> /bin/busybox means the rootfs's
    /bin/busybox, not the phone's."""
    assert mapped(harness, root, "/bin/sh") == os.path.join(root, "bin/busybox")


def test_a_relative_link_is_followed_too(harness, root):
    assert mapped(harness, root, "/bin/ls") == os.path.join(root, "bin/busybox")


def test_a_link_in_the_middle_of_a_path_is_followed(harness, root):
    assert mapped(harness, root, "/usr/bin/host/busybox") == \
        os.path.join(root, "bin/busybox")


def test_the_last_component_is_left_alone_when_asked(harness, root):
    """readlink and unlink mean the link itself, and so does anything whose
    flags say AT_SYMLINK_NOFOLLOW."""
    assert mapped(harness, root, "/bin/sh", follow=False) == \
        os.path.join(root, "bin/sh")


def test_dot_dot_cannot_climb_out(harness, root):
    for path in ("/..", "/../..", "/etc/../..", "/../etc"):
        assert mapped(harness, root, path).startswith(root), path
    assert mapped(harness, root, "/etc/../etc/passwd") == \
        os.path.join(root, "etc/passwd")


def test_a_path_that_does_not_exist_yet_is_still_answered(harness, root):
    """A program about to create a file needs an answer as much as one about
    to read one."""
    assert mapped(harness, root, "/tmp/new/file") == \
        os.path.join(root, "tmp/new/file")


def test_a_loop_is_refused_rather_than_followed_forever(harness, root):
    # the reason travels with the refusal: the loader prints it back from
    # the device, where a bad link and a long path want different fixes
    assert mapped(harness, root, "/etc/loop") == "fail loop"


def test_the_host_s_own_directories_are_not_translated(harness, root):
    """musl reads /proc/self/fd and /dev/urandom before it does anything else,
    and a rootfs has nothing but empty directories there."""
    for path in ("/proc", "/proc/self/exe", "/dev/null", "/sys/kernel"):
        assert mapped(harness, root, path) == "pass", path


def test_a_prefix_is_matched_by_whole_component(harness, root):
    """/devices is not /dev."""
    assert mapped(harness, root, "/devices") == os.path.join(root, "devices")


def test_a_path_too_long_to_hold_is_refused_not_truncated(harness, root):
    """A truncated path is a different file, and quietly opening a different
    file is worse than failing."""
    assert mapped(harness, root, "/" + "a" * 2000) == "fail long"


def test_a_path_with_no_mount_under_it_says_so(harness, root, tmp_path):
    """With the rootfs switched off there is nowhere for /etc to be, and the
    refusal has to name that rather than look like a path that did not fit."""
    result = subprocess.run(
        [harness, "/sdcard=%s" % tmp_path, PASS, "/etc/passwd"],
        stdout=subprocess.PIPE)
    assert result.stdout.decode("utf-8").strip() == "fail noroot"


# --------------------------------------------------------------- mounts

@pytest.fixture
def elsewhere(tmp_path):
    """A host directory grafted onto a name of the guest's own."""
    other = tmp_path / "sdcard"
    (other / "Download").mkdir(parents=True)
    (other / "Download/note.txt").write_text("hi")
    return str(other)


def test_a_mount_takes_the_path_out_of_the_rootfs(harness, root, elsewhere):
    extra = [("/sdcard", elsewhere)]
    assert mapped(harness, root, "/sdcard", extra=extra) == elsewhere
    assert mapped(harness, root, "/sdcard/Download/note.txt", extra=extra) == \
        os.path.join(elsewhere, "Download/note.txt")


def test_an_unmounted_name_is_just_a_directory_in_the_rootfs(harness, root):
    """Nothing special happens to /sdcard when it is not mounted: it is a name
    inside Alpine like any other, and Alpine has no such directory."""
    assert mapped(harness, root, "/sdcard/x") == os.path.join(root, "sdcard/x")


def test_the_longest_mount_wins(harness, root, elsewhere, tmp_path):
    """A mount inside another one has to be reached, not shadowed."""
    inner = str(tmp_path / "inner")
    os.makedirs(inner)
    extra = [("/sdcard", elsewhere), ("/sdcard/deep", inner)]
    assert mapped(harness, root, "/sdcard/deep/file", extra=extra) == \
        os.path.join(inner, "file")


def test_a_mount_is_matched_by_whole_component(harness, root, elsewhere):
    extra = [("/sdcard", elsewhere)]
    assert mapped(harness, root, "/sdcardish") == os.path.join(root, "sdcardish")


def test_getcwd_is_answered_in_the_guest_s_own_names(harness, root, elsewhere):
    extra = [("/sdcard", elsewhere)]
    assert unmapped(harness, root, root, extra) == "/"
    assert unmapped(harness, root, os.path.join(root, "etc"), extra) == "/etc"
    assert unmapped(harness, root, elsewhere, extra) == "/sdcard"
    assert unmapped(harness, root, os.path.join(elsewhere, "Download"),
                    extra) == "/sdcard/Download"


def test_a_host_path_under_nothing_is_left_as_it_is(harness, root, elsewhere):
    """Standing outside every mount is not an error — /proc is a real place."""
    assert unmapped(harness, root, "/proc/self", [("/sdcard", elsewhere)]) == \
        "fail"


def test_a_path_that_has_been_mapped_once_is_not_mapped_again(harness, root,
                                                              elsewhere):
    """A real path finds its way back into the guest more often than it looks:
    the loader is handed one when it is exec'd, /proc/self/exe answers with one.
    Translating it a second time buries it inside the rootfs, where nothing is —
    which is what "cannot open: /data/.../rootfs/lib/apk/exec/..." was."""
    extra = [("/sdcard", elsewhere)]
    assert kind(harness, root, root, extra) == "host"
    assert kind(harness, root, os.path.join(root, "bin/busybox"), extra) == "host"
    assert kind(harness, root, elsewhere, extra) == "host"
    # and a path the guest means is still the guest's
    assert kind(harness, root, "/bin/busybox", extra) == "guest"
    assert kind(harness, root, "/", extra) == "guest"


# ---------------------------------------------- the numbers the loader is made of

LOADER = Path(__file__).resolve().parent.parent / "native" / "loader.c"


def defined(name):
    """A `#define NAME <number>` from the loader, as an int."""
    match = re.search(r"^#define %s\s+(-?\d+)\s*$" % name, LOADER.read_text(),
                      re.MULTILINE)
    assert match, "%s is not defined in loader.c" % name
    return int(match.group(1))


def test_the_errnos_the_loader_names_are_the_errnos_they_are():
    """Freestanding means no errno.h, so these are written out by hand — and a
    wrong one here is a hardlink fallback that never fires, or one that fires
    on the wrong failure."""
    for name in ("EPERM", "EACCES", "EXDEV", "EMLINK", "ENOSYS", "EOPNOTSUPP"):
        assert defined(name) == getattr(errno, name), name


def test_the_syscall_numbers_are_the_generic_table():
    """aarch64 uses the asm-generic numbers, and these are copied from it.

    Two of them are the reason `apk add unzip` could fail on exactly one file:
    the loader watches for linkat and, when the kernel refuses, copies instead.
    A number wrong by one would have it watching something else entirely.
    """
    for name, number in (("openat", 56), ("close", 57), ("read", 63),
                         ("write", 64), ("readlinkat", 78), ("fstat", 80),
                         ("unlinkat", 35), ("linkat", 37)):
        assert defined("SYS_" + name) == number, name


def test_linkat_is_watched_on_the_way_out_and_translated_on_the_way_in():
    """Both, or the copy is made between paths the guest meant and the
    supervisor cannot open."""
    source = LOADER.read_text()
    assert "{ 37, ARG1 | ARG3," in source           # both paths translated
    assert "SYS_linkat) {" in source and "mend_link" in source


def test_a_link_through_proc_self_is_never_copied():
    """apk links an unnamed file into place through /proc/self/fd/N. Read in
    the supervisor that names *its* descriptor N, so copying from it would put
    some file of ours where the guest wanted its download."""
    source = LOADER.read_text()
    assert "means_the_same_here" in source
    assert '"/proc/"' in source
    # and it is asked before anything is opened
    guard = source.index("means_the_same_here(guest)")
    opened = source.index("static int copy_file")
    assert guard > opened, "the check must be in host_path_of, not in copy_file"
