# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Arranging the extra key rows.

The rows under the terminal are Termux's, and Termux's are right for Termux.
They are not right for everybody: somebody who lives in vi wants escape twice
the size and no page keys at all, somebody writing shell wants a pipe and a
dollar where the arrows are, and somebody on a small phone wants five keys
they can hit rather than seven they cannot.

What makes this worth a screen of its own is that the thing being arranged is
drawn by the same code that draws the real rows — `softkeys.build`, with a
layout handed to it and taps reported instead of acted on. So the preview is
not a picture of the keyboard, it *is* the keyboard, in the console's own
colours and font, only wired to open a chooser rather than to type. There is
nothing to be surprised by afterwards.

Everything about what a row may contain — which actions exist, what each is
called, how many fit — is `ui.softkeys`, which has no screen in it and is
tested without one. This module draws that and writes the answer down.
"""

from ..compat import i18n
from ..utils import log
from . import dialogs, prefs, softkeys

# The slot at the end of a row that adds a key to it. It goes through the same
# layout the real keys do, so it lines up with them exactly; the action name is
# not in the catalogue and could never be stored.
ADD = "\x00add"
ADD_LABEL = "+"

# The prefix of every key that simply types a character.
INSERT = "insert:"

# How many rows there may be. Two is Termux and the default; three is as far as
# a phone can go before the terminal is a strip at the top.
MAX_ROWS = 3

# The preview's frame, and how much of the text colour is mixed into the
# background of a block in the chooser.
CARD_RADIUS = 16
SURFACE_STRENGTH = 0.07
ROW_HEIGHT = 46

# What the preview shows above the keys, so the block reads as a console rather
# than as a floating row of words.
SAMPLE = "~ $ "


def _s(key, fallback):
    return i18n.get(key, fallback)


def edit(activity=None):
    """Opens the editor. Returns True if it got on screen."""
    activity = activity or _activity()
    if activity is None:
        dialogs.toast(_s("tools_no_screen", "Open this from the app"))
        return False
    try:
        return _Editor(activity).show()
    except Exception as e:
        log.error("keyrows: cannot open the editor", e)
        dialogs.toast("%s: %s" % (type(e).__name__, e), error=True)
        return False


def _activity():
    try:
        from client_utils import get_last_fragment

        fragment = get_last_fragment()
        return fragment.getParentActivity() if fragment else None
    except Exception:
        return None


# --------------------------------------------------------------- the editor


class _Editor(object):
    """The dialog: a live keyboard, and the rules about what can go in it."""

    def __init__(self, activity):
        self.activity = activity
        self.rows = [list(row) for row in softkeys.rows()]
        self.holder = None
        self.keys = None

    # ------------------------------------------------------------- building

    def show(self):
        from android.widget import LinearLayout
        from org.telegram.messenger import AndroidUtilities
        from ui.alert import AlertDialogBuilder

        dp = AndroidUtilities.dp
        colours = _colours()

        body = LinearLayout(self.activity)
        body.setOrientation(LinearLayout.VERTICAL)
        body.setPadding(dp(12), 0, dp(12), 0)

        self.holder = LinearLayout(self.activity)
        self.holder.setOrientation(LinearLayout.VERTICAL)
        self.holder.setBackground(_card(_palette().role("bg"), CARD_RADIUS))
        self.holder.addView(self._sample(), LinearLayout.LayoutParams(-1, -2))
        body.addView(self.holder, LinearLayout.LayoutParams(-1, -2))
        self._draw()

        body.addView(_note(self.activity, colours,
                           _s("keys_hint",
                              "Tap a key to change it, + to add one.")),
                     LinearLayout.LayoutParams(-1, -2))
        body.addView(_action(self.activity, colours,
                             _s("keys_reset", "Back to the default rows"),
                             self._reset),
                     LinearLayout.LayoutParams(-1, -2))

        builder = AlertDialogBuilder(self.activity)
        builder.set_title(_s("keys_title", "Key rows"))
        builder.set_view(body)
        builder.set_positive_button(_s("keys_save", "Save"),
                                    lambda dialog, which: self._save(dialog))
        builder.set_negative_button(_s("cancel_button", "Cancel"),
                                    lambda dialog, which: dialog.dismiss())
        builder.show()
        return True

    def _sample(self):
        """A prompt over the keys, in the console's own font and colours."""
        from android.util import TypedValue
        from android.widget import TextView
        from org.telegram.messenger import AndroidUtilities

        from ..compat import fonts

        palette = _palette()
        dp = AndroidUtilities.dp
        view = TextView(self.activity)
        view.setText(SAMPLE)
        view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
        view.setTextColor(palette.role("dim"))
        view.setPadding(dp(10), dp(10), dp(10), dp(10))
        typeface = fonts.mono_typeface()
        if typeface is not None:
            view.setTypeface(typeface)
        return view

    def _draw(self):
        """Puts the keyboard back the way `self.rows` now says it is.

        Rebuilt rather than patched: the rows are laid out by weight, so one
        key leaving changes the width of every other key on its row, and there
        is nothing to be gained by trying to do that by hand.
        """
        if self.holder is None:
            return
        from android.widget import LinearLayout

        try:
            if self.keys is not None:
                self.holder.removeView(self.keys)
            self.keys = softkeys.build(self.activity, _palette(), None,
                                       layout=self._layout(),
                                       on_key=self._tapped)
            self.holder.addView(self.keys, LinearLayout.LayoutParams(-1, -2))
        except Exception as e:
            log.error("keyrows: cannot draw the preview", e)

    def _layout(self):
        """What to draw: the rows, each with an add slot where one fits.

        The empty row at the end is the one way to get a third row, and it is
        only offered while there is a row to spare — an editor that shows a
        row that cannot exist is worse than one that shows none.
        """
        drawn = []
        for row in self.rows:
            keys = list(row)
            if len(keys) < softkeys.MAX_PER_ROW:
                keys.append((ADD_LABEL, ADD))
            drawn.append(tuple(keys))
        if len(drawn) < MAX_ROWS and self.rows and self.rows[-1]:
            drawn.append(((ADD_LABEL, ADD),))
        return tuple(drawn)

    # ------------------------------------------------------------- tapping

    def _tapped(self, action, number, index):
        """A key in the preview was pressed.

        The row and the position are the ones it was drawn at, and what was
        drawn is the real rows each with an add slot on the end — so a
        position inside a real row is that key, and anything past it is the
        slot, which is the one case where the answer is "there is no key here
        yet".
        """
        if action == ADD:
            self._add(number)
            return
        if number < len(self.rows) and index < len(self.rows[number]):
            self._change(number, index)
            return
        # the preview and the rows have come apart, which should not happen;
        # redrawing puts them back rather than acting on the wrong key
        log.log("keyrows: tapped %s at %d/%d and could not place it"
                % (action, number, index), debug=True)
        self._draw()

    def _add(self, number):
        _Chooser(self.activity, _s("keys_add", "Add a key"),
                 current=None,
                 on_pick=lambda picked: self._insert(number, picked)).show()

    def _insert(self, number, action):
        while len(self.rows) <= number:
            self.rows.append([])
        self.rows[number].append((softkeys.LABELS[action], action))
        self._draw()

    def _change(self, number, index):
        label, action = self.rows[number][index]
        _Chooser(
            self.activity, label, current=action,
            on_pick=lambda picked: self._replace(number, index, picked),
            on_move=lambda step: self._move(number, index, step),
            on_remove=lambda: self._remove(number, index),
            movable=len(self.rows[number]) > 1).show()

    def _replace(self, number, index, action):
        self.rows[number][index] = (softkeys.LABELS[action], action)
        self._draw()

    def _move(self, number, index, step):
        row = self.rows[number]
        target = index + step
        if 0 <= target < len(row):
            row[index], row[target] = row[target], row[index]
            self._draw()

    def _remove(self, number, index):
        """Takes a key out, and the row with it if that was the last one.

        Never the last row: a keyboard with no keys is not an arrangement
        anybody meant to make, and there would be nothing left to tap to undo
        it.
        """
        del self.rows[number][index]
        if not self.rows[number] and len(self.rows) > 1:
            del self.rows[number]
        if not any(self.rows):
            self.rows = [list(row) for row in softkeys.DEFAULT_ROWS]
        self._draw()

    def _reset(self):
        self.rows = [list(row) for row in softkeys.DEFAULT_ROWS]
        self._draw()

    # -------------------------------------------------------------- saving

    def _save(self, dialog):
        """Writes the arrangement down, or clears it when it is the default.

        Storing nothing for the default rows is deliberate: it means a build
        that changes them changes them for everybody who never touched this
        screen, rather than leaving them with a copy of what the default used
        to be.
        """
        try:
            dialog.dismiss()
        except Exception:
            pass
        wanted = tuple(tuple(row) for row in self.rows if row)
        default = tuple(tuple(row) for row in softkeys.DEFAULT_ROWS)
        text = "" if wanted == default else softkeys.serialise(wanted)
        try:
            prefs.remember_softkeys(text)
        except Exception as e:
            log.error("keyrows: cannot save the layout", e)
            dialogs.toast("%s: %s" % (type(e).__name__, e), error=True)
            return
        dialogs.toast(_s("keys_saved", "Reopen the console to see the change"))


