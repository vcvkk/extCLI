# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Colors, pulled from whatever Telegram theme the user is running.

The console is meant to look like it belongs to the client, so the default
palette is the client's own: background of a chat, its text color, its link
color as the accent. render/palette.py turns these into a terminal palette;
this module only knows how to ask the client.
"""

import ctypes

from ..utils import log

# theme key -> role used by the renderer
_ROLE_KEYS = {
    "bg": "key_windowBackgroundWhite",
    "fg": "key_windowBackgroundWhiteBlackText",
    "dim": "key_windowBackgroundWhiteGrayText",
    "accent": "key_windowBackgroundWhiteBlueText",
    "error": "key_text_RedBold",
    "success": "key_avatar_backgroundGreen",
    "warn": "key_statisticChartLine_orange",
    "selection": "key_chat_inBubbleSelected",
    "divider": "key_divider",
}

_FALLBACK = {
    "bg": 0xFF101010,
    "fg": 0xFFE6E6E6,
    "dim": 0xFF8A8A8A,
    "accent": 0xFF4EA1F3,
    "error": 0xFFE0574B,
    "success": 0xFF5FB85F,
    "warn": 0xFFE0A03C,
    "selection": 0xFF2A3A4A,
    "divider": 0xFF303030,
}


def signed(color):
    """Android colors are signed 32-bit; Python ints are not."""
    return ctypes.c_int32(int(color)).value


# the old name, still used inside this module
_signed = signed


def get_color(theme_key, default=None):
    try:
        from org.telegram.ui.ActionBar import Theme

        key = getattr(Theme, theme_key, None)
        if key is None:
            return default
        return int(Theme.getColor(key))
    except Exception:
        return default


def roles():
    """{role: argb int} for the current client theme."""
    out = {}
    for role, theme_key in _ROLE_KEYS.items():
        color = get_color(theme_key)
        if color is None:
            color = _signed(_FALLBACK[role])
            log.log("theme: %s missing, using fallback for %s" % (theme_key, role),
                    debug=True)
        out[role] = color
    return out


def is_dark():
    try:
        from org.telegram.ui.ActionBar import Theme

        info = Theme.getActiveTheme()
        if info is not None and hasattr(info, "isDark"):
            return bool(info.isDark())
    except Exception:
        pass
    # luminance of the background is a good enough answer
    bg = roles()["bg"] & 0xFFFFFF
    r, g, b = (bg >> 16) & 0xFF, (bg >> 8) & 0xFF, bg & 0xFF
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def alpha(color, a):
    """Same color at a different alpha (0-255)."""
    return _signed((int(a) << 24) | (int(color) & 0x00FFFFFF))


def mix(front, back, weight):
    """`front` laid over `back` at `weight` (0..1), opaque.

    The answer a translucent colour would have given, worked out here instead
    of left to the compositor. It matters wherever something is drawn twice:
    a translucent colour over itself is darker than one draw of it, and the
    overlap shows up as a mark nobody put there.
    """
    weight = 0.0 if weight < 0 else (1.0 if weight > 1 else float(weight))
    front, back = int(front), int(back)
    out = 0xFF
    for shift in (16, 8, 0):
        a = (front >> shift) & 0xFF
        b = (back >> shift) & 0xFF
        out = (out << 8) | int(round(b + (a - b) * weight))
    return _signed(out)
