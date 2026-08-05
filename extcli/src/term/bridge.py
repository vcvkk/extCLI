# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Python side of the terminal widget.

The renderer ships as extcli/dex/terminal.dex and is loaded at runtime. Two
constraints shape the code below:

* The plugin directory is writable, and Android's W^X policy refuses to load a
  writable dex file through DexClassLoader. Reading the bytes and using
  InMemoryDexClassLoader sidesteps that.
* Chaquopy cannot `import dev.vcvkk...` — the class is not on its import path —
  so calls go through java.lang.reflect on the Class object we already hold.
"""

import os

from ..compat import fonts, paths
from ..utils import log

CLASS_NAME = "dev.vcvkk.extcli.terminal.TerminalNative"
DEX_NAME = "terminal"

# must match the constants in TerminalNative.kt
ROLE_BG = 0
ROLE_FG = 1
ROLE_DIM = 2
ROLE_ACCENT = 3
ROLE_ERROR = 4
ROLE_SUCCESS = 5
ROLE_WARN = 6
ROLE_SELECTION = 7
ROLE_DIVIDER = 8
ROLE_COUNT = 9
PALETTE_SIZE = ROLE_COUNT + 16

DEFAULT_TEXT_SIZE_SP = 12.0
DEFAULT_SCROLLBACK = 4000

_STATIC = 0x8  # java.lang.reflect.Modifier.STATIC

_class = None
_load_error = None


def dex_path():
    return os.path.join(paths.dex_dir(), DEX_NAME + ".dex")


def _load_class():
    global _class, _load_error
    if _class is not None or _load_error is not None:
        return _class

    try:
        path = dex_path()
    except Exception as e:
        _load_error = "cannot resolve the dex path: %s" % e
        log.error("term: %s" % _load_error)
        return None
    if not os.path.exists(path):
        _load_error = "%s not found" % path
        log.error("term: %s" % _load_error)
        return None
    try:
        from dalvik.system import InMemoryDexClassLoader
        from java.nio import ByteBuffer
        from org.telegram.messenger import ApplicationLoader

        with open(path, "rb") as f:
            data = f.read()
        parent = ApplicationLoader.applicationContext.getClassLoader()
        loader = InMemoryDexClassLoader(ByteBuffer.wrap(data), parent)
        _class = loader.loadClass(CLASS_NAME)
        log.log("term: loaded %s (%d bytes)" % (CLASS_NAME, len(data)), debug=True)
    except Exception as e:
        _load_error = str(e)
        log.error("term: cannot load %s" % CLASS_NAME, e)
        return None
    return _class


def _call(method, *args):
    """Invokes a static method by name and argument count."""
    cls = _load_class()
    if cls is None:
        raise RuntimeError("terminal renderer unavailable: %s" % _load_error)
    count = len(args)
    for m in cls.getMethods():
        if (m.getName() == method
                and (m.getModifiers() & _STATIC) != 0
                and len(m.getParameterTypes()) == count):
            return m.invoke(None, *args)
    raise RuntimeError("static %s(%d args) not found on %s" % (method, count, CLASS_NAME))


def available():
    return _load_class() is not None


def load_error():
    return _load_error


def self_check():
    """(ok, detail) for the diagnostics report. Never raises: this runs inside
    the diagnostics report, which exists to explain failures."""
    try:
        path = dex_path()
    except Exception as e:
        return False, "cannot resolve the dex path: %s: %s" % (type(e).__name__, e)
    if not os.path.exists(path):
        return False, "dex missing: %s" % path
    try:
        version = int(_call("version"))
    except Exception as e:
        return False, "load failed: %s" % e
    return True, "renderer v%d, dex %d bytes" % (version, os.path.getsize(path))


def int_array(values):
    """Python ints -> Java int[], which is what the renderer takes."""
    from java import jarray, jint

    return jarray(jint)([int(v) for v in values])


# kept for readability at call sites inside this module
_int_array = int_array


def palette_array(roles, ansi=None):
    """Builds the int[] the renderer expects from a {role: color} mapping."""
    order = ("bg", "fg", "dim", "accent", "error", "success", "warn",
             "selection", "divider")
    values = [int(roles.get(name, 0)) for name in order]
    if ansi:
        values.extend(int(c) for c in ansi[:16])
    return _int_array(values)


class Terminal:
    """A live terminal view. Create one per screen and keep the instance."""

    # the dex renderer has no notion of a transient line yet, so the console
    # falls back to a visible input field when this one is in use
    echoes_input = False

    def __init__(self, context, palette, text_size_sp=DEFAULT_TEXT_SIZE_SP,
                 scrollback=DEFAULT_SCROLLBACK):
        from java import jfloat, jint

        typeface = fonts.mono_typeface()
        if typeface is None:
            # the renderer accepts null and falls back itself, but passing a
            # real object keeps the reflective call unambiguous
            from android.graphics import Typeface

            typeface = Typeface.MONOSPACE
        self._root = _call(
            "create",
            context,
            jfloat(float(text_size_sp)),
            typeface,
            palette,
            jint(int(scrollback)),
        )
        log.log("term: created %s" % self.describe(), debug=True)

    @property
    def view(self):
        """The View to put in a layout."""
        return self._root

    def append(self, text):
        _call("append", self._root, str(text))

    def write_line(self, text=""):
        self.append("%s\n" % text)

    def write_lines(self, lines):
        self.append("".join("%s\n" % line for line in lines))

    def blit(self, chars, fg, bg, cols, rows):
        """Pushes one complete frame (TUI mode)."""
        from java import jint

        _call("blit", self._root, _int_array(chars), _int_array(fg),
              _int_array(bg), jint(int(cols)), jint(int(rows)))

    def clear(self):
        _call("clear", self._root)

    def set_palette(self, palette):
        _call("setPalette", self._root, palette)

    def set_text_size(self, text_size_sp):
        from java import jfloat

        _call("setTextSize", self._root, jfloat(float(text_size_sp)))

    def metrics(self):
        """(cols, rows, cell_width_px, cell_height_px)."""
        values = _call("metrics", self._root)
        return tuple(int(v) for v in values)

    def describe(self):
        """Renderer state in one line: mode, grid size, cell size, line count.

        Printed by the console's status line and by `host doctor`, so an empty
        screen can be told apart from an empty scrollback.
        """
        try:
            return str(_call("describe", self._root))
        except Exception as e:
            return "unavailable: %s: %s" % (type(e).__name__, e)

    def scroll_to_bottom(self):
        _call("scrollToBottom", self._root)

    def text(self):
        return str(_call("getText", self._root))

    def release(self):
        try:
            _call("release", self._root)
        except Exception as e:
            log.error("term: release failed", e)