# -------------------------------------------------------------- the chooser


class _Chooser(object):
    """What one key may become, and what may be done to it where it is.

    Every action the console understands, each with its caption drawn the way
    the key itself is drawn, so picking one is picking something already seen
    rather than reading a name and hoping.
    """

    LIST_MAX = 320

    def __init__(self, activity, title, current=None, on_pick=None,
                 on_move=None, on_remove=None, movable=False):
        self.activity = activity
        self.title = title
        self.current = current
        self.on_pick = on_pick
        self.on_move = on_move
        self.on_remove = on_remove
        self.movable = movable
        self.builder = None
        self.shown = None

    def show(self):
        from android.widget import LinearLayout, ScrollView
        from org.telegram.messenger import AndroidUtilities
        from ui.alert import AlertDialogBuilder

        dp = AndroidUtilities.dp
        colours = _colours()

        body = LinearLayout(self.activity)
        body.setOrientation(LinearLayout.VERTICAL)
        body.setPadding(dp(8), 0, dp(8), 0)

        if self.on_remove is not None:
            body.addView(self._actions(colours),
                         LinearLayout.LayoutParams(-1, -2))
            body.addView(_note(self.activity, colours,
                               _s("keys_replace", "Replace with")),
                         LinearLayout.LayoutParams(-1, -2))

        catalogue = LinearLayout(self.activity)
        catalogue.setOrientation(LinearLayout.VERTICAL)
        last = len(softkeys.CATALOGUE) - 1
        for position, (action, label, about) in enumerate(softkeys.CATALOGUE):
            row = self._entry(colours, action, label, about, position, last)
            catalogue.addView(row, LinearLayout.LayoutParams(-1, -2))

        scroll = ScrollView(self.activity)
        scroll.addView(catalogue, LinearLayout.LayoutParams(-1, -2))
        body.addView(scroll, LinearLayout.LayoutParams(-1, dp(self.LIST_MAX)))
        _fit(scroll, catalogue, dp(self.LIST_MAX))

        builder = AlertDialogBuilder(self.activity)
        builder.set_title(str(self.title))
        builder.set_view(body)
        builder.set_negative_button(_s("cancel_button", "Cancel"),
                                    lambda dialog, which: dialog.dismiss())
        self.builder = builder
        self.shown = builder.show()
        return True

    def _actions(self, colours):
        """Move it, or take it away. Only for a key that is already on a row."""
        from android.widget import LinearLayout
        from org.telegram.messenger import AndroidUtilities

        dp = AndroidUtilities.dp
        block = LinearLayout(self.activity)
        block.setOrientation(LinearLayout.HORIZONTAL)
        block.setPadding(0, 0, 0, dp(4))

        wanted = []
        if self.movable:
            wanted.append(("←", _s("keys_move_left", "Left"),
                           lambda: self._did(self.on_move, -1)))
            wanted.append(("→", _s("keys_move_right", "Right"),
                           lambda: self._did(self.on_move, 1)))
        wanted.append(("✕", _s("keys_remove", "Remove"),
                       lambda: self._did(self.on_remove)))

        last = len(wanted) - 1
        for position, (glyph, label, function) in enumerate(wanted):
            view = _chip(self.activity, colours, glyph, label, function,
                         danger=position == last)
            params = LinearLayout.LayoutParams(0, -2)
            params.weight = 1.0
            if position != last:
                params.rightMargin = dp(4)
            block.addView(view, params)
        return block

    def _entry(self, colours, action, label, about, position, last):
        """One line of the catalogue: the key as drawn, its name, what it does."""
        from android.util import TypedValue
        from android.view import Gravity
        from android.widget import LinearLayout, TextView
        from org.telegram.messenger import AndroidUtilities

        from ..compat import fonts

        dp = AndroidUtilities.dp
        row = LinearLayout(self.activity)
        row.setOrientation(LinearLayout.HORIZONTAL)
        row.setGravity(Gravity.CENTER_VERTICAL)
        row.setPadding(dp(12), dp(6), dp(12), dp(6))
        row.setMinimumHeight(dp(ROW_HEIGHT))
        row.setBackground(_block(colours, position, last + 1))

        chosen = action == self.current

        cap = TextView(self.activity)
        cap.setText(label)
        cap.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 13)
        cap.setTextColor(colours["accent"] if chosen else colours["fg"])
        cap.setGravity(Gravity.CENTER)
        typeface = fonts.mono_typeface()
        if typeface is not None:
            cap.setTypeface(typeface)
        params = LinearLayout.LayoutParams(dp(52), -2)
        row.addView(cap, params)

        text = TextView(self.activity)
        text.setText(_about(action, about))
        text.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 13)
        text.setTextColor(colours["fg"] if chosen else colours["dim"])
        params = LinearLayout.LayoutParams(0, -2)
        params.weight = 1.0
        params.leftMargin = dp(8)
        row.addView(text, params)

        if chosen:
            tick = TextView(self.activity)
            tick.setText("✓")
            tick.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 14)
            tick.setTextColor(colours["accent"])
            row.addView(tick, LinearLayout.LayoutParams(-2, -2))

        _on_click(row, lambda: self._did(self.on_pick, action))
        return row

    def _did(self, function, *args):
        """Carries out one choice and closes; the editor draws the result."""
        _dismiss(self.builder, self.shown)
        if function is None:
            return
        try:
            function(*args)
        except Exception as e:
            log.error("keyrows: that key change failed", e)


