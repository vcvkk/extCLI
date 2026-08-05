# SPDX-License-Identifier: Apache-2.0

"""Classic terminal style: a `user@host:cwd$` prompt and plain colored text.

Chosen as the first style because it is the one that mixes naturally with real
`/system/bin/sh` output — the shell backend writes its own text into the same
scrollback, and nothing here decorates it into something it is not.
"""

from .. import blocks
from . import base


class ClassicStyle(base.Style):
    name = "classic"

    USER = "extcli"
    HOST = "exteraGram"

    def prompt(self, cwd="~", user=None, host=None):
        return "%s@%s:%s$ " % (user or self.USER, host or self.HOST, cwd)

    def colored_prompt(self, cwd="~", user=None, host=None):
        accent = self.color(blocks.ACCENT)
        dim = self.color(blocks.DIM)
        return "%s%s%s " % (
            base.colored("%s@%s" % (user or self.USER, host or self.HOST), accent),
            base.colored(":%s" % cwd, dim),
            base.colored("$", dim),
        )

    def echo(self, command, cwd="~"):
        """The command line as it appears in the scrollback."""
        return "%s%s" % (self.colored_prompt(cwd),
                         base.colored(command, self.color(blocks.FG)))

    # ------------------------------------------------------------- block kinds

    def block_text(self, block):
        color = self.color(block.role)
        out = []
        for line in block.lines:
            for wrapped in base.wrap(line, self.width):
                out.append(base.colored(wrapped, color) if wrapped else "")
        return out

    def block_summary(self, block):
        # classic shells put the count on its own plain line, no marker
        return [base.colored(w, self.color(block.role))
                for w in base.wrap(block.text, self.width)]

    def block_error(self, block):
        out = [base.colored("error: %s" % block.message, self.color(blocks.ERROR))]
        if block.hint:
            for line in base.wrap(block.hint, self.width, indent=7):
                out.append(base.colored(line, self.color(blocks.DIM)))
        return out

    def block_fields(self, block):
        out = []
        if block.title:
            out.append(base.colored(block.title, self.color(blocks.ACCENT), bold=True))
        label_width = min(
            max((len(label) for label, _, _ in block.rows), default=0),
            max(self.width // 2, 8),
        )
        for label, value, role in block.rows:
            # colon hugs the label, padding goes after it
            head = (base.clip(label, label_width) + ":").ljust(label_width + 1)
            room = self.width - label_width - 2
            wrapped = base.wrap(value, max(room, base.MIN_WIDTH))
            first = wrapped[0] if wrapped else ""
            out.append("%s %s" % (
                base.colored(head, self.color(blocks.DIM)),
                base.colored(first, self.color(role)),
            ))
            for extra in wrapped[1:]:
                out.append("%s %s" % (" " * (label_width + 1),
                                      base.colored(extra, self.color(role))))
        return out

    def block_table(self, block):
        widths = base.column_widths(block.rows, block.header, self.width)
        out = []
        if block.header:
            out.append(base.colored(
                base.format_row(block.header, widths, block.aligns),
                self.color(blocks.DIM),
            ))
        for row in block.rows:
            out.append(base.colored(
                base.format_row(row, widths, block.aligns),
                self.color(blocks.FG),
            ))
        return out

    def block_items(self, block):
        """`name  detail  [on]` — the state marker goes last, like a status flag."""
        markers = {"on": "[on]", "off": "[off]", "warn": "[!]", None: ""}
        marker_width = max(
            (len(markers.get(state, "")) for _, _, state in block.entries), default=0
        )
        name_width = max((len(name) for name, _, _ in block.entries), default=0)
        detail_width = max((len(detail) for _, detail, _ in block.entries), default=0)

        # names first, then details, then the marker; shrink details when tight
        room = self.width - marker_width - 2
        name_width = min(name_width, max(room - 4, 8))
        detail_width = min(detail_width, max(room - name_width - 2, 0))

        out = []
        for name, detail, state in block.entries:
            marker = markers.get(state, "")
            role = blocks.FG if state != "off" else blocks.DIM
            line = base.clip(name, name_width).ljust(name_width)
            text = base.colored(line, self.color(role))
            if detail and detail_width > 0:
                text += "  " + base.colored(
                    base.clip(detail, detail_width).ljust(detail_width),
                    self.color(blocks.DIM),
                )
            if marker:
                marker_role = {"on": blocks.SUCCESS, "off": blocks.DIM,
                               "warn": blocks.WARN}.get(state, blocks.DIM)
                text += "  " + base.colored(marker.rjust(marker_width),
                                            self.color(marker_role))
            out.append(text)
        return out
