# SPDX-License-Identifier: Apache-2.0

"""A command's output, in a message that keeps changing.

`.cli <command>` typed into a chat used to open the console and run it there,
which answers a different question from the one that was asked: the point of
typing it into a chat is to see the answer in the chat.

So the output goes into one message that is edited as it arrives. Three things
decide what that message says and when it is written, and all three are here,
away from anything that needs a device:

* **What.** Program output is terminal text — a progress bar is one line
  rewritten forty times a second. Rendering it through the same `Screen` the
  console uses means the message shows the three lines an install ends up
  being, not the twelve hundred it wrote.

* **How much.** Telegram takes 4096 characters. The tail is what matters while
  something is running, so the head is what goes.

* **How often.** Telegram does not publish its edit limits and they vary by
  account, so there is no correct constant to write down. Instead: a floor
  nobody may go under, an interval the user picks above it, no edit at all
  when the text has not changed, and — when the server does object — the wait
  it asks for, honoured exactly and remembered.
"""

from ..render.styles import base

# What one message can hold. Telegram's own limit, and the reason the head of
# a long run is dropped rather than the tail.
MESSAGE_LIMIT = 4096

# Below this, nothing. Telegram publishes no numbers and the real limit moves
# with the account, so this is not "the safe value" — it is the point past
# which asking for trouble stops being accidental.
MIN_INTERVAL = 3.0

DEFAULT_INTERVAL = 5.0

# What is put where the dropped head was.
ELLIPSIS = "…\n"


class LiveText(object):
    """What the message should say at any moment.

    Pure: it is given output and a clock, and answers with text. Nothing here
    sends anything.
    """

    def __init__(self, limit=MESSAGE_LIMIT, interval=DEFAULT_INTERVAL,
                 header=None):
        self.limit = int(limit)
        self.interval = max(float(interval), MIN_INTERVAL)
        self.header = header
        self._screen = base.Screen()
        self._done = []
        self._sent = None
        self._next_allowed = 0.0

    # ----------------------------------------------------------------- input

    def write(self, text):
        """Adds program output, carrying out the cursor moves in it."""
        for line in self._screen.write(str(text)):
            self._done.append(line)
        self._trim()

    def finish(self):
        """The run has ended; everything still on the screen is final."""
        for line in self._screen.finish():
            self._done.append(line)
        while self._done and base.is_blank(self._done[-1]):
            self._done.pop()
        self._trim()

    def _trim(self):
        """Keeps roughly a message's worth of lines, so a long run does not
        grow a list nothing will ever read."""
        while len(self._done) > 200:
            self._done.pop(0)

    # ---------------------------------------------------------------- output

    def text(self):
        """The message body: the tail of the output, plain, within the limit."""
        lines = [base.strip_codes(line) for line in self._done]
        lines += [base.strip_codes(line) for line in self._screen.lines if line]
        body = "\n".join(lines)
        head = "%s\n" % self.header if self.header else ""
        room = self.limit - len(head)
        if len(body) > room:
            body = ELLIPSIS + body[-(room - len(ELLIPSIS)):]
        return head + body

    # ------------------------------------------------------------------ when

    def has_output(self):
        return bool(self._done) or any(self._screen.lines)

    def due(self, now):
        """Is it worth writing to the chat right now?

        No when there is nothing yet — the message already says the command is
        starting, and Telegram will not accept an empty one anyway. No when
        nothing has changed either: an edit that says what the message already
        says spends the account's allowance on nothing.
        """
        if not self.has_output():
            return False
        if now < self._next_allowed:
            return False
        return self.text() != self._sent

    def sent(self, now):
        """Records that the current text is what the message now holds."""
        self._sent = self.text()
        self._next_allowed = now + self.interval

    def defer(self, now, seconds):
        """The server said to wait. It knows better than the interval does."""
        self._next_allowed = max(self._next_allowed, now + float(seconds))
        self.interval = max(self.interval, float(seconds))


def flood_wait_seconds(error):
    """The number in a FLOOD_WAIT_x, or None.

    Telegram answers a rate limit by saying exactly how long to wait. Reading
    it is better than any interval guessed in advance, because it is the only
    number in this whole business that is not a guess.
    """
    text = str(error or "")
    marker = "FLOOD_WAIT_"
    at = text.find(marker)
    if at < 0:
        return None
    digits = ""
    for char in text[at + len(marker):]:
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None