def _dismiss(builder, shown):
    """Closes a dialog, however this SDK lets one be closed.

    A row in the list is not one of the dialog's buttons, so nothing hands it
    the dialog to close — it has to be found. `show` returns it on some builds
    and nothing on others, and the builder carries its own `dismiss` on some;
    trying each in turn is what makes the chooser reliably go away instead of
    sitting there over an editor that has already changed underneath it.
    """
    for candidate in (shown, builder):
        if candidate is None:
            continue
        try:
            candidate.dismiss()
            return True
        except Exception:
            continue
    log.log("keyrows: nothing here will close a dialog", debug=True)
    return False


# ------------------------------------------------------------------ drawing


def _about(action, fallback):
    """What a key does, translated.

    The keys that type one character are the whole punctuation half of the
    catalogue and every one of them does the same thing, so they share one
    sentence with the character in it rather than a dozen locale entries that
    would all have to be written again for each new character.
    """
    action = str(action)
    if action.startswith(INSERT):
        return _s("keys_insert", "type %s") % action[len(INSERT):]
    return _s("keys_%s" % action, fallback)


def _chip(activity, colours, glyph, label, function, danger=False):
    """A small rounded button: a glyph over a word."""
    from android.util import TypedValue
    from android.view import Gravity
    from android.widget import LinearLayout, TextView
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    colour = colours["error"] if danger else colours["fg"]
    view = LinearLayout(activity)
    view.setOrientation(LinearLayout.VERTICAL)
    view.setGravity(Gravity.CENTER)
    view.setPadding(0, dp(8), 0, dp(8))
    view.setBackground(_card(colours["surface"], 12))

    top = TextView(activity)
    top.setText(glyph)
    top.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 15)
    top.setTextColor(colour)
    top.setGravity(Gravity.CENTER)
    view.addView(top, LinearLayout.LayoutParams(-1, -2))

    bottom = TextView(activity)
    bottom.setText(label)
    bottom.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 11)
    bottom.setTextColor(colour)
    bottom.setGravity(Gravity.CENTER)
    view.addView(bottom, LinearLayout.LayoutParams(-1, -2))

    _on_click(view, function)
    return view


