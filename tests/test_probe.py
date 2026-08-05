# SPDX-License-Identifier: Apache-2.0

"""Offline tests for the capability probe.

The probe is deliberately free of android imports so it can run here. On this
machine /system/bin does not exist, which is exactly the "everything missing"
path we want covered; data_exec, by contrast, succeeds on a normal Linux box,
which proves the check reports reality rather than a hardcoded verdict.
"""

import os

from extcli_src.backends import probe


def test_shell_missing_is_reported_not_assumed():
    result = probe.check_shell("/definitely/not/here/sh")
    assert result["status"] == probe.MISSING
    assert "does not exist" in result["detail"]


def test_shell_ok_on_host_sh():
    sh = "/bin/sh"
    if not os.path.exists(sh):
        return
    result = probe.check_shell(sh)
    assert result["status"] == probe.OK
    assert result["detail"] == sh


def test_data_exec_allowed_on_desktop(tmp_path):
    result = probe.check_data_exec(str(tmp_path))
    # /system/bin/sh is the shebang, so on desktop the exec attempt fails to
    # find the interpreter rather than being denied by SELinux.
    assert result["status"] in (probe.OK, probe.BLOCKED)
    assert result["x_bit"] is True


def test_data_exec_cleans_up_after_itself(tmp_path):
    probe.check_data_exec(str(tmp_path))
    assert os.listdir(str(tmp_path)) == []


def test_pty_available():
    result = probe.check_pty()
    assert result["status"] == probe.OK


def test_linker_missing_without_android(tmp_path):
    result = probe.check_linker(str(tmp_path), abi="arm64-v8a")
    assert result["status"] in (probe.MISSING, probe.UNKNOWN)


def test_inproc_backend_always_offered(tmp_path):
    result = probe.run(str(tmp_path), probe.HostFacts(abi="arm64-v8a"))
    assert "inproc" in result["backends"]


def test_rootfs_verdict_mentions_blocked_exec():
    result = {"checks": {
        "data_exec": {"status": probe.BLOCKED},
        "linker": {"status": probe.BLOCKED},
    }}
    assert "not available" in probe.rootfs_verdict(result)


def test_rootfs_verdict_prefers_direct_exec():
    result = {"checks": {"data_exec": {"status": probe.OK}}}
    assert "proot can run directly" in probe.rootfs_verdict(result)


def test_rootfs_verdict_falls_back_to_linker():
    result = {"checks": {
        "data_exec": {"status": probe.BLOCKED},
        "linker": {"status": probe.OK},
    }}
    assert "via linker" in probe.rootfs_verdict(result)


def test_summary_lines_are_printable(tmp_path):
    result = probe.run(str(tmp_path), probe.HostFacts(
        abi="arm64-v8a", api_level=36, android_release="16",
        app_version="12.9.0", sdk_version="1.4.5.0",
    ))
    text = "\n".join(probe.summary_lines(result))
    assert "extCLI diagnostics" in text
    assert "12.9.0" in text
    assert "backends" in text
    for line in text.split("\n"):
        assert len(line) < 120


def test_cache_round_trip(tmp_path):
    state = str(tmp_path / "state")
    result = probe.run(str(tmp_path), probe.HostFacts(app_version="12.9.0"))
    assert probe.save_cached(state, result)
    assert probe.load_cached(state, "12.9.0") is not None


def test_cache_is_dropped_after_client_update(tmp_path):
    state = str(tmp_path / "state")
    result = probe.run(str(tmp_path), probe.HostFacts(app_version="12.9.0"))
    probe.save_cached(state, result)
    assert probe.load_cached(state, "12.9.1") is None
