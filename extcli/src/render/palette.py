# SPDX-License-Identifier: Apache-2.0

"""Terminal palette: nine semantic roles plus the sixteen ANSI colors.

The default theme mirrors the client, so this module takes a {role: color}
mapping from compat/theme.py and derives the ANSI ramp from it — a red that
matches the client's red, a dim that matches its gray. Themes that do not
follow the client (Amoled, user files) provide their own values instead.

Colors are ARGB ints, as Android expects. No Android imports here so the math
stays testable.
"""

ROLE_ORDER = ("bg", "fg", "dim", "accent", "error", "success", "warn",
              "selection", "divider")

# ANSI slots, in the order the renderer expects them
ANSI_ORDER = ("black", "red", "green", "yellow", "blue", "magenta", "cyan",
              "white", "bright_black", "bright_red", "bright_green",
              "bright_yellow", "bright_blue", "bright_magenta", "bright_cyan",
              "bright_white")

MODE_CLIENT = "client"   # follow the client theme
MODE_MONO = "mono"       # one hue, luminance only (Amoled)
MODE_FIXED = "fixed"     # values come from a theme file


INT32_MIN = -0x80000000
INT32_MAX = 0x7FFFFFFF


def signed(value):
    """Android colors are signed 32-bit ints.

    Packing 0xFF1A1B20 the obvious way gives 4279898400, which no Java `int`
    can hold: passing it to setBackgroundColor raises OverflowError and the
    whole view fails to build. Every color that leaves this module goes through
    here.
    """
    value = int(value) & 0xFFFFFFFF
    return value - 0x100000000 if value > INT32_MAX else value


def argb(a, r, g, b):
    return signed(((a & 255) << 24) | ((r & 255) << 16) | ((g & 255) << 8)
                  | (b & 255))


def parts(color):
    color = int(color) & 0xFFFFFFFF
    return ((color >> 24) & 255, (color >> 16) & 255, (color >> 8) & 255, color & 255)


