# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""A bulletin that shows the first setup happening.

The client's own BulletinHelper offers a line of text and nothing else, and
what this has to say — that something is running, and how far through it is —
is not a line of text. But the card it says it on is the client's: this builds
a real Bulletin and puts its own two views inside it.

That is where the paddings come from. `Bulletin.Layout`, which every one of
them extends, sets a minimum height of 48dp, padding of 16/8/16/8, the
undo-background colour and the press animation; `Bulletin` handles where it
sits against the insets and how it comes and goes. Copying those numbers into
a card of our own would have meant keeping them true across client versions,
and they are not ours to keep.

That Layout is abstract, and so is the ButtonLayout under it, so the card is
one of the client's finished ones with its own picture and line of text hidden
— it is here for the frame, not for what it was built to hold.

The colours are the same keys the "Plugin installed" bulletin uses:
key_undo_background behind it, key_undo_infoColor for the line of text, and
what that bulletin gives its button, key_undo_cancelColor, for the bar.

A card of our own is still here as a fallback, for a client whose Bulletin is
not the one this was written against.

Everything Android is imported inside the functions, so the parts worth testing
— when to redraw, in what order to show — can be exercised on a desktop.

The bar is Material's own LinearProgressIndicator, which the client carries a
recent copy of — recent enough for the gap, the rounded ends and the dot at the
far end that make it Material 3 rather than a rectangle we drew. It wants a
Material theme on its context and this client's activity has none, so it is
handed one. Where that fails the bar is two views and a scale: a rounded fill
scaled from its left edge, which needs no measurement to have happened and
animates by itself.

