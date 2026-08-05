# SPDX-License-Identifier: Apache-2.0

"""The diagnostics report is the only thing a user can send back from a device,
so its formatting is worth pinning down."""

from extcli_src.backends import probe


def _result():
    return {
        "duration": 0.5,
        "host": {"abi": "arm64-v8a", "api_level": 36, "android": "16",
                 "app_version": "12.9.0", "sdk_version": "1.4.5.0"},
        "checks": {
            "shell": {"status": probe.OK, "detail": "/system/bin/sh"},
            "toybox": {"status": probe.OK, "detail": "180 applets"},
            "pty": {"status": probe.OK, "detail": "pty.fork works"},
            "data_exec": {"status": probe.BLOCKED, "detail": "permission denied"},
            "linker": {"status": probe.BLOCKED, "detail": "exit 1"},
        },
        "backends": ["system", "inproc"],
    }


def test_every_check_appears():
    text = "\n".join(probe.summary_lines(_result()))
    for name in ("shell", "toybox", "pty", "data_exec", "linker"):
        assert name in text


def test_marks_distinguish_ok_from_blocked():
    lines = probe.summary_lines(_result())
    shell = next(line for line in lines if "shell" in line)
    blocked = next(line for line in lines if "data_exec" in line)
    assert shell.startswith("[+]")
    assert blocked.startswith("[x]")


def test_extra_checks_are_rendered_in_the_same_block():
    lines = probe.summary_lines(
        _result(), extra_checks=[("renderer", True, "renderer v1, dex 24284 bytes")]
    )
    renderer_index = next(i for i, line in enumerate(lines) if "renderer" in line)
    linker_index = next(i for i, line in enumerate(lines) if "linker" in line)
    backends_index = next(i for i, line in enumerate(lines) if line.startswith("backends"))
    assert linker_index < renderer_index < backends_index


def test_failed_extra_check_is_marked_blocked():
    lines = probe.summary_lines(
        _result(), extra_checks=[("renderer", False, "dex missing")]
    )
    assert any(line.startswith("[x] renderer") for line in lines)


def test_long_details_are_clipped():
    result = _result()
    result["checks"]["shell"]["detail"] = "x" * 400
    for line in probe.summary_lines(result):
        assert len(line) <= probe.MAX_LINE


def test_blocked_exec_still_leaves_working_backends():
    result = _result()
    assert "inproc" in probe.available_backends(result)
    assert "system" in probe.available_backends(result)
    assert "linker" not in probe.available_backends(result)