def _note(activity, colours, text):
    from android.util import TypedValue
    from android.widget import TextView
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    view = TextView(activity)
    view.setText(str(text))
    view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
    view.setTextColor(colours["dim"])
    view.setPadding(dp(4), dp(10), dp(4), dp(6))
    return view


def _action(activity, colours, text, function):
    """A line that does something, in the accent colour so it reads as one."""
    from android.util import TypedValue
    from android.widget import TextView
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    view = TextView(activity)
    view.setText(str(text))
    view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 13)
    view.setTextColor(colours["accent"])
    view.setPadding(dp(4), dp(8), dp(4), dp(8))
    _on_click(view, function)
    return view


def _card(colour, radius):
    """A rounded rectangle of one colour."""
    from org.telegram.messenger import AndroidUtilities

    size = AndroidUtilities.dp(radius)
    try:
        from org.telegram.ui.ActionBar import Theme

        return Theme.createRoundRectDrawable(size, size, size, size,
                                             int(colour))
    except Exception:
        from android.graphics.drawable import GradientDrawable

        drawable = GradientDrawable()
        drawable.setShape(GradientDrawable.RECTANGLE)
        drawable.setColor(int(colour))
        drawable.setCornerRadius(float(size))
        return drawable


def _block(colours, position, count):
    """One row of a grouped list: rounded at the ends, square between."""
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    top = dp(16) if position == 0 else dp(3)
    bottom = dp(16) if position == count - 1 else dp(3)
    colour = int(colours["surface"])
    try:
        from org.telegram.ui.ActionBar import Theme

        return Theme.createRoundRectDrawable(top, top, bottom, bottom, colour)
    except Exception:
        from android.graphics.drawable import GradientDrawable

        drawable = GradientDrawable()
        drawable.setShape(GradientDrawable.RECTANGLE)
        drawable.setColor(colour)
        drawable.setCornerRadii([float(top), float(top), float(top),
                                 float(top), float(bottom), float(bottom),
                                 float(bottom), float(bottom)])
        return drawable


