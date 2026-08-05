# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Asking what to put in the container.

The container comes up able to do almost nothing, and the things that make it
worth having are tens to hundreds of megabytes. Downloading them without
asking would be rude on somebody's mobile data; making them find a command
would mean nobody ever has them. So it asks, once, when the container is
ready: a row per group, the first one on, and every row opens to show what is
inside it and let anything be taken out.

What is chosen and what it costs is `rootfs.packages.Selection`, which knows
nothing about screens and is tested without one. This module only draws it.
"""

from ..compat import i18n
from ..rootfs import packages
from ..utils import log

# how the rows look, in dp. The tick is 21 because that is the size the
# client uses for the one in its own plugin-install sheet.
ROW_HEIGHT = 48
INDENT = 32
CHECK_SIZE = 21
# as tall as the list is allowed to get before it starts scrolling; below that
# it is exactly as tall as what is in it, so there is no empty strip under the
# last container
LIST_MAX = 360

# How the tick is drawn. The client passes 10 for the one in its plugin-install
# sheet, and the number matters more than it looks: it picks which of
# CheckBoxBase's ways of drawing is used, and the default one is the green tick
# from a message, which ignores the colours it is given.
ARC_STYLE = 10
# The outer corners of the block, and the ones between two rows of it. A
# grouped list is one shape with rounded ends, not a pile of rounded boxes.
CONTAINER_RADIUS = 20
INNER_RADIUS = 4
CONTAINER_GAP = 2

# how much of the text colour is mixed into the background for a container
SURFACE_STRENGTH = 0.07


def _s(key, fallback):
    return i18n.get(key, fallback)


def offer(activity, selection, on_install, on_decline=None, installed=()):
    """Shows the question. Returns True if it got on screen.

    `on_install(selection)` is called with what was ticked when it is; nothing
    is called when it is not, beyond `on_decline`. `installed` is what the
    container already has, by package name — those are shown as done rather
    than offered, and cannot be ticked or unticked.
    """
    try:
        return _build(activity, selection, on_install, on_decline,
                      frozenset(installed))
    except Exception as e:
        log.error("tools: cannot ask about the tools", e)
        return False


def _build(activity, selection, on_install, on_decline, installed):
    from android.widget import LinearLayout, ScrollView
    from org.telegram.messenger import AndroidUtilities
    from ui.alert import AlertDialogBuilder

    dp = AndroidUtilities.dp
    colours = _colours()

    body = LinearLayout(activity)
    body.setOrientation(LinearLayout.VERTICAL)

    install = _Install(selection)
    rows = []

    def changed():
        # one row can decide another's fate — ticking Python is what makes the
        # Python tools possible — so they are all asked to look at themselves
        for row in rows:
            row.restate()
        install.refresh()

    last = len(packages.GROUPS) - 1
    for position, group in enumerate(packages.GROUPS):
        row = _GroupRow(activity, group, selection, colours, changed,
                        position=position, count=len(packages.GROUPS),
                        installed=installed)
        rows.append(row)
        params = LinearLayout.LayoutParams(-1, -2)
        if position != last:
            params.bottomMargin = dp(CONTAINER_GAP)
        body.addView(row.view, params)

    scroll = ScrollView(activity)
    scroll.addView(body, LinearLayout.LayoutParams(-1, -2))

    holder = LinearLayout(activity)
    holder.setOrientation(LinearLayout.VERTICAL)
    holder.setPadding(dp(8), 0, dp(8), 0)
    holder.addView(scroll, LinearLayout.LayoutParams(-1, dp(LIST_MAX)))
    _fit(scroll, body, dp(LIST_MAX))

    builder = AlertDialogBuilder(activity)
    builder.set_title(_s("tools_ask_title", "Install useful tools?"))
    builder.set_message(_s(
        "tools_ask_message",
        "Alpine comes with almost nothing. These are fetched from its own "
        "repositories and live in the container."))
    builder.set_view(holder)

    def accept(dialog, which):
        dialog.dismiss()
        if not selection.packages():
            # the button is hidden in this state; if it was pressed anyway,
            # installing nothing is what it asked for
            return
        try:
            on_install(selection)
        except Exception as e:
            log.error("tools: cannot start the install", e)

    def decline(dialog, which):
        dialog.dismiss()
        if on_decline is not None:
            try:
                on_decline()
            except Exception:
                pass

    builder.set_positive_button(_title(selection), accept)
    # short on purpose: the client puts the two buttons side by side and drops
    # them onto separate lines when they do not fit, and the install button has
    # a count and a size to carry
    builder.set_negative_button(_s("tools_ask_later", "No"), decline)
    builder.show()
    # only now: the buttons are views the dialog builds when it is created
    install.attach(builder)
    return True


def _title(selection):
    """What the install button says.

    The count and the size are on the button rather than in a line above it:
    the button is the one thing somebody reads before pressing, and it is the
    only place where "how much is this going to cost me" is actually being
    asked. The size is the space it takes in the container, which is the same
    number each group's own row shows, so the two agree.
    """
    names = selection.packages()
    if not names:
        return _s("tools_ask_install", "Install")
    _download, installed = selection.cost()
    text = i18n.plural("tools_ask_install", len(names),
                       "Install (%d packages, %d MB)")
    try:
        return text % (len(names), installed)
    except Exception:
        return _s("tools_ask_install", "Install")


class _Install(object):
    """The button, kept in step with what is ticked.

    With nothing ticked there is nothing for it to do, so it leaves rather than
    standing there doing nothing — and comes back the moment something is
    ticked again. It fades and shrinks on the way out and is gone by the end,
    so the row it was in gets the space back; the button that stays walks into
    the middle of that space rather than leaving a hole where this one was.
    """

    FADE = 160
    SMALL = 0.9

    def __init__(self, selection):
        self.selection = selection
        self.view = None
        self.label = None
        self.stays = None
        self.shown = True

    def attach(self, builder):
        try:
            from ui.alert import AlertDialogBuilder

            self.view = builder.get_button(AlertDialogBuilder.BUTTON_POSITIVE)
            self.label = _text_view(self.view)
            self.stays = _Centre(
                builder.get_button(AlertDialogBuilder.BUTTON_NEGATIVE))
        except Exception as e:
            log.error("tools: cannot reach the install button", e)
            self.view = self.label = None
        self.refresh(animated=False)

    def refresh(self, animated=True):
        if self.view is None:
            return
        wanted = bool(self.selection.packages())
        if wanted:
            # only when there is something to say: relabelling it on the way
            # out would be a flicker of text nobody asked to read
            self._say(_title(self.selection))
        self._reveal(wanted, animated)

    def _say(self, text):
        try:
            if self.label is not None:
                self.label.setText(text)
        except Exception:
            pass

    def _reveal(self, on, animated):
        if on == self.shown:
            return
        self.shown = on
        view = self.view
        if self.stays is not None:
            self.stays.aim(alone=not on, animated=animated)
        try:
            if not animated:
                view.setAlpha(1.0 if on else 0.0)
                view.setScaleX(1.0 if on else self.SMALL)
                view.setScaleY(1.0 if on else self.SMALL)
                view.setVisibility(0 if on else 8)  # VISIBLE / GONE
                return
            view.animate().cancel()
            if on:
                view.setVisibility(0)
            animation = view.animate()
            animation.alpha(1.0 if on else 0.0)
            animation.scaleX(1.0 if on else self.SMALL)
            animation.scaleY(1.0 if on else self.SMALL)
            animation.setDuration(self.FADE)
            if not on:
                animation.withEndAction(_runnable(self._gone))
            animation.start()
        except Exception as e:
            log.error("tools: the install button will not move", e)

    def _gone(self):
        # ticked something again while it was fading out: leave it alone
        if not self.shown and self.view is not None:
            self.view.setVisibility(8)


class _Centre(object):
    """The button that stays, walking into the middle and back.

    On the same spring as a group opening, and aimed rather than started, so
    ticking something twice in a second turns it round from where it is instead
    of snapping. It moves rather than being laid out somewhere else: the row is
    the client's, and translating a view is the one way to shift it that cannot
    upset how the client measures its own dialog.
    """

    STIFFNESS = 900.0
    DAMPING = 0.82

    def __init__(self, view):
        self.view = view
        self.spring = None
        self.tried = False
        self.alone = False
        self.placed = False
        self._watch()

    def aim(self, alone, animated=True):
        self.alone = bool(alone)
        self._go(animated)

    def _go(self, animated):
        if self.view is None:
            return
        if self.alone and not self._laid_out():
            # nothing to aim at yet; the layout listener will call back when
            # there is
            return
        target = self._middle() if self.alone else 0.0
        # the first placement is where it belongs, not a journey to it: a
        # dialog that opens with nothing ticked should already be settled
        spring = self._spring() if animated and self.placed else None
        self.placed = True
        try:
            if spring is None:
                if self.spring is not None:
                    # a spring that is still moving would drag it back
                    self.spring.cancel()
                self.view.setTranslationX(target)
            else:
                spring.animateToFinalPosition(target)
        except Exception as e:
            log.error("tools: the button will not move over", e)

    def _watch(self):
        """Re-aims whenever the row is laid out again.

        The row can change shape under us — the buttons stack when they do not
        fit side by side and unstack when one of them goes — and a middle
        worked out before that is the wrong middle afterwards.
        """
        if self.view is None:
            return
        try:
            from android.view import View
            from java import dynamic_proxy

            outer = self

            class _Moved(dynamic_proxy(View.OnLayoutChangeListener)):
                def onLayoutChange(self, view, left, top, right, bottom,
                                   old_left, old_top, old_right, old_bottom):
                    if right - left != old_right - old_left or \
                            left != old_left:
                        outer._go(True)

            self.view.addOnLayoutChangeListener(_Moved())
        except Exception as e:
            log.error("tools: cannot watch the button's row", e)

    def _laid_out(self):
        try:
            return bool(self.view.getWidth())
        except Exception:
            return False

    def _middle(self):
        from java import cast
        from android.view import View

        try:
            row = cast(View, self.view.getParent())
            width = row.getWidth()
            mine = self.view.getWidth()
            if not width or not mine:
                return 0.0
            return float((width - mine) / 2.0 - self.view.getLeft())
        except Exception as e:
            log.error("tools: cannot find the middle of the row", e)
            return 0.0

    def _spring(self):
        if self.spring is not None or self.tried:
            return self.spring
        self.tried = True
        try:
            from androidx.dynamicanimation.animation import (
                DynamicAnimation, SpringAnimation, SpringForce)

            self.spring = SpringAnimation(self.view,
                                          DynamicAnimation.TRANSLATION_X)
            self.spring.setSpring(_force(SpringForce, self.STIFFNESS,
                                         self.DAMPING))
        except Exception as e:
            log.error("tools: no spring for the button", e)
            self.spring = None
        return self.spring


def _text_view(view):
    """The TextView inside a button, however deeply it is wrapped."""
    from android.widget import TextView

    if view is None:
        return None
    if isinstance(view, TextView):
        return view
    try:
        from android.view import ViewGroup

        if isinstance(view, ViewGroup):
            for index in range(view.getChildCount()):
                found = _text_view(view.getChildAt(index))
                if found is not None:
                    return found
    except Exception:
        pass
    return None


def _fit(scroll, body, maximum):
    """Makes the list as tall as its contents, and no taller.

    A fixed height leaves a strip of nothing under the last container when the
    groups are all closed, and a wrapping one would let an open group push the
    buttons off the screen. So the height is measured every frame: it follows
    the contents while a group opens, and stops at `maximum`.
    """
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
                        # the very first pass: skip the frame rather than show
                        # one at the wrong height
                        self.first = False
                        return False
            except Exception:
                pass
            return True

    try:
        scroll.getViewTreeObserver().addOnPreDrawListener(_Fit())
    except Exception as e:
        log.error("tools: the list cannot size itself", e)


def _runnable(function):
    from java import dynamic_proxy
    from java.lang import Runnable

    class _Run(dynamic_proxy(Runnable)):
        def run(self):
            try:
                function()
            except Exception:
                pass

    return _Run()


class _GroupRow(object):
    """One group: a line that toggles it, and a list that opens under it."""

    DIM = 0.4
    FADE = 180

    def __init__(self, activity, group, selection, colours, changed,
                 position=0, count=1, installed=()):
        from android.widget import LinearLayout

        self.activity = activity
        self.group = group
        self.selection = selection
        self.colours = colours
        self.changed = changed
        self.installed = frozenset(installed)
        # a group the container already has in full is not something to offer;
        # it is something to say is done
        self.done = all(name in self.installed for name in group.names)
        self.enabled = None
        self.open = False
        self.springs = None

        self.view = LinearLayout(activity)
        self.view.setOrientation(LinearLayout.VERTICAL)
        # a Material container: the group is one surface, and opening it grows
        # that surface rather than pushing rows apart
        self.view.setBackground(_container(colours, position, count))

        self.head, self.check, self.arrow = self._head()
        self.view.addView(self.head, LinearLayout.LayoutParams(-1, -2))

        self.items = LinearLayout(activity)
        self.items.setOrientation(LinearLayout.VERTICAL)
        self.items.setVisibility(8)  # View.GONE
        self.boxes = {}
        for package in group.packages:
            row, box = self._item(package)
            self.items.addView(row, LinearLayout.LayoutParams(-1, -2))
            self.boxes[package.name] = box
        self.view.addView(self.items, LinearLayout.LayoutParams(-1, -2))
        self.restate(animated=False)

    # --------------------------------------------------------------- state

    def usable(self):
        """Can this group be ticked at all right now?"""
        return not self.done and self.selection.is_possible(self.group.name)

    def restate(self, animated=True):
        """Looks at itself again: what it says, and whether it can be used.

        Called on every change anywhere in the sheet, because a group's fate
        can be decided by another one — ticking Python is what makes the Python
        tools installable, and unticking it takes them away again.
        """
        usable = self.usable()
        self.detail.setText(self._detail())
        self.check_widget.set(self.done or
                              self.selection.is_on(self.group.name))
        for name, box in self.boxes.items():
            box.set(name in self.installed or
                    self.selection.has(self.group.name, name))
        if usable == self.enabled:
            return
        self.enabled = usable
        target = 1.0 if usable else self.DIM
        try:
            if not animated:
                self.view.setAlpha(target)
                return
            self.view.animate().cancel()
            self.view.animate().alpha(target).setDuration(self.FADE).start()
        except Exception:
            pass

    def _detail(self):
        """The line under the title: what the group is, or why it is not."""
        if self.done:
            return _s("tools_already_here", "Already installed")
        if not self.selection.is_possible(self.group.name):
            needs = self.selection.needs_of(self.group.name)
            if needs == ("python",):
                return _s("tools_needs_python", "Needs Python")
            return _s("tools_needs", "Needs %s") % ", ".join(needs)
        return _s("tools_%s_desc" % self.group.name, self.group.summary)

    # ------------------------------------------------------------- building

    def _head(self):
        from android.util import TypedValue
        from android.view import Gravity
        from android.widget import LinearLayout, TextView
        from org.telegram.messenger import AndroidUtilities

        dp = AndroidUtilities.dp
        row = LinearLayout(self.activity)
        row.setOrientation(LinearLayout.HORIZONTAL)
        row.setGravity(Gravity.CENTER_VERTICAL)
        row.setPadding(dp(12), dp(6), dp(12), dp(6))
        row.setMinimumHeight(dp(ROW_HEIGHT))

        check = _Check(self.activity, self.colours,
                       self.selection.is_on(self.group.name))
        row.addView(check.view, _size(dp(CHECK_SIZE + 6)))

        text = LinearLayout(self.activity)
        text.setOrientation(LinearLayout.VERTICAL)
        title = TextView(self.activity)
        title.setText("%s · %d MB" % (
            _s("tools_%s_label" % self.group.name, self.group.title),
            self.group.installed))
        title.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 15)
        title.setTextColor(self.colours["fg"])
        detail = TextView(self.activity)
        detail.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
        detail.setTextColor(self.colours["dim"])
        self.detail = detail
        text.addView(title, LinearLayout.LayoutParams(-1, -2))
        text.addView(detail, LinearLayout.LayoutParams(-1, -2))
        params = LinearLayout.LayoutParams(0, -2)
        params.weight = 1.0
        params.leftMargin = params.rightMargin = dp(12)
        row.addView(text, params)

        arrow = _arrow(self.activity, self.colours["dim"])
        row.addView(arrow, _size(dp(24)))

        _on_click(check.view, self._toggle_group)
        _on_click(row, self._toggle_open)
        self.check_widget = check
        return row, check, arrow

    def _item(self, package):
        from android.util import TypedValue
        from android.view import Gravity
        from android.widget import LinearLayout, TextView
        from org.telegram.messenger import AndroidUtilities

        dp = AndroidUtilities.dp
        row = LinearLayout(self.activity)
        row.setOrientation(LinearLayout.HORIZONTAL)
        row.setGravity(Gravity.CENTER_VERTICAL)
        row.setPadding(dp(12) + dp(INDENT), dp(4), dp(12), dp(4))

        box = _Check(self.activity, self.colours,
                     self.selection.has(self.group.name, package.name))
        row.addView(box.view, _size(dp(CHECK_SIZE + 6)))

        text = TextView(self.activity)
        text.setText("%s — %s" % (package.name, package.summary))
        text.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 13)
        text.setTextColor(self.colours["fg"])
        params = LinearLayout.LayoutParams(0, -2)
        params.weight = 1.0
        params.leftMargin = dp(12)
        row.addView(text, params)

        size = TextView(self.activity)
        size.setText(_s("tools_here", "✓") if package.name in self.installed
                     else "%.1f MB" % package.size)
        size.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
        size.setTextColor(self.colours["dim"])
        row.addView(size, LinearLayout.LayoutParams(-2, -2))

        _on_click(row, lambda: self._toggle_package(package.name))
        return row, box

    # -------------------------------------------------------------- clicking

    def _toggle_group(self):
        if not self.usable():
            return
        on = not self.selection.is_on(self.group.name)
        self.selection.set_group(self.group.name, on)
        self._refresh()
        if on and not self.open:
            self._toggle_open()

    def _toggle_package(self, name):
        if not self.usable() or name in self.installed:
            # what is already there is not a choice, and neither is anything in
            # a group that cannot be installed yet
            return
        on = not self.selection.has(self.group.name, name)
        self.selection.set_package(self.group.name, name, on)
        self._refresh()

    def _toggle_open(self):
        self.open = not self.open
        self._animate()

    def _animate(self):
        """Opens or closes, on springs.

        Springs rather than a duration, because a spring can be re-aimed while
        it is moving: tapping twice quickly turns the animation round from
        wherever it is, at the speed it already had, instead of cutting to the
        start of a new one.

        The stagger is physics rather than timers. Every row is pulled to the
        same place by its own spring, and each spring down the list is a little
        softer than the one above it — so they arrive in order, and when the
        group is closed halfway through opening they leave in order too,
        without anything to cancel.
        """
        if self.springs is None:
            self.springs = _Springs(self)
        self.springs.aim(self.open)

    def _refresh(self):
        # `changed` comes back round to restate() here and everywhere else
        self.changed()


class _Check(object):
    """A tick box.

    The client's own, built the way the client builds one: `CheckBox2` at
    21dp with the theme's radio and checkbox keys, which is what the sheet
    that installed this plugin puts next to "enable after installation". It is
    round, it animates, and it is the same shape as every other tick in the
    app — which two drawn glyphs were never going to be.

    Where that class is not there, an ordinary Android checkbox; where even
    that fails, the glyphs, because a dialog with no ticks at all is worse
    than an ugly one.
    """

    def __init__(self, activity, colours, ticked):
        self.colours = colours
        self.kind = "client"
        self.view = self._client(activity) or self._plain(activity)
        if self.view is None:
            self.kind = "text"
            self.view = self._glyph(activity)
        self.set(ticked, animated=False)

    def _client(self, activity):
        """The client's checkbox, set up exactly as the client sets it up.

        The four calls below are the four the plugin-install sheet makes, in
        its order. The last one is the one that was missing, and it is the
        whole reason three different colour keys all came out green:
        `setDrawBackgroundAsArc` chooses how the thing is drawn, and until it
        is called the checkbox draws itself the way a tick in a message does —
        green, and paying no attention to the colours it was given.
        """
        try:
            from org.telegram.ui.ActionBar import Theme
            from org.telegram.ui.Components import CheckBox2

            view = CheckBox2(activity, CHECK_SIZE, None)
            view.setColor(Theme.key_radioBackgroundChecked,
                          Theme.key_checkboxDisabled, Theme.key_checkboxCheck)
            view.setDrawUnchecked(True)
            view.setDrawBackgroundAsArc(ARC_STYLE)
            return view
        except Exception as e:
            log.error("tools: no client checkbox", e)
            return None

    def _plain(self, activity):
        try:
            from android.widget import CheckBox

            self.kind = "android"
            view = CheckBox(activity)
            view.setFocusable(False)
            try:
                # not the theme's accent, which is whatever the activity says;
                # the same colour the rest of this dialog is using
                from android.content.res import ColorStateList

                view.setButtonTintList(
                    ColorStateList.valueOf(int(self.colours["accent"])))
            except Exception:
                pass
            return view
        except Exception:
            self.kind = "text"
            return None

    def _glyph(self, activity):
        from android.util import TypedValue
        from android.view import Gravity
        from android.widget import TextView

        view = TextView(activity)
        view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 15)
        view.setGravity(Gravity.CENTER)
        return view

    def set(self, ticked, animated=True):
        try:
            if self.kind == "client":
                self.view.setChecked(bool(ticked), bool(animated))
            elif self.kind == "android":
                self.view.setChecked(bool(ticked))
            else:
                self.view.setText("☑" if ticked else "☐")
                self.view.setTextColor(
                    self.colours["accent" if ticked else "dim"])
        except Exception:
            pass


class _Springs(object):
    """The physics of one group opening.

    Three things move: the height of the list, the tilt of the arrow, and each
    row's own fade and slide. Every one of them is a spring that is *aimed*
    rather than started — `animateToFinalPosition` keeps whatever speed the
    spring already had, which is what makes a tap during the animation turn it
    round instead of jerking.

    The rows are staggered by stiffness: the first is stiff and snaps into
    place, each one below it is softer and therefore later. No delays, nothing
    queued, nothing to cancel when the direction changes.
    """

    # how hard the list itself is pulled, and how much it overshoots
    HEIGHT_STIFFNESS = 900.0
    HEIGHT_DAMPING = 0.82
    # the first row, and how much softer each one below it is
    ROW_STIFFNESS = 1200.0
    ROW_FALLOFF = 0.82
    ROW_DAMPING = 0.7
    # how far a row falls in from, in dp
    ROW_RISE = 10

    def __init__(self, row):
        self.row = row
        self.ok = False
        try:
            self._build()
            self.ok = True
        except Exception as e:
            log.error("tools: no springs; opening without them", e)

    def _build(self):
        from androidx.dynamicanimation.animation import (
            DynamicAnimation, FloatValueHolder, SpringAnimation, SpringForce)
        from org.telegram.messenger import AndroidUtilities

        self.dp = AndroidUtilities.dp
        self.height = self._measure()
        self.holder = FloatValueHolder(0.0)
        self.opening = SpringAnimation(self.holder)
        self.opening.setSpring(_force(SpringForce, self.HEIGHT_STIFFNESS,
                                      self.HEIGHT_DAMPING))
        self.opening.addUpdateListener(_update(self._draw_height))

        self.turn = SpringAnimation(self.row.arrow, DynamicAnimation.ROTATION)
        self.turn.setSpring(_force(SpringForce, self.HEIGHT_STIFFNESS, 0.7))

        self.rows = []
        stiffness = self.ROW_STIFFNESS
        for index in range(self.row.items.getChildCount()):
            child = self.row.items.getChildAt(index)
            child.setAlpha(0.0)
            child.setTranslationY(float(-self.dp(self.ROW_RISE)))
            fade = SpringAnimation(child, DynamicAnimation.ALPHA)
            fade.setSpring(_force(SpringForce, stiffness, 1.0))
            slide = SpringAnimation(child, DynamicAnimation.TRANSLATION_Y)
            slide.setSpring(_force(SpringForce, stiffness, self.ROW_DAMPING))
            self.rows.append((fade, slide))
            stiffness *= self.ROW_FALLOFF

    def _measure(self):
        """How tall the list wants to be, asked before it is ever shown."""
        from android.view import View

        items = self.row.items
        items.setVisibility(0)
        width = self.row.view.getWidth()
        spec = (View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY)
                if width else
                View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED))
        items.measure(spec,
                      View.MeasureSpec.makeMeasureSpec(
                          0, View.MeasureSpec.UNSPECIFIED))
        height = items.getMeasuredHeight()
        self._draw_height(0.0)
        return float(height or self.dp(48))

    def _draw_height(self, value):
        items = self.row.items
        params = items.getLayoutParams()
        params.height = max(int(value), 0)
        items.setLayoutParams(params)
        items.setVisibility(8 if value <= 0.5 else 0)

    def aim(self, opening):
        """Points every spring at where it should end up."""
        if not self.ok:
            self._plain(opening)
            return
        try:
            self.opening.animateToFinalPosition(
                self.height if opening else 0.0)
            self.turn.animateToFinalPosition(180.0 if opening else 0.0)
            rise = float(-self.dp(self.ROW_RISE))
            for fade, slide in self.rows:
                fade.animateToFinalPosition(1.0 if opening else 0.0)
                slide.animateToFinalPosition(0.0 if opening else rise)
        except Exception as e:
            log.error("tools: the springs would not move", e)
            self.ok = False
            self._plain(opening)

    def _plain(self, opening):
        try:
            self.row.items.setVisibility(0 if opening else 8)
            params = self.row.items.getLayoutParams()
            params.height = -2 if opening else 0
            self.row.items.setLayoutParams(params)
            self.row.arrow.setRotation(180.0 if opening else 0.0)
            for index in range(self.row.items.getChildCount()):
                child = self.row.items.getChildAt(index)
                child.setAlpha(1.0 if opening else 0.0)
                child.setTranslationY(0.0)
        except Exception:
            pass


def _force(spring_force, stiffness, damping):
    force = spring_force()
    force.setStiffness(float(stiffness))
    force.setDampingRatio(float(damping))
    return force


def _update(function):
    """An OnAnimationUpdateListener that calls a Python function."""
    from androidx.dynamicanimation.animation import DynamicAnimation
    from java import dynamic_proxy

    class _Listener(dynamic_proxy(DynamicAnimation.OnAnimationUpdateListener)):
        def onAnimationUpdate(self, animation, value, velocity):
            try:
                function(value)
            except Exception:
                pass

    return _Listener()


def _arrow(activity, colour):
    """The chevron, from the client's own drawables where it has one."""
    from android.util import TypedValue
    from android.view import Gravity
    from android.widget import ImageView, TextView

    for name in ("arrow_more", "ic_arrow_drop_down", "msg_arrowright"):
        try:
            resources = activity.getResources()
            found = resources.getIdentifier(name, "drawable",
                                            activity.getPackageName())
            if not found:
                continue
            view = ImageView(activity)
            view.setImageResource(found)
            view.setColorFilter(int(colour))
            view.setScaleType(ImageView.ScaleType.CENTER_INSIDE)
            return view
        except Exception:
            continue
    view = TextView(activity)
    view.setText("⌄")
    view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 16)
    view.setTextColor(int(colour))
    view.setGravity(Gravity.CENTER)
    return view


def _container(colours, position, count):
    """The surface a group sits on.

    A grouped list, the way the client draws one: the first has its top two
    corners rounded, the last its bottom two, and everything between them is
    square, so the rows read as one block rather than as a stack of cards.
    `Theme.createRoundRectDrawable` takes the four corners separately and is
    what the client uses for exactly this.
    """
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    top = dp(CONTAINER_RADIUS) if position == 0 else dp(INNER_RADIUS)
    bottom = dp(CONTAINER_RADIUS) if position == count - 1 else dp(INNER_RADIUS)
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


def _size(pixels):
    from android.widget import LinearLayout

    return LinearLayout.LayoutParams(pixels, pixels)


def _on_click(view, function):
    from android_utils import OnClickListener

    try:
        view.setOnClickListener(OnClickListener(lambda *args: function()))
    except Exception as e:
        log.error("tools: cannot wire a row", e)


def _colours():
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
        # opaque, so nothing drawn over it shows through twice
        "surface": theme.mix(fg, background, SURFACE_STRENGTH),
    }
