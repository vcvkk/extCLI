# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""The extra key rows under the terminal.

Same two rows as Termux, in the same order, because that is the layout a
terminal user's thumbs already know: a phone keyboard has no tab, no escape,
no arrows, and the console is unusable without them.

    ESC  /  —  HOME  ↑  END  PGUP
    TAB CTRL ALT  ←  ↓  →   PGDN

Keys are flat text on the background, evenly divided across the width. CTRL is
the one with state: it latches, the next character typed becomes a control
combination, and it un-latches itself.
"""

from ..compat import fonts
from ..utils import log

# The keys that go on repeating while they are held. Moving through a line one
# tap at a time is what a phone keyboard's own arrows never make you do, and an
# editor is unusable at one character per tap. Paging is here for the same
# reason; nothing that acts on a whole line is, because holding one of those
# by accident should not run away with the text.
REPEATING = ("left", "right", "history_prev", "history_next",
             "page_up", "page_down")

# how long a key waits before it starts repeating, and how fast it goes then.
# The first number is a deliberate pause: it is what tells a tap from a hold.
FIRST_REPEAT = 400
NEXT_REPEAT = 55

# (label, action name) — the console maps action names to behaviour
DEFAULT_ROWS = (
    (("ESC", "cancel"), ("/", "insert:/"), ("—", "insert:-"), ("HOME", "home"),
     ("↑", "history_prev"), ("END", "end"), ("PGUP", "page_up")),
    (("TAB", "complete"), ("CTRL", "ctrl"), ("ALT", "alt"), ("←", "left"),
     ("↓", "history_next"), ("→", "right"), ("PGDN", "page_down")),
)

# Every key that can be put on a row, and what it does. The console decides
# behaviour from the action name, so this is the whole vocabulary: anything
# not here would be a key that does nothing when pressed.
CATALOGUE = (
    ("cancel", "ESC", "escape, or clear the line"),
    ("complete", "TAB", "complete the word"),
    ("ctrl", "CTRL", "the next key is a control combination"),
    ("alt", "ALT", "meta, for a keyboard that has one"),
    ("left", "←", "move the caret left"),
    ("right", "→", "move the caret right"),
    ("history_prev", "↑", "the command before"),
    ("history_next", "↓", "the command after"),
    ("home", "HOME", "start of the line"),
    ("end", "END", "end of the line"),
    ("page_up", "PGUP", "scroll up"),
    ("page_down", "PGDN", "scroll down"),
    ("clear", "CLR", "clear what is typed"),
    ("insert:/", "/", "type a slash"),
    ("insert:-", "—", "type a dash"),
    ("insert:|", "|", "type a pipe"),
    ("insert:~", "~", "type a tilde"),
    ("insert:$", "$", "type a dollar"),
    ("insert:*", "*", "type a star"),
    ("insert:.", ".", "type a dot"),
    ("insert:'", "\'", "type a quote"),
    ('insert:"', '"', "type a double quote"),
)

ACTIONS = tuple(action for action, _label, _about in CATALOGUE)
LABELS = {action: label for action, label, _about in CATALOGUE}

# How many keys fit on one row before they are too narrow to hit. Seven is what
# the default rows use and what a phone comfortably holds.
MAX_PER_ROW = 8


def serialise(rows):
    """Rows as one line of text, for the settings store.

    Actions only: the label of a key is not the user's to change, and storing
    it would mean a key whose caption and behaviour could drift apart.
    """
    return "|".join(",".join(action for _label, action in row) for row in rows)


def parse(text):
    """Rows back out of the settings store, or None when it says nothing.

    Anything unknown is dropped rather than kept: an action this build does
    not have would be a key that silently does nothing, and a row of those is
    worse than the default row.
    """
    if not text:
        return None
    rows = []
    for part in str(text).split("|"):
        keys = [action for action in part.split(",")
                if action and action in LABELS]
        if keys:
            rows.append(tuple((LABELS[action], action)
                              for action in keys[:MAX_PER_ROW]))
    return tuple(rows) or None


def rows():
    """The rows to draw: the user's if they have set any, else the defaults."""
    from . import prefs

    try:
        return parse(prefs.softkey_layout()) or DEFAULT_ROWS
    except Exception:
        return DEFAULT_ROWS


