# SPDX-License-Identifier: Apache-2.0

"""Regression tests for problems found on a real device.

1. The pty check used to fork the live app process to see whether pty.fork
   worked. Forking an Android app to test a feature can deadlock on a runtime
   lock; allocating a pty pair proves the same thing.
2. sdk_version() called any attribute of `_sdk_version` that happened to be
   callable. That module also exposes setup_hooks(), __start__() and
   check_safemode() — invoking one of those for a version string is a bad
   trade, and it made a failure inside the SDK look like our bug.
3. log.error dropped the traceback, so a device log said "name 'file' is not
   defined" with no frame to attribute it to.
"""

import sys

from extcli_src.backends import probe
from extcli_src.compat import host
from extcli_src.utils import log


# ------------------------------------------------------------------- pty check

def test_pty_check_does_not_fork(monkeypatch):
    called = []

    def explode(*args, **kwargs):
        called.append(True)
        raise AssertionError("the probe must not fork the app process")

    import os as os_module

    monkeypatch.setattr(os_module, "fork", explode, raising=False)
    monkeypatch.setattr(os_module, "forkpty", explode, raising=False)

    result = probe.check_pty()
    assert not called
    assert result["status"] in (probe.OK, probe.BLOCKED, probe.MISSING)


def test_pty_check_succeeds_on_a_normal_kernel():
    result = probe.check_pty()
    assert result["status"] == probe.OK
    assert "pty pair" in result["detail"]


def test_pty_check_closes_what_it_opens():
    import resource

    before = _open_fd_count()
    for _ in range(20):
        probe.check_pty()
    after = _open_fd_count()
    # 20 iterations leaking two fds each would be unmistakable
    assert after - before < 5, "pty fds leaked: %d -> %d" % (before, after)
    assert resource is not None


def _open_fd_count():
    import os

    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return 0


def test_pty_check_reports_missing_modules(monkeypatch):
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "termios":
            raise ImportError("no termios here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "termios", None)
    if isinstance(__builtins__, dict):
        monkeypatch.setitem(__builtins__, "__import__", fake_import)
    else:
        monkeypatch.setattr(__builtins__, "__import__", fake_import)

    result = probe.check_pty()
    assert result["status"] == probe.MISSING
    assert "termios" in result["detail"]


# ---------------------------------------------------------------- sdk_version

class FakeSdkModule(object):
    """Mimics the SDK internals module: a version string next to functions that
    must not be invoked."""

    __version__ = "1.4.5.3"

    def __init__(self):
        self.invoked = []

    def setup_hooks(self):
        self.invoked.append("setup_hooks")
        raise AssertionError("setup_hooks must never be called by extCLI")

    def version_str(self):
        self.invoked.append("version_str")
        raise AssertionError("SDK attributes must be read, not called")


def test_sdk_version_reads_the_string(monkeypatch):
    fake = FakeSdkModule()
    monkeypatch.setitem(sys.modules, "_sdk_version", fake)
    assert host.sdk_version() == "1.4.5.3"
    assert fake.invoked == []


def test_sdk_version_accepts_a_tuple(monkeypatch):
    class TupleVersion(object):
        version = (1, 4, 5, 3)

    monkeypatch.setitem(sys.modules, "_sdk_version", TupleVersion())
    assert host.sdk_version() == "1.4.5.3"


def test_sdk_version_absent_module():
    sys.modules.pop("_sdk_version", None)
    assert host.sdk_version() is None


def test_sdk_version_ignores_callables(monkeypatch):
    class OnlyCallables(object):
        def version_str(self):
            raise AssertionError("must not be called")

    monkeypatch.setitem(sys.modules, "_sdk_version", OnlyCallables())
    assert host.sdk_version() is None


# ----------------------------------------------------------------- log traces

def test_error_records_a_traceback():
    log.clear()
    try:
        raise ValueError("boom")
    except ValueError as e:
        log.error("something failed", e)
    lines = [text for _, level, text in log.tail(20) if level == "E"]
    assert any("something failed: ValueError: boom" in line for line in lines)
    assert any("test_diagnostics.py" in line for line in lines), \
        "the frame that raised should be in the log"


def test_error_without_an_exception_has_no_trace():
    log.clear()
    log.error("plain message")
    lines = [text for _, level, text in log.tail(20) if level == "E"]
    assert lines == ["plain message"]


def test_traceback_lines_are_empty_outside_an_except():
    assert log.traceback_lines() == []


def test_error_can_skip_the_trace():
    log.clear()
    try:
        raise KeyError("k")
    except KeyError as e:
        log.error("quiet failure", e, trace=False)
    lines = [text for _, level, text in log.tail(20) if level == "E"]
    assert len(lines) == 1