Either way it runs along the bottom of the card from corner to corner, clipped
to the card's own shape — nothing straight fits a rounded corner, so the bar
is given the corner instead. What clips it is transparent: the card's real
colour is the client's business, and a guess at it showed up as a grey block
over half the bulletin.
"""

import time

from ..compat import i18n
from ..utils import log

# how close together two redraws may be, in seconds, and how small a change is
# worth one. The unpacking calls back every twenty-five files and the scan on
# every refused number — hundreds of times either way, which is far more often
# than a bar four pixels tall can show.
MIN_INTERVAL = 0.08
MIN_CHANGE = 0.01


def worth_drawing(fraction, label, last_fraction, last_label, now, last_time):
    """Should this update reach the screen?

    Pure, so the throttle is settled here rather than argued about in a device
    log. The end and a change of wording always go through: the last frame is
    the one that says it is finished, and a label that arrives late describes
    the wrong step.
    """
    if last_time is None:
        return True
    if label != last_label:
        return True
    if fraction >= 1.0 and last_fraction < 1.0:
        return True
    if now - last_time < MIN_INTERVAL:
        return False
    return abs(fraction - last_fraction) >= MIN_CHANGE


# the dots after the title, in the order they are shown. A wave that fades each
# dot in turn — the way iOS does it while a voice message is being
# transcribed — is the nicer version of this and wants a spannable per frame;
# this is the same idea at a tenth of the cost.
DOTS = ("", ".", "..", "...")
DOT_INTERVAL = 380  # milliseconds

# How long the client's own bulletin — "Plugin installed" — is left up before
# ours takes its place, and how long its dismissal takes. Both in
# milliseconds. The first is a decision: long enough to read four words, short
# enough that the setup is still worth announcing.
OTHER_BULLETIN_GRACE = 1000
HIDE_ANIMATION = 220

# The duration handed to Bulletin.make. Bulletins are made to go away by
# themselves and this one must not until the work is done, so it is told a
# number it will never reach and `setCanHide(False)` on top of that.
LONG_ENOUGH = 10 * 60 * 1000

# how much of the text colour is in the track behind the bar
TRACK_STRENGTH = 0.18

# thickness of the bar, in dp. Its corners are half of it, which is what makes
# the ends round rather than merely soft.
BAR_HEIGHT = 6

# How far inside the card its contents sit, and how tall the card is. Both are
# the client's: its own children carry a 16dp inset, and the bulletin the
# plugin was installed with is as tall as the 48dp animation in it — which is
# why ours was coming out shorter than the one it follows.
INSIDE_CARD = 16
CARD_HEIGHT = 48

# how tall the clipped strip at the bottom is: the bar, plus enough above it
# that a corner radius of CARD_RADIUS is not clamped to half the height
STRIP_HEIGHT = 2 * 16 + 6

# the corner radius Layout.setBackground uses, for the fallback card to match
CARD_RADIUS = 16


class SetupBulletin(object):
    """The card itself. One per setup; `close` takes it away."""

    def __init__(self, activity=None):
        self.activity = activity
        # the client's bulletin, when it is one; the card either way
        self.bulletin = None
        self.card = None
        self.container = None
        self.title_view = None
        self.fill = None
        self.title = i18n.get("setup_running", "Setting up extCLI")
        self._dots = 0
        self._closed = False
        self._last_fraction = 0.0
        self._last_label = None
        self._last_time = None
        # the newest value, drawn or not: the card is built on another thread
        # and has to catch up with wherever the setup has got to
        self._wanted = 0.0

    # ------------------------------------------------------------- building

    def show(self):
        """Puts the card on screen. Says whether there was a screen to put it on.

        The building itself happens on the UI thread — this is called from the
        one the setup runs on, and views may not be made or attached from
        there. So the answer is about whether there is an activity at all, and
        the card appears a moment later.
        """
        activity = self.activity or _activity()
        if activity is None:
            return False
        self.activity = activity
        self._on_ui(self._after_the_other_one)
        return True

    def _after_the_other_one(self):
        """Waits for the client's own bulletin to have been read, and takes it
        away before ours arrives.

        The two happen within a moment of each other: the plugin is installed,
        the client says so, and the setup this belongs to starts on the same
        breath. Two cards in the same place is one card nobody can read — so
        the client's is left up long enough to be read, then dismissed the way
        it would dismiss itself, and ours comes after it has gone.
        """
        other = _visible_bulletin()
        if other is None:
            self._build_safely()
            return

        def dismiss():
            try:
                # its own animation, the one it plays when its time is up
                other.hide()
            except Exception as e:
                log.error("progress: cannot dismiss the client's bulletin", e)
            self._later(self._build_safely, HIDE_ANIMATION)

        self._later(dismiss, OTHER_BULLETIN_GRACE)

    def _build_safely(self):
        try:
            self._build()
        except Exception as e:
            log.error("progress: cannot show the bulletin", e)
            self.card = None

    def _build(self):
        """The client's own bulletin, with our content in it.

        Everything that makes a bulletin look like one happens in
        `Bulletin.Layout`'s constructor — the undo-background, a minimum height
        of 48dp, padding of 16/8/16/8, the press animation — and every one of
        the client's layouts inherits it. Putting our two views inside one of
        them means the card is the client's, the same corner radius in the same
        place with the same way in and out, and only what is written on it is
        ours.
        """
        if self._closed:
            # it was over before the card was built; putting it on screen now
            # would leave it there with nobody left to take it away
            return
        activity = self.activity or _activity()
        if activity is None:
            return
        if self._build_bulletin(activity):
            return
        # no client bulletin to be had: a card of our own, in the same place
        self._build_card(activity)

    def _build_bulletin(self, activity):
        """Says whether it managed it. A client whose Bulletin is not the one
        this was written against gets the card below instead, so every failure
        here is an answer rather than an exception."""
        try:
            from org.telegram.ui.Components import Bulletin

            fragment = _fragment()
            layout = _client_layout(activity)
            if fragment is None or layout is None:
                return False
            content, text, fill = self._content(activity)
            _min_height(content, CARD_HEIGHT)
            layout.addView(content, _fill_params())
            bulletin = Bulletin.make(fragment, layout, LONG_ENOUGH)
            try:
                # it must not walk off on its own timer while work is running
                bulletin.setCanHide(False)
            except Exception:
                pass
            bulletin.show()
        except Exception as e:
            log.error("progress: cannot use the client's bulletin", e)
            return False
        self.bulletin = bulletin
        self.card = layout
        self.title_view = text
        self.fill = fill
        self._draw(self._wanted)
        self._tick_dots()
        return True

    def _content(self, activity):
        """The title and the bar, as the client would have written them.

        15sp, the regular face and `key_undo_infoColor` are what the bulletin
        the plugin was installed with uses for its own line of text; the bar
        takes the colour that bulletin gives its button.

        The bar runs along the bottom edge of the card, corner to corner, and
        the title is centred in what is left above it. The inset the client's
        own children carry is on the title alone — the bar is meant to reach
        the edges.
        """
        from android.util import TypedValue
        from android.view import Gravity
        from android.widget import FrameLayout, TextView
        from org.telegram.messenger import AndroidUtilities

        dp = AndroidUtilities.dp
        colours = _colours()

        content = FrameLayout(activity)

        bar, fill = _bar(activity, colours)
        content.addView(_bottom_strip(activity, bar), _strip_params(dp))

        text = TextView(activity)
        text.setText(self.title)
        text.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 15)
        text.setTextColor(colours["text"])
        text.setSingleLine()
        try:
            text.setTypeface(AndroidUtilities.regular())
        except Exception:
            pass
        title_params = FrameLayout.LayoutParams(
            -2, -2, Gravity.START | Gravity.CENTER_VERTICAL)
        title_params.leftMargin = title_params.rightMargin = dp(INSIDE_CARD)
        # centred in the space above the bar, not in the whole card
        title_params.bottomMargin = dp(BAR_HEIGHT)
        content.addView(text, title_params)
        return content, text, fill

    def _build_card(self, activity):
        """A bulletin-shaped card of our own.

        For a client whose Bulletin is not the one this was written against —
        another fork, another version — where a card in roughly the right place
        beats no card at all.
        """
        from android.graphics.drawable import GradientDrawable
        from android.view import Gravity
        from android.widget import FrameLayout
        from org.telegram.messenger import AndroidUtilities

        container = _container(activity)
        if container is None:
            return
        dp = AndroidUtilities.dp
        colours = _colours()
        content, text, fill = self._content(activity)
        content.setMinimumHeight(dp(CARD_HEIGHT))
        content.setBackground(_rounded(GradientDrawable, dp(CARD_RADIUS),
                                       colours["card"]))
        content.setElevation(float(dp(4)))

        params = FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM)
        params.leftMargin = params.rightMargin = dp(8)
        params.bottomMargin = dp(8)
        container.addView(content, params)
        content.setTranslationY(float(dp(80)))
        content.animate().translationY(0.0).setDuration(220).start()

        self.container = container
        self.card = content
        self.title_view = text
        self.fill = fill
        self._draw(self._wanted)
        self._tick_dots()

    # -------------------------------------------------------------- updating

    def update(self, fraction, label=""):
        """Called from whatever thread the setup runs on."""
        if self._closed:
            return
        self._wanted = fraction
        now = time.time()
        if not worth_drawing(fraction, label, self._last_fraction,
                             self._last_label, now, self._last_time):
            return
        self._last_fraction = fraction
        self._last_label = label
        self._last_time = now
        self._on_ui(lambda: self._draw(fraction))

    def _draw(self, fraction):
        if self.fill is None:
            return
        value = 0.0 if fraction < 0 else (1.0 if fraction > 1 else fraction)
        setter = getattr(self.fill, "setProgressCompat", None)
        if setter is not None:
            # Material's own, which animates between the two values itself
            setter(int(value * 1000), True)
            return
        self.fill.animate().scaleX(float(value)).setDuration(180).start()

    def finish(self, text=None, ok=True, delay=1400):
        """The last frame, then away.

        It stays for a moment on purpose: a bar that vanishes the instant it
        fills leaves the person who was watching it unsure whether it got
        there.
        """
        if self._closed:
            return
        self._closed = True
        message = text or (i18n.get("setup_done", "extCLI is ready") if ok
                           else i18n.get("setup_failed", "extCLI setup failed"))

        def show_last():
            try:
                if self.title_view is not None:
                    self.title_view.setText(message)
                    if not ok:
                        self.title_view.setTextColor(_colours()["error"])
                self._draw(1.0)
            except Exception:
                pass
            self._later(self._remove, delay)

        self._on_ui(show_last)

    def close(self):
        """Away now, with nothing said. For a setup that never started."""
        self._closed = True
        self._on_ui(self._remove)

    def _remove(self):
        bulletin, self.bulletin = self.bulletin, None
        if bulletin is not None:
            self.card = None
            try:
                # let it go the way every other bulletin goes
                bulletin.setCanHide(True)
            except Exception:
                pass
            try:
                bulletin.hide()
            except Exception as e:
                log.error("progress: cannot hide our bulletin", e)
            return
        card, self.card = self.card, None
        if card is None:
            return
        try:
            card.animate().translationY(float(card.getHeight() + 40)).alpha(
                0.0).setDuration(200).withEndAction(
                    _runnable(lambda: _detach(self.container, card))).start()
        except Exception:
            _detach(self.container, card)

    # ---------------------------------------------------------------- dots

    def _tick_dots(self):
        """The three dots after the title, going round."""
        if self._closed or self.title_view is None:
            return
        self._dots = (self._dots + 1) % len(DOTS)
        try:
            self.title_view.setText(self.title + DOTS[self._dots])
        except Exception:
            return
        self._later(self._tick_dots, DOT_INTERVAL)

    # --------------------------------------------------------------- thread

    def _on_ui(self, function):
        try:
            from org.telegram.messenger import AndroidUtilities

            AndroidUtilities.runOnUIThread(_runnable(function))
        except Exception:
            try:
                function()
            except Exception as e:
                log.error("progress: update failed", e)

    def _later(self, function, milliseconds):
        try:
            from org.telegram.messenger import AndroidUtilities

            AndroidUtilities.runOnUIThread(_runnable(function),
                                           int(milliseconds))
        except Exception:
            pass


def _runnable(function):
    from java import dynamic_proxy
    from java.lang import Runnable

    class _Run(dynamic_proxy(Runnable)):
        def run(self):
            try:
                function()
            except Exception as e:
                log.error("progress: runnable failed", e)

    return _Run()


def _visible_bulletin():
    """The client's own bulletin, if one is on screen.

    Telegram keeps one at a time and hands it out statically, which is the
    whole reason this can be done politely rather than by drawing over it.
    """
    try:
        from org.telegram.ui.Components import Bulletin

        return Bulletin.getVisibleBulletin()
    except Exception:
        return None


# Bulletin.Layout is abstract, and so is ButtonLayout under it: the card has
# to be one of the client's finished ones. Any of these will do — all any of
# them is here for is the frame around our own two views — so they are tried in
# turn and the first that this client has wins.
LAYOUT_KINDS = ("SimpleLayout", "MultiLineLayout", "LottieLayout")


def _strip_params(dp):
    """The strip along the bottom of the card, edge to edge."""
    from android.view import Gravity
    from android.widget import FrameLayout

    return FrameLayout.LayoutParams(-1, dp(STRIP_HEIGHT), Gravity.BOTTOM)


def _bottom_strip(activity, bar):
    """The bar, cut to the corners of the card it sits at the bottom of.

    A bar that stops short of the corners looks like it is missing its ends,
    and one that does not stop crosses them — the card's edge is 16dp in at the
    very bottom and 3dp in six above it, so nothing straight fits both. What
    fits is the card's own shape: the strip is clipped to a rounded rectangle
    of the same radius, and the bar inside it ends exactly where the card does.

    The strip is taller than the bar so the radius is not clamped: a round rect
    cannot be rounder than half its height.

    Its background is transparent and exists only to be the outline — painting
    it the card's colour, on the theory that it would then not be seen, put a
    grey block over half the bulletin. What the card is actually painted with
    is the client's business and not a colour to guess at.
    """
    from android.graphics.drawable import GradientDrawable
    from android.view import Gravity, ViewOutlineProvider
    from android.widget import FrameLayout
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    strip = FrameLayout(activity)
    strip.addView(bar, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
    try:
        strip.setBackground(_rounded(GradientDrawable, dp(CARD_RADIUS), 0))
        strip.setOutlineProvider(ViewOutlineProvider.BACKGROUND)
        strip.setClipToOutline(True)
    except Exception as e:
        log.error("progress: cannot clip the bar to the card", e)
    return strip


def _bar(activity, colours):
    """(the view to add, the thing that shows progress).

    Material's own indicator when the client carries one — it does, and a
    recent one: gap, rounded ends and the dot at the far end are all there,
    which is what makes it look like the rest of Android rather than like a
    rectangle we drew. It needs a Material theme on its context, which this
    client's activity does not have, so it is given one; if that cannot be
    found the bar is two views and a scale, which needs nothing.
    """
    indicator = _material_bar(activity, colours)
    if indicator is not None:
        return indicator, indicator
    return _plain_bar(activity, colours)


def _material_bar(activity, colours):
    from org.telegram.messenger import AndroidUtilities

    context = _material_context(activity)
    if context is None:
        return None
    try:
        from com.google.android.material.progressindicator import (
            LinearProgressIndicator)

        dp = AndroidUtilities.dp
        bar = LinearProgressIndicator(context)
        bar.setIndeterminate(False)
        bar.setMax(1000)
        bar.setTrackThickness(dp(BAR_HEIGHT))
        bar.setTrackCornerRadius(dp(BAR_HEIGHT) // 2)
        bar.setIndicatorColor([colours["fill"]])
        bar.setTrackColor(colours["track"])
    except Exception as e:
        log.error("progress: no material indicator", e)
        return None
    # The gap is the whole point of the Material 3 bar and it stays. What went
    # with it: the dot at the end of the track, which was never visible, and
    # the rounded caps facing the gap — of the three things that draw at the
    # end of the fill, that is the one still to be ruled out as the grey
    # half-circle. Square ends against the gap are what Material itself drew
    # before it started rounding them.
    for name, value in (("setIndicatorTrackGapSize", AndroidUtilities.dp(4)),
                        ("setTrackStopIndicatorSize", 0),
                        ("setTrackInnerCornerRadius", 0)):
        setter = getattr(bar, name, None)
        if setter is None:
            continue
        try:
            setter(value)
        except Exception:
            pass
    return bar


# themes to look for, best first. The identifier is asked of the client's own
# resources rather than hardcoded: the number differs per build, and a style
# that was shrunk out of the app is simply not found.
MATERIAL_THEMES = (
    "Theme.Material3.DayNight.NoActionBar",
    "Theme.MaterialComponents.DayNight.NoActionBar",
    "Theme.MaterialComponents.DayNight",
    "Theme.MaterialComponents.NoActionBar",
)


def _material_context(activity):
    """The activity, wearing a theme Material's own views will accept."""
    try:
        from android.view import ContextThemeWrapper

        resources = activity.getResources()
        package = activity.getPackageName()
        for name in MATERIAL_THEMES:
            found = resources.getIdentifier(name, "style", package)
            if found:
                return ContextThemeWrapper(activity, found)
    except Exception:
        pass
    return None