def _fit(scroll, body, maximum):
    """Makes the list as tall as its contents, and no taller."""
    from android.view import ViewTreeObserver
    from java import dynamic_proxy

    class _Fit(dynamic_proxy(ViewTreeObserver.OnPreDrawListener)):
        first = True

        def onPreDraw(self):
            try:
                wanted = min(body.getMeasuredHeight(), maximum)
                params = scroll.getLayoutParams()
                if wanted > 0 and params.height != wanted:
                    params.height = wanted
                    scroll.setLayoutParams(params)
                    if self.first:
                        self.first = False
                        return False
            except Exception:
                pass
            return True

    try:
        scroll.getViewTreeObserver().addOnPreDrawListener(_Fit())
    except Exception as e:
        log.error("keyrows: the list cannot size itself", e)


def _on_click(view, function):
    from android_utils import OnClickListener

    try:
        view.setOnClickListener(OnClickListener(lambda *args: function()))
    except Exception as e:
        log.error("keyrows: cannot wire a row", e)


def _palette():
    """The console's own colours, so the preview is the console."""
    from . import console

    return console.current_palette()


def _colours():
    """The dialog's colours, which are the client's rather than the console's."""
    from ..compat import theme

    try:
        roles = theme.roles()
    except Exception:
        roles = {}
    fg = roles.get("fg", theme.signed(0xFFE6E6E6))
    background = roles.get("bg", theme.signed(0xFF1B1B1B))
    return {
        "fg": fg,
        "dim": roles.get("dim", theme.signed(0xFF8A8A8A)),
        "accent": roles.get("accent", theme.signed(0xFF4EA1F3)),
        "error": roles.get("error", theme.signed(0xFFE05A5A)),
        "surface": theme.mix(fg, background, SURFACE_STRENGTH),
    }
