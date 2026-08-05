# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Making a Dialog behave like a full-screen terminal window.

Shared by the console and the copy view, and separate from either, because
getting a dialog to actually cover the display took several attempts and the
knowledge should not live in one of the two places that needs it.

The awkward parts, in order of how much time each cost:

  * On targetSdk 36 `setStatusBarColor` and `setNavigationBarColor` throw
    outright, so every window call gets its own attempt and the two that throw
    go last.
  * `FLAG_LAYOUT_INSET_DECOR` asks the framework to shrink the window to fit
    around the system bars — the opposite of what a full-screen window wants —
    so it is cleared, not set.
  * A dialog applies its layout params when it is shown, so the size has to be
    set again afterwards or the window wraps its content.
  * Insets are applied by hand to the content root, whose background is the
    terminal's, so there is something opaque behind every bar.
"""

from ..render import palette as palette_module
from ..utils import log


def attach(dialog, root, palette):
    """Puts `root` in the dialog and makes the window full screen.

    Call before show(); call `expand` after.
    """
    _set_content(dialog, root, palette)
    _prepare(dialog, palette)


def expand(dialog):
    """Makes the window fill the display. Only takes effect once it is showing.

    In pixels, not MATCH_PARENT. MATCH_PARENT means the parent frame, and what
    that frame is depends on flags the platform reinterprets between versions:
    the window came out 2334px tall on a 2400px screen, short by exactly the
    navigation bar, and the strip behind the bar showed the screen underneath.
    Asking for the screen's own measurements leaves nothing to interpret.
    """
    from android.view import Gravity, ViewGroup

    window = dialog.getWindow()
    if window is None:
        return
    width, height = screen_size()
    if width is None:
        width = height = ViewGroup.LayoutParams.MATCH_PARENT
    try:
        attributes = window.getAttributes()
        attributes.width = width
        attributes.height = height
        attributes.x = 0
        attributes.y = 0
        attributes.gravity = Gravity.TOP | Gravity.START
        attributes.horizontalMargin = 0.0
        attributes.verticalMargin = 0.0
        try:
            # and over the camera cutout, which is inside the status bar area
            attributes.layoutInDisplayCutoutMode = \
                attributes.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
        except Exception:
            pass
        window.setAttributes(attributes)
    except Exception as e:
        log.log("window: cannot set attributes: %s" % e, debug=True)
    try:
        window.setLayout(width, height)
    except Exception as e:
        log.error("window: cannot fill the screen", e)


def screen_size():
    """The whole display, bars included, or (None, None).

    Not Telegram's displaySize: that is the usable area, which is why a window
    that stopped at the navigation bar measured as if it were correct.
    """
    try:
        from android.content import Context
        from client_utils import get_last_fragment

        activity = get_last_fragment().getParentActivity()
        manager = activity.getSystemService(Context.WINDOW_SERVICE)
        bounds = manager.getMaximumWindowMetrics().getBounds()
        return int(bounds.width()), int(bounds.height())
    except Exception as e:
        log.log("window: cannot measure the screen: %s" % e, debug=True)
        return None, None


# ------------------------------------------------------------------ internals

def _set_content(dialog, root, palette):
    from android.view import ViewGroup

    window = dialog.getWindow()
    try:
        window.setDecorFitsSystemWindows(False)
    except Exception as e:
        # API 30+; below that the window is not edge to edge anyway. Logged
        # rather than swallowed: if it ever fails on a device that has it, the
        # window silently stops covering the bars and it looks like a layout bug
        log.log("window: decor keeps the insets: %s: %s" % (type(e).__name__, e),
                debug=True)
    inset_by_hand(root)
    dialog.setContentView(root, ViewGroup.LayoutParams(-1, -1))


def inset_by_hand(root):
    """Pads the view by the system bars and the keyboard, and remembers by how.

    Letting the decor do it leaves the window ending at the navigation bar, so
    the strip behind the bar belongs to whatever was on screen before.
    """
    from android.view import View, WindowInsets
    from java import dynamic_proxy

    def padding_for(insets):
        try:
            bars = WindowInsets.Type.systemBars() | WindowInsets.Type.ime()
            values = insets.getInsets(bars)
            return (int(values.left), int(values.top),
                    int(values.right), int(values.bottom))
        except Exception:
            return (int(insets.getSystemWindowInsetLeft()),
                    int(insets.getSystemWindowInsetTop()),
                    int(insets.getSystemWindowInsetRight()),
                    int(insets.getSystemWindowInsetBottom()))

    class _Listener(dynamic_proxy(View.OnApplyWindowInsetsListener)):
        def onApplyWindowInsets(self, view, insets):
            try:
                values = padding_for(insets)
                view.setPadding(*values)
                # kept for `host window`: the numbers the system handed us
                view.setTag(_TAG, "insets %d,%d,%d,%d" % values)
            except Exception as e:
                log.error("window: cannot apply insets", e)
            return insets

    try:
        root.setOnApplyWindowInsetsListener(_Listener())
        root.setFitsSystemWindows(False)
    except Exception as e:
        log.error("window: cannot watch the insets", e)


# a view tag id has to be a resource id; this one is only read back by us
_TAG = 0x7F5C1101


def _prepare(dialog, palette):
    from android.graphics.drawable import ColorDrawable
    from android.view import View, WindowManager

    background = palette.role("bg")
    window = dialog.getWindow()
    if window is None:
        return

    def attempt(what, call, *args):
        try:
            call(*args)
        except Exception as e:
            log.log("window: %s: %s: %s" % (what, type(e).__name__, e), debug=True)

    params = WindowManager.LayoutParams
    attempt("background", window.setBackgroundDrawable, ColorDrawable(background))
    # FLAG_LAYOUT_IN_SCREEN alone buys the status bar and not the navigation
    # bar: the device measured a 2334px window against a 2400px screen, short
    # by exactly the 66px navigation bar, and that strip showed the screen
    # underneath. NO_LIMITS is what lets a window past the bars entirely. The
    # keyboard still makes room because the inset listener pads for it — the
    # window resizing was never what did that here.
    attempt("layout in screen", window.addFlags,
            params.FLAG_LAYOUT_IN_SCREEN | params.FLAG_LAYOUT_NO_LIMITS)
    attempt("no decor inset", window.clearFlags, params.FLAG_LAYOUT_INSET_DECOR)
    attempt("bar backgrounds", window.addFlags,
            params.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
    attempt("translucency", window.clearFlags,
            params.FLAG_TRANSLUCENT_STATUS | params.FLAG_TRANSLUCENT_NAVIGATION)
    attempt("dim", window.clearFlags, params.FLAG_DIM_BEHIND)
    attempt("soft input", window.setSoftInputMode,
            params.SOFT_INPUT_ADJUST_RESIZE | params.SOFT_INPUT_STATE_VISIBLE)
    # pre-35 only; on 35+ these are no-ops and on 36 they throw
    attempt("status bar color", window.setStatusBarColor, background)
    attempt("navigation bar color", window.setNavigationBarColor, background)

    try:
        decor = window.getDecorView()
        decor.setPadding(0, 0, 0, 0)
        # whatever the content does not cover, the window covers with this
        decor.setBackgroundColor(background)
        flags = int(decor.getSystemUiVisibility())
        if palette_module.luminance(background) > 0.5:
            flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        else:
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        decor.setSystemUiVisibility(flags)
    except Exception as e:
        log.log("window: cannot dress the decor: %s" % e, debug=True)


# ---------------------------------------------------------------- diagnostics

def describe(dialog, root=None):
    """Rows for `host window`: what the window actually turned out to be.

    Written because two rounds of fixing the navigation bar strip were guesses.
    The question is narrow — is the window as tall as the display, or does it
    stop at the bars — and this answers it without a screenshot.
    """
    rows = []

    def row(label, value):
        rows.append((label, str(value)))

    if dialog is None:
        return [("window", "no console window is open")]
    window = None
    try:
        window = dialog.getWindow()
    except Exception as e:
        row("window", "unreadable: %s" % e)
    if window is None:
        return rows or [("window", "the dialog has no window")]

    try:
        attributes = window.getAttributes()
        row("requested", "%s x %s" % (_size(attributes.width),
                                      _size(attributes.height)))
        row("flags", "0x%08x" % (int(attributes.flags) & 0xFFFFFFFF))
        row("soft input", "0x%x" % int(attributes.softInputMode))
    except Exception as e:
        row("attributes", "unreadable: %s" % e)

    decor_height = None
    try:
        decor = window.getDecorView()
        decor_height = int(decor.getHeight())
        row("decor", "%d x %d" % (int(decor.getWidth()), decor_height))
    except Exception as e:
        row("decor", "unreadable: %s" % e)

    for label, value in _display_rows():
        row(label, value)

    # the verdict, so the answer is not left as arithmetic for the reader
    _, screen_height = screen_size()
    if decor_height is not None and screen_height:
        short = screen_height - decor_height
        row("covers", "yes" if short <= 0 else "no, %dpx short of the bottom"
            % short)

    if root is not None:
        try:
            row("content", "%d x %d" % (int(root.getWidth()),
                                        int(root.getHeight())))
            row("padding", "%d,%d,%d,%d" % (
                int(root.getPaddingLeft()), int(root.getPaddingTop()),
                int(root.getPaddingRight()), int(root.getPaddingBottom())))
            tag = root.getTag(_TAG)
            if tag is not None:
                row("reported", str(tag))
        except Exception as e:
            row("content", "unreadable: %s" % e)
    return rows


def _size(value):
    value = int(value)
    return {-1: "match", -2: "wrap"}.get(value, str(value))


def _display_rows():
    rows = []
    try:
        from org.telegram.messenger import AndroidUtilities

        size = AndroidUtilities.displaySize
        rows.append(("display", "%d x %d" % (int(size.x), int(size.y))))
        rows.append(("bars", "status %d, navigation %d"
                     % (int(AndroidUtilities.statusBarHeight),
                        int(AndroidUtilities.navigationBarHeight))))
    except Exception as e:
        rows.append(("display", "unreadable: %s" % e))
    # the number the decor should match: the whole screen, bars included.
    # Telegram's displaySize is the usable area, which is why comparing against
    # it made a window that stopped at the navigation bar look correct.
    width, height = screen_size()
    rows.append(("screen", "%d x %d" % (width, height)
                 if width is not None else "unreadable"))
    return rows
