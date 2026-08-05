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
ROWS = (
    (("ESC", "cancel"), ("/", "insert:/"), ("—", "insert:-"), ("HOME", "home"),
     ("↑", "history_prev"), ("END", "end"), ("PGUP", "page_up")),
    (("TAB", "complete"), ("CTRL", "ctrl"), ("ALT", "alt"), ("←", "left"),
     ("↓", "history_next"), ("→", "right"), ("PGDN", "page_down")),
)


def build(activity, palette, session):
    """Returns the two key rows and wires CTRL's indicator to the session."""
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

    for keys in ROWS:
        row = LinearLayout(activity)
        row.setOrientation(LinearLayout.HORIZONTAL)
        for label, action in keys:
            key = TextView(activity)
            key.setText(label)
            key.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
            key.setTextColor(idle)
            key.setGravity(Gravity.CENTER)
            key.setPadding(0, dp(9), 0, dp(9))
            key.setAllCaps(False)
            if typeface is not None:
                key.setTypeface(typeface)
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

    session.set_ctrl_indicator = indicate
    return container


def _hold(key, action, session):
    """Makes one key go on doing its thing while it is held down.

    The touch is taken rather than the click, because a click is only the end
    of a press and says nothing about how long it lasted. Taking it means this
    has to do what the click did — the first press acts at once — and it means
    the key draws itself as pressed by hand.
    """
    from android.view import View
    from java import dynamic_proxy

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

    class _Touch(dynamic_proxy(View.OnTouchListener)):
        def onTouch(self, view, event):
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
        key.setOnTouchListener(_Touch())
    except Exception as e:
        log.error("softkeys: cannot hold %s" % action, e)


def _inside(view, event):
    x, y = float(event.getX()), float(event.getY())
    return 0 <= x <= view.getWidth() and 0 <= y <= view.getHeight()


def _runnable(function):
    from java import dynamic_proxy
    from java.lang import Runnable

    class _Run(dynamic_proxy(Runnable)):
        def run(self):
            try:
                function()
            except Exception as e:
                log.error("softkeys: repeat failed", e)

    return _Run()


def _handler(action, session):
    def handler(view):
        try:
            session.on_softkey(action)
        except Exception as e:
            log.error("softkeys: %s failed" % action, e)

    return handler