def build(activity, palette, session, layout=None, on_key=None):
    """Returns the two key rows and wires CTRL's indicator to the session.

    `layout` and `on_key` are for the settings editor, which draws the same
    rows so that what is being arranged and what will appear are the same
    thing rather than two descriptions of it. With `on_key` a tap calls
    `on_key(action, row, index)` instead of doing what the key does, and
    holding one repeats nothing — in an editor a held key should open once,
    not forty times. The row and the index are how it was drawn, not what the
    action is, because the same action twice on a row is a thing somebody may
    quite reasonably want and a tap on either of them has to say which.
    """
    from android.util import TypedValue
    from android.view import Gravity
    from android.widget import LinearLayout, TextView
    from android_utils import OnClickListener
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    typeface = fonts.mono_typeface()
    # white, like Termux; a latched CTRL turns the accent color
    idle, active = palette.role("fg"), palette.role("accent")

    container = LinearLayout(activity)
    container.setOrientation(LinearLayout.VERTICAL)
    container.setBackgroundColor(palette.role("bg"))
    container.setPadding(0, dp(2), 0, dp(4))

    ctrl_keys = []

    for number, keys in enumerate(layout or rows()):
        row = LinearLayout(activity)
        row.setOrientation(LinearLayout.HORIZONTAL)
        for index, (label, action) in enumerate(keys):
            key = TextView(activity)
            key.setText(label)
            key.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
            key.setTextColor(idle)
            key.setGravity(Gravity.CENTER)
            key.setPadding(0, dp(9), 0, dp(9))
            key.setAllCaps(False)
            if typeface is not None:
                key.setTypeface(typeface)
            if on_key is not None:
                key.setOnClickListener(
                    OnClickListener(_reporter(on_key, action, number, index)))
            else:
                key.setOnClickListener(OnClickListener(_handler(action, session)))
                if action in REPEATING:
                    _hold(key, action, session)
            if action == "ctrl":
                ctrl_keys.append(key)
            # weight 1 with zero width: seven equal columns, whatever the screen
            params = LinearLayout.LayoutParams(0, -2)
            params.weight = 1.0
            row.addView(key, params)
        container.addView(row, LinearLayout.LayoutParams(-1, -2))

    def indicate(armed):
        for key in ctrl_keys:
            try:
                key.setTextColor(active if armed else idle)
            except Exception:
                pass

    if on_key is None:
        session.set_ctrl_indicator = indicate
    return container


def _reporter(on_key, action, number, index):
    """A tap that says which key it was, for the editor."""

    def handler(view):
        try:
            on_key(action, number, index)
        except Exception as e:
            log.error("softkeys: cannot open the key editor", e)

    return handler


def _hold(key, action, session):
    """Makes one key go on doing its thing while it is held down.

    The touch is taken rather than the click, because a click is only the end
    of a press and says nothing about how long it lasted. Taking it means this
    has to do what the click did — the first press acts at once — and it means
    the key draws itself as pressed by hand.
    """
    from ..compat import proxies

    held = {"on": False}

    def again():
        if not held["on"]:
            return
        try:
            session.on_softkey(action)
        except Exception as e:
            log.error("softkeys: %s failed" % action, e)
            held["on"] = False
            return
        key.postDelayed(_runnable(again), NEXT_REPEAT)

    def stop(view):
        held["on"] = False
        try:
            view.setPressed(False)
        except Exception:
            pass

    def touched(view, event):
        try:
            kind = int(event.getActionMasked())
            if kind == 0:                      # ACTION_DOWN
                held["on"] = True
                view.setPressed(True)
                session.on_softkey(action)
                view.postDelayed(_runnable(again), FIRST_REPEAT)
                return True
            if kind == 2:                      # ACTION_MOVE
                # a finger that has slid off the key is no longer on it
                if held["on"] and not _inside(view, event):
                    stop(view)
                return True
            if kind in (1, 3):                 # ACTION_UP, ACTION_CANCEL
                stop(view)
                return True
        except Exception as e:
            log.error("softkeys: holding %s failed" % action, e)
            held["on"] = False
        return False

    try:
        key.setOnTouchListener(proxies.touch_listener(touched))
    except Exception as e:
        log.error("softkeys: cannot hold %s" % action, e)


def _inside(view, event):
    x, y = float(event.getX()), float(event.getY())
    return 0 <= x <= view.getWidth() and 0 <= y <= view.getHeight()


def _runnable(function):
    """A Runnable for the repeat timer.

    This is posted again on every tick of a held key, so a class defined here
    would have been dozens of them for one long press.
    """
    from ..compat import proxies

    return proxies.runnable(function)


def _handler(action, session):
    def handler(view):
        try:
            session.on_softkey(action)
        except Exception as e:
            log.error("softkeys: %s failed" % action, e)

    return handler
