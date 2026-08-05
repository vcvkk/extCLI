# SPDX-License-Identifier: Apache-2.0

"""Reading the dynamic-linker probe correctly.

The first version of this check reported "blocked" on a device where the trick
actually worked. The device said:

  /system/bin/linker64: exit 127: toybox: Unknown command /data/user/0/...

That is toybox's own complaint, not a refusal: the copy sitting in the app's
data directory had been mapped and executed, and it only objected to how it was
invoked (it is a multi-call binary and dispatches on argv[0], so the copy has to
be named after an applet). Getting this wrong is expensive — it decides whether
a real Alpine userspace is reachable at all — so the classification is a pure
function with the real strings pinned here.
"""

from extcli_src.backends import probe


def test_marker_in_output_is_success():
    status, detail = probe.interpret_linker_output(0, "extcli-probe", None)
    assert status == probe.OK
    assert "data directory" in detail


def test_the_device_case_that_was_misread():
    out = "toybox: Unknown command /data/user/0/com.exteragram.messenger/files/extcli/tmp/toybox"
    status, detail = probe.interpret_linker_output(127, out, None)
    assert status == probe.OK, "the ELF ran; only the invocation was wrong"
    assert "executed" in detail


def test_usage_output_also_counts_as_executed():
    status, _ = probe.interpret_linker_output(1, "usage: echo [-n] [-e] ARG...", None)
    assert status == probe.OK


def test_selinux_refusal_is_blocked():
    for message in (
        "/system/bin/linker64: error: unable to open file",
        "CANNOT LINK EXECUTABLE: permission denied",
        "linker64: failed to open /data/user/0/pkg/files/x",
        "execve failed: operation not permitted",
    ):
        status, _ = probe.interpret_linker_output(1, message, None)
        assert status == probe.BLOCKED, message


def test_spawn_error_is_blocked():
    status, detail = probe.interpret_linker_output(None, "", "permission denied: x")
    assert status == probe.BLOCKED
    assert "permission denied" in detail


def test_silent_success_is_trusted():
    status, _ = probe.interpret_linker_output(0, "", None)
    assert status == probe.OK


def test_unexplained_failure_is_blocked():
    status, detail = probe.interpret_linker_output(9, "segmentation fault", None)
    assert status == probe.BLOCKED
    assert "exit 9" in detail


def test_a_working_linker_changes_the_rootfs_verdict():
    result = {"checks": {
        "data_exec": {"status": probe.BLOCKED},
        "linker": {"status": probe.OK},
    }}
    assert "via linker" in probe.rootfs_verdict(result)
    assert "linker" in probe.available_backends(result)


def test_the_probe_copy_is_named_after_an_applet(tmp_path, monkeypatch):
    """toybox only dispatches when argv[0] is an applet name."""
    seen = {}

    def fake_run(argv, timeout=None, cwd=None):
        seen["argv"] = argv
        return 0, "extcli-probe", None

    monkeypatch.setattr(probe, "_run", fake_run)
    monkeypatch.setattr(probe.os.path, "exists", lambda p: True)
    monkeypatch.setattr(probe.shutil, "copyfile", lambda a, b: None)
    monkeypatch.setattr(probe.os, "chmod", lambda p, m: None)
    monkeypatch.setattr(probe.os, "remove", lambda p: None)

    result = probe.check_linker(str(tmp_path), abi="arm64-v8a")
    assert result["status"] == probe.OK
    assert seen["argv"][1].endswith("/toybox")
    assert seen["argv"][2:] == ["echo", "extcli-probe"]