def _plain_bar(activity, colours):
    """A track and a fill scaled across it. Nothing to go wrong."""
    from android.graphics.drawable import GradientDrawable
    from android.view import View
    from android.widget import FrameLayout
    from org.telegram.messenger import AndroidUtilities

    dp = AndroidUtilities.dp
    bar = FrameLayout(activity)
    track = View(activity)
    track.setBackground(_rounded(GradientDrawable, dp(BAR_HEIGHT) / 2.0,
                                 colours["track"]))
    bar.addView(track, FrameLayout.LayoutParams(-1, dp(BAR_HEIGHT)))
    fill = View(activity)
    fill.setBackground(_rounded(GradientDrawable, dp(BAR_HEIGHT) / 2.0,
                                colours["fill"]))
    fill.setPivotX(0.0)
    fill.setScaleX(0.0)
    bar.addView(fill, FrameLayout.LayoutParams(-1, dp(BAR_HEIGHT)))
    return bar, fill


def _min_height(view, height):
    from org.telegram.messenger import AndroidUtilities

    try:
        view.setMinimumHeight(AndroidUtilities.dp(height))
    except Exception:
        pass


def _client_layout(activity):
    """One of the client's own bulletin layouts, emptied of its content."""
    from android.view import View
    from org.telegram.ui.Components import Bulletin

    for name in LAYOUT_KINDS:
        kind = getattr(Bulletin, name, None)
        if kind is None:
            continue
        try:
            layout = kind(activity, None)
        except Exception:
            continue
        # what it builds for itself — a picture and a line of text — makes
        # room we want for our own; gone, they take none
        for field in ("imageView", "textView"):
            child = getattr(layout, field, None)
            if child is None:
                continue
            try:
                child.setVisibility(View.GONE)
            except Exception:
                pass
        return layout
    return None


