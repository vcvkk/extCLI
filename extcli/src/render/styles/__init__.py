# SPDX-License-Identifier: Apache-2.0

"""Output styles.

`classic` is the only one implemented; the plan adds three more later (a
Claude-Code-like marker style, framed panels, and a hybrid). They all consume
the same blocks, so adding one is a new module here and nothing else.
"""

from . import classic
from . import termux

STYLES = {
    "classic": classic.ClassicStyle,
    "termux": termux.TermuxStyle,
}

DEFAULT = "classic"

# What the on-screen console uses. It differs from DEFAULT because DEFAULT is
# also what non-interactive output goes through (`.cli` in a chat, pipes), and
# there a `~ $` prompt would mean nothing.
CONSOLE_DEFAULT = "termux"


def get(name):
    return STYLES.get(name or DEFAULT, STYLES[DEFAULT])


def names():
    return sorted(STYLES)