def luminance(color):
    _, r, g, b = parts(color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def mix(color_a, color_b, t):
    """Linear blend; t=0 gives color_a, t=1 gives color_b."""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    aa, ar, ag, ab = parts(color_a)
    ba, br, bg, bb = parts(color_b)
    return argb(
        int(aa + (ba - aa) * t),
        int(ar + (br - ar) * t),
        int(ag + (bg - ag) * t),
        int(ab + (bb - ab) * t),
    )


def brighten(color, amount=0.25):
    """Toward white, for the bright half of the ANSI ramp."""
    return mix(color, argb(255, 255, 255, 255), amount)


def darken(color, amount=0.25):
    return mix(color, argb(255, 0, 0, 0), amount)


def opaque(color):
    """Drops transparency: a terminal cell has no idea what is behind it."""
    _, r, g, b = parts(color)
    return argb(255, r, g, b)


def derive_ansi(roles):
    """Builds the sixteen ANSI colors out of the semantic roles.

    The client theme gives us red/green/orange/blue that already look right in
    it; magenta and cyan are interpolated from the accent so the ramp stays in
    the same family instead of jumping to saturated primaries.
    """
    fg = opaque(roles["fg"])
    bg = opaque(roles["bg"])
    dim = opaque(roles["dim"])
    accent = opaque(roles["accent"])
    red = opaque(roles["error"])
    green = opaque(roles["success"])
    yellow = opaque(roles["warn"])
    magenta = mix(accent, red, 0.45)
    cyan = mix(accent, green, 0.4)

    base = {
        "black": mix(bg, fg, 0.25),
        "red": red,
        "green": green,
        "yellow": yellow,
        "blue": accent,
        "magenta": magenta,
        "cyan": cyan,
        "white": dim,
    }
    out = dict(base)
    for name, color in base.items():
        out["bright_" + name] = brighten(color, 0.3)
    out["bright_white"] = fg
    out["bright_black"] = dim
    return out


def to_mono(roles):
    """Amoled: pure black background, everything else by luminance."""
    fg = opaque(roles["fg"])
    black = argb(255, 0, 0, 0)
    white = argb(255, 255, 255, 255)
    out = {
        "bg": black,
        "fg": white if luminance(fg) > 0.5 else mix(white, black, 0.15),
        "dim": argb(255, 130, 130, 130),
        "accent": white,
        "error": argb(255, 200, 200, 200),
        "success": argb(255, 200, 200, 200),
        "warn": argb(255, 200, 200, 200),
        "selection": argb(255, 40, 40, 40),
        "divider": argb(255, 60, 60, 60),
    }
    return out


class Palette(object):
    """A resolved palette, ready to hand to the renderer."""

    def __init__(self, roles, ansi=None, name="default", mode=MODE_CLIENT):
        self.name = name
        self.mode = mode
        self.roles = {key: opaque(roles[key]) for key in ROLE_ORDER}
        self.ansi = dict(ansi) if ansi else derive_ansi(self.roles)

    def role(self, name):
        return self.roles.get(name, self.roles["fg"])

    def ansi_color(self, index):
        """One of the 256 colours a program can ask for by number.

        The first sixteen are the theme's own, so a program's red is the red
        the rest of the console uses. Above those the numbers are not a palette
        anybody chose — they are an arithmetic every terminal agrees on: a
        6x6x6 cube of levels, and then twenty-four greys — so they are computed
        rather than stored.
        """
        index = int(index)
        if 0 <= index < len(ANSI_ORDER):
            return self.ansi[ANSI_ORDER[index]]
        if 16 <= index <= 231:
            index -= 16
            levels = (0, 95, 135, 175, 215, 255)
            return argb(255, levels[(index // 36) % 6],
                        levels[(index // 6) % 6], levels[index % 6])
        if 232 <= index <= 255:
            grey = 8 + (index - 232) * 10
            return argb(255, grey, grey, grey)
        return self.roles["fg"]

    def as_array(self):
        """Flat list matching TerminalNative's palette layout."""
        values = [self.roles[name] for name in ROLE_ORDER]
        values.extend(self.ansi[name] for name in ANSI_ORDER)
        return values

    def describe(self):
        return "%s (%s)" % (self.name, self.mode)


def from_client(roles, name="default"):
    """Palette that follows the client theme."""
    return Palette(roles, None, name, MODE_CLIENT)


def amoled(roles, name="amoled"):
    return Palette(to_mono(roles), None, name, MODE_MONO)


# Termux's own defaults, kept literal on purpose: the point of this theme is
# that a screenshot of extCLI and a screenshot of Termux are the same picture.
TERMUX_ROLES = {
    "bg": 0x000000,
    "fg": 0xFFFFFF,
    "dim": 0x7F7F7F,
    "accent": 0x00CD00,   # the cwd in the prompt
    "error": 0xCD0000,
    "success": 0x00CD00,
    "warn": 0xCDCD00,
    "selection": 0x1C1C1C,
    "divider": 0x2A2A2A,
}

TERMUX_ANSI = {
    "black": 0x000000, "red": 0xCD0000, "green": 0x00CD00, "yellow": 0xCDCD00,
    "blue": 0x0000EE, "magenta": 0xCD00CD, "cyan": 0x00CDCD, "white": 0xE5E5E5,
    "bright_black": 0x7F7F7F, "bright_red": 0xFF0000, "bright_green": 0x00FF00,
    "bright_yellow": 0xFFFF00, "bright_blue": 0x5C5CFF,
    "bright_magenta": 0xFF00FF, "bright_cyan": 0x00FFFF,
    "bright_white": 0xFFFFFF,
}


def termux(name="termux"):
    """Black background, white text, the standard sixteen colors."""
    ansi = {key: opaque(value) for key, value in TERMUX_ANSI.items()}
    return Palette(TERMUX_ROLES, ansi, name, MODE_FIXED)


def from_theme_file(data, client_roles=None, name=None):
    """Palette from a theme JSON.

    Missing roles fall back to the client's, so a theme can restyle only the
    parts it cares about. Colors may be "#rrggbb", "#aarrggbb" or ints.
    """
    roles = dict(client_roles or {})
    raw_roles = data.get("roles") or {}
    for key in ROLE_ORDER:
        if key in raw_roles:
            roles[key] = parse_color(raw_roles[key])
    missing = [key for key in ROLE_ORDER if key not in roles]
    if missing:
        raise ValueError("theme is missing roles and no client colors given: %s"
                         % ", ".join(missing))

    ansi = None
    raw_ansi = data.get("ansi") or {}
    if raw_ansi:
        derived = derive_ansi(roles)
        ansi = dict(derived)
        for key, value in raw_ansi.items():
            if key in ANSI_ORDER:
                ansi[key] = parse_color(value)

    return Palette(roles, ansi, name or data.get("name") or "custom",
                   data.get("mode") or MODE_FIXED)


def parse_color(value):
    if isinstance(value, int):
        # no alpha byte means the caller wrote 0xrrggbb and wants it opaque
        return opaque(value) if (int(value) & 0xFF000000) == 0 else signed(value)
    text = str(value).strip().lstrip("#")
    if len(text) == 6:
        return argb(255, int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    if len(text) == 8:
        return argb(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16),
                    int(text[6:8], 16))
    if len(text) == 3:
        return argb(255, int(text[0] * 2, 16), int(text[1] * 2, 16), int(text[2] * 2, 16))
    raise ValueError("cannot parse color: %r" % value)
