# SPDX-License-Identifier: Apache-2.0

"""Deleting what extCLI has written.

The plugin keeps its data outside its own directory so an update does not
throw away an Alpine somebody has spent an evening on. That is also why
removing the plugin leaves it behind, and why this exists.
"""

import os

from extcli_src.utils import purge


def _tree(tmp_path):
    data = tmp_path / "extcli"
    (data / "rootfs/bin").mkdir(parents=True)
    (data / "state").mkdir(parents=True)
    (data / "rootfs/bin/busybox").write_bytes(b"x" * 1000)
    (data / "rootfs/bin/sh").symlink_to("busybox")
    (data / "state/syscalls").write_text("146\n")
    return str(data)


def test_what_it_would_cost_is_known_before_it_happens(tmp_path):
    """A dialog that asks to delete a container has to say how big it is."""
    data = _tree(tmp_path)
    files, total = purge.measure(data)
    assert files == 3
    assert total >= 1000


def test_a_link_is_counted_as_itself(tmp_path):
    """A rootfs is full of absolute symlinks. Following one would measure the
    phone rather than the container."""
    data = _tree(tmp_path)
    _files, total = purge.measure(data)
    assert total < 2000


def test_a_missing_directory_is_nothing_rather_than_an_error(tmp_path):
    assert purge.measure(str(tmp_path / "never")) == (0, 0)
    assert purge.describe([str(tmp_path / "never")])[0] == "nothing to delete"


def test_everything_under_it_goes(tmp_path):
    data = _tree(tmp_path)
    result = purge.remove([data])
    assert result.ok
    assert result.files == 3
    assert not os.path.exists(data)
    assert "deleted 3 files" in result.sentence()


def test_the_directories_it_must_never_touch(tmp_path):
    """`data_dir` is built from the app's own files directory. A bug that
    returned that directory instead would take the client's data with it."""
    files_dir = str(tmp_path)
    data = _tree(tmp_path)
    result = purge.remove([files_dir], keep=[files_dir, "/sdcard"])
    assert not result.ok
    assert result.refused and result.refused[0][0] == files_dir
    # and the tree it was asked to spare is still there
    assert os.path.isdir(data)
    for path in ("/", "", "."):
        assert purge.protects(path, files_dir)


def test_deleting_what_is_not_there_is_not_a_failure(tmp_path):
    result = purge.remove([str(tmp_path / "never")])
    assert result.ok
    assert result.sentence() == "there was nothing to delete"


def test_sizes_are_readable(tmp_path):
    assert purge.human_size(512) == "512 B"
    assert purge.human_size(2048) == "2.0 KB"
    assert purge.human_size(5 * 1024 * 1024) == "5.0 MB"
