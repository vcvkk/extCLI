# SPDX-License-Identifier: Apache-2.0

"""Where a command's output goes.

Three destinations matter: the terminal (rich blocks, styled later), another
command (plain text through a pipe) and a file. A command does not choose — the
executor hands it a sink, and the sink decides whether blocks keep their
structure or get flattened.
"""

from ..render import plain


class Sink(object):
    """Base output destination."""

    is_terminal = False

    def write(self, text):
        raise NotImplementedError

    def write_result(self, result):
        """Blocks written to a non-terminal sink lose their styling: a pipe
        wants lines, not colors."""
        text = plain.text(result)
        if text:
            self.write(text + "\n")

    def close(self):
        pass


class MemorySink(Sink):
    """A pipe, or anything else that needs the text back."""

    def __init__(self):
        self.chunks = []

    def write(self, text):
        if text:
            self.chunks.append(str(text))

    @property
    def text(self):
        return "".join(self.chunks)

    def lines(self):
        text = self.text
        return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


class TerminalSink(Sink):
    """The console. Keeps blocks intact so the style can render them.

    With `live` set it hands each piece over as it happens instead of keeping
    everything for the end. That is the difference between watching a command
    work and waiting for it in silence — and it is the same order either way,
    because everything goes through here, text and blocks alike.
    """

    is_terminal = True

    def __init__(self, live=None, live_text=None):
        self.results = []
        self.text_chunks = []
        self.live = live
        # Where a program's own output goes, unaltered. It is terminal text
        # already — its own colours, its own wrapping to a width we gave it,
        # and its own carriage returns, which are how a progress bar rewrites
        # the line it is on. Cut into lines and put back together it stops
        # being any of those things.
        self.live_text = live_text
        # blank lines at the end of what has arrived so far, held back in case
        # that is where the output ends
        self.pending_blanks = 0

    def write(self, text):
        if not text:
            return
        if self.live_text is not None:
            self.live_text(str(text))
            return
        self.text_chunks.append(str(text))
        if self.live is not None and "\n" in str(text):
            self._flush_text()

    def write_result(self, result):
        self._flush_text()
        if self.live is not None:
            self.live(result)
            return
        self.results.append(result)

    def _flush_text(self, final=False):
        if not self.text_chunks:
            return
        from ..render import blocks

        text = "".join(self.text_chunks)
        self.text_chunks = []
        lines = text.split("\n")
        if self.live is not None and not final:
            # the last line has not ended yet; it waits for the rest of itself
            # rather than being shown and then shown again with more on it
            self.text_chunks = [lines.pop()]
        elif text.endswith("\n"):
            lines.pop()
        # Blank lines at the end wait too. A program that finishes by moving
        # its cursor down past a logo ends with a handful of them, and they
        # would push the next prompt down the screen for no reason; one that
        # carries on gets them back, because then they are between things.
        from ..render.styles import base

        held = self.pending_blanks
        self.pending_blanks = 0
        while lines and base.is_blank(lines[-1]):
            lines.pop()
            self.pending_blanks += 1
        if not lines:
            self.pending_blanks += held
            return
        lines = [""] * held + lines
        result = blocks.Result([blocks.Text(lines)])
        if self.live is not None:
            self.live(result)
        else:
            self.results.append(result)

    def collected(self):
        """Everything that was not handed over as it happened."""
        from ..render import blocks

        self._flush_text(final=True)
        merged = blocks.Result()
        for result in self.results:
            for block in result:
                merged.add(block)
        return merged


class FileSink(Sink):
    """A redirection target: > or >>."""

    def __init__(self, path, append=False, encoding="utf-8"):
        self.path = path
        self.handle = open(path, "a" if append else "w", encoding=encoding)

    def write(self, text):
        if text:
            self.handle.write(str(text))

    def close(self):
        try:
            self.handle.close()
        except Exception:
            pass


class NullSink(Sink):
    def write(self, text):
        pass


class Source(object):
    """A command's stdin.

    `is_terminal` tells the two kinds apart, and they behave differently: text
    from a pipe or a file has an end, and a terminal does not. A program given
    the second when it should have had the first waits for ever — which is what
    `ls -la … | grep yaml` did.
    """

    is_terminal = False

    def __init__(self, text="", is_terminal=False):
        self.text = text or ""
        self.is_terminal = bool(is_terminal)

    def read(self):
        return self.text

    def lines(self):
        if not self.text:
            return []
        text = self.text
        return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")

    @classmethod
    def from_file(cls, path, encoding="utf-8"):
        with open(path, "r", encoding=encoding, errors="replace") as handle:
            return cls(handle.read())
