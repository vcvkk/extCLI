# SPDX-License-Identifier: Apache-2.0

"""Termux's look: the prompt is the working directory and a `$`, nothing else.

Everything below the prompt is `classic` — the block formatting is the same,
only the line the user types on differs. That is the whole difference between
the two styles, and it is why this subclasses rather than repeats.
"""

from .. import blocks
from . import base, classic


class TermuxStyle(classic.ClassicStyle):
    name = "termux"

    def prompt(self, cwd="~", user=None, host=None):
        return "%s $ " % cwd

    def colored_prompt(self, cwd="~", user=None, host=None):
        return "%s %s " % (
            base.colored(cwd, self.color(blocks.ACCENT)),
            base.colored("$", self.color(blocks.FG)),
        )