def _fragment():
    try:
        from client_utils import get_last_fragment

        return get_last_fragment()
    except Exception:
        return None


def _fill_params():
    """Our content, filling the card exactly.

    The padding is not inside the card, it is around it: the background's
    bounds are (paddingLeft, paddingTop) to (width - paddingRight, height -
    paddingBottom), so the rounded card *is* the padded area. Filling it is
    what the bar wants — it is meant to reach the corners — and the inset the
    client's own children carry is on the title instead.
    """
    from android.view import Gravity
    from android.widget import FrameLayout

    return FrameLayout.LayoutParams(-1, -2, Gravity.CENTER_VERTICAL)


def _activity():
    try:
        from client_utils import get_last_fragment

        fragment = get_last_fragment()
        return fragment.getParentActivity() if fragment else None
    except Exception:
        return None


# android.R.id.content, in case the R class cannot be imported: the id of the
# frame every activity puts its content in, and a platform constant that has
# not changed since API 1
CONTENT_ID = 0x01020002


def _container(activity):
    """The frame everything on screen sits in.

    The activity's content view rather than the fragment's: a fragment that is
    replaced while the setup runs would take the card with it, and this
    outlives any one screen.
    """
    identifier = CONTENT_ID
    try:
        from android import R as android_R

        identifier = android_R.id.content
    except Exception:
        pass
    try:
        return activity.findViewById(identifier)
    except Exception:
        return None


