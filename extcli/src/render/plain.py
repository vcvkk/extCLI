# SPDX-License-Identifier: Apache-2.0

"""Blocks as plain text, for pipes, redirection and clipboard.

A command that writes into a pipe must not emit escape codes or padding meant
for a screen: `plugin list | grep extcli` has to see plain lines. So blocks are
rendered twice by different renderers — this one, and the styled one that draws
to the terminal.
"""

from . import blocks


def render(result):
    """Result -> list of plain lines."""
    lines = []
    for block in result:
        lines.extend(render_block(block))
    return lines


def text(result):
    return "\n".join(render(result))


def render_block(block):
    kind = block.kind
    if kind == "text":
        return list(block.lines)
    if kind == "summary":
        return [block.text]
    if kind == "error":
        out = ["error: %s" % block.message]
        if block.hint:
            out.append(block.hint)
        return out
    if kind == "fields":
        return _fields(block)
    if kind == "table":
        return _table(block)
    if kind == "items":
        return _items(block)
    if kind == "blank":
        return [""]
    return [str(block)]


def _fields(block):
    out = []
    if block.title:
        out.append(block.title)
    width = max((len(label) for label, _, _ in block.rows), default=0)
    for label, value, _role in block.rows:
        out.append("%s %s" % ((label + ":").ljust(width + 1), value))
    return out


def _table(block):
    rows = ([block.header] if block.header else []) + block.rows
    if not rows:
        return []
    columns = max(len(row) for row in rows)
    widths = [0] * columns
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    out = []
    for row in rows:
        cells = [str(row[i]).ljust(widths[i]) if i < len(row) else " " * widths[i]
                 for i in range(columns)]
        out.append("  ".join(cells).rstrip())
    return out


def _items(block):
    markers = {"on": "[on]", "off": "[off]", "warn": "[!]", None: ""}
    out = []
    for name, detail, state in block.entries:
        parts = [name]
        if detail:
            parts.append(detail)
        marker = markers.get(state, "")
        if marker:
            parts.append(marker)
        out.append("  ".join(parts))
    return out
