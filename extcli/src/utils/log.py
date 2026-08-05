# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Logging for extCLI.

Every log line goes to logcat (so bug reports can be collected the usual way)
and into an in-memory ring buffer, which is what `log tail` reads — the plugin
cannot read logcat back on an unrooted device.

Two levels, same rule as the rest of the ecosystem:
  log(msg)              -> always shown; errors and unexpected states
  log(msg, debug=True)  -> only when the "Debug logs" switch is on
"""

import threading
import time
from collections import deque

TAG = "extCLI"

_RING_SIZE = 2000
_ring = deque(maxlen=_RING_SIZE)
_ring_lock = threading.Lock()

# resolved lazily: elyx.settings is not importable at module import time
_debug_enabled = None


def _android_log(line):
    try:
        from android_utils import log as _log

        _log(line)
    except Exception:
        # desktop/unit-test context: logcat does not exist
        pass


def is_debug():
    global _debug_enabled
    if _debug_enabled is None:
        try:
            from elyx import settings

            _debug_enabled = bool(settings.get("debug_logs", False))
        except Exception:
            _debug_enabled = False
    return _debug_enabled


def set_debug(enabled):
    global _debug_enabled
    _debug_enabled = bool(enabled)


def log(msg, debug=False):
    if debug and not is_debug():
        return
    line = "%s: %s" % (TAG, msg)
    with _ring_lock:
        _ring.append((time.time(), "D" if debug else "I", str(msg)))
    _android_log(line)


def error(msg, exc=None, trace=True):
    """Always-visible log for a failure.

    The traceback matters: this plugin is debugged from logs a user pastes back,
    and "name 'x' is not defined" without a stack frame costs a whole round trip.
    """
    text = str(msg) if exc is None else "%s: %s: %s" % (msg, type(exc).__name__, exc)
    with _ring_lock:
        _ring.append((time.time(), "E", text))
    _android_log("%s: %s" % (TAG, text))
    if trace and exc is not None:
        for line in traceback_lines():
            _android_log("%s:   %s" % (TAG, line))
            with _ring_lock:
                _ring.append((time.time(), "E", "  " + line))


def traceback_lines():
    """Frames of the exception being handled, innermost last. Empty outside
    an except block."""
    import traceback

    text = traceback.format_exc()
    if not text or text.startswith("NoneType"):
        return []
    return [line for line in text.rstrip().split("\n") if line.strip()]


def tail(count=50, level=None):
    """Last `count` buffered lines, newest last. `level` filters by I/D/E."""
    with _ring_lock:
        rows = list(_ring)
    if level:
        rows = [r for r in rows if r[1] == level.upper()]
    return rows[-count:]


def formatted_tail(count=50, level=None):
    out = []
    for ts, lvl, text in tail(count, level):
        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
        out.append("%s %s %s" % (stamp, lvl, text))
    return out


def clear():
    with _ring_lock:
        _ring.clear()