def _detach(container, card):
    try:
        if container is not None and card is not None:
            container.removeView(card)
    except Exception:
        pass


def _rounded(gradient_drawable, radius, colour):
    drawable = gradient_drawable()
    drawable.setShape(gradient_drawable.RECTANGLE)
    drawable.setCornerRadius(float(radius))
    drawable.setColor(int(colour))
    return drawable


def _colours():
    """The client's own, so the card belongs to the app it is sitting in."""
    from ..compat import theme

    try:
        roles = theme.roles()
    except Exception:
        roles = {}

    def role(name, fallback):
        found = roles.get(name)
        return found if found is not None else theme.signed(fallback)

    # the bulletin's own keys, so the card reads as the client's: the same
    # background, the same text colour its one line uses, and for the bar the
    # colour it gives the button beside that line
    info = theme.get_color("key_undo_infoColor")
    card = theme.get_color("key_undo_background") or role("bg", 0xFF1B1B1B)
    return {
        "card": card,
        "text": info or role("fg", 0xFFFFFFFF),
        # Opaque, worked out rather than left to alpha. Material draws the
        # track and then a rounded cap at the end of it, one over the other,
        # and a translucent colour drawn twice is darker where they overlap —
        # which came out as a grey half-circle sitting after the blue.
        "track": theme.mix(info, card, TRACK_STRENGTH) if info
        else role("divider", 0xFF3A3A3A),
        "fill": theme.get_color("key_undo_cancelColor")
        or role("accent", 0xFF4EA1F3),
        "error": role("error", 0xFFE0574B),
    }
