# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""The console as a bottom sheet.

Two reasons this exists rather than only the full-screen fragment. It is the
surface chosen for `.cli` from a chat — the chat stays where it is and the
terminal slides up over it — and it does not depend on a fragment template
honouring a returned view, which is the part that left the screen blank.

The layout is exactly the same as the full-screen console: the same builder
produces it, so there is one console, shown two ways.
"""

from ..utils import log
from . import console

# fraction of the screen the sheet takes when it opens
HEIGHT_FRACTION = 0.75


def open_sheet(plugin, initial_command=None):
    """Shows the console in a bottom sheet. Returns the session, or None."""
    try:
        from client_utils import get_last_fragment
        from org.telegram.ui.ActionBar import BottomSheet
    except Exception as e:
        log.error("sheet: client UI classes unavailable", e)
        return None

    fragment = get_last_fragment()
    activity = fragment.getParentActivity() if fragment else None
    if activity is None:
        log.error("sheet: no activity")
        return None

    session = console.resume_or_create(plugin, activity)

    try:
        resource_provider = fragment.getResourceProvider() if fragment else None
    except Exception:
        resource_provider = None

    try:
        sheet = BottomSheet(activity, False, resource_provider)
        sheet.setApplyBottomPadding(False)
        sheet.setApplyTopPadding(False)
    except Exception as e:
        log.error("sheet: cannot create the bottom sheet", e)
        return None

    try:
        root = console.build_view(session)
    except Exception as e:
        log.error("sheet: cannot build the console", e)
        try:
            root = console.error_view(
                activity, session.palette, "The console could not start.",
                "%s: %s\n\n%s" % (type(e).__name__, e,
                                  "\n".join(log.traceback_lines())))
        except Exception:
            return None

    try:
        _set_height(activity, root)
        sheet.setCustomView(root)
        session.window = sheet
        sheet.show()
        _allow_keyboard(sheet)
        log.log("sheet: console opened")
    except Exception as e:
        log.error("sheet: cannot show the console", e)
        return None

    _detach_on_dismiss(sheet, session)
    session.start(initial_command)
    return session


def _detach_on_dismiss(sheet, session):
    """Dismissing the sheet takes the views down, not the session.

    Same rule as the full screen: only `exit` ends a session, so the scrollback
    and anything still running survive being swiped away.
    """
    try:
        from android.content import DialogInterface
        from java import dynamic_proxy

        class _OnDismiss(dynamic_proxy(DialogInterface.OnDismissListener)):
            def onDismiss(self, dialog):
                session.detach()
                return None

        sheet.setOnDismissListener(_OnDismiss())
    except Exception as e:
        log.error("sheet: cannot watch for dismissal", e)


def _allow_keyboard(sheet):
    """A sheet window keeps the keyboard out by default.

    Without this the input holds focus, the caret blinks in the terminal, and
    no keyboard ever appears — which looks exactly like a dead text field.
    """
    try:
        from android.view import WindowManager

        window = sheet.getWindow()
        window.clearFlags(WindowManager.LayoutParams.FLAG_ALT_FOCUSABLE_IM)
        window.setSoftInputMode(
            WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
            | WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE
        )
    except Exception as e:
        log.error("sheet: cannot enable the keyboard", e)


def _set_height(activity, root):
    """A terminal needs room; without a height the sheet wraps to nothing."""
    try:
        from android.widget import FrameLayout
        from org.telegram.messenger import AndroidUtilities

        height = int(AndroidUtilities.displaySize.y * HEIGHT_FRACTION)
        root.setLayoutParams(FrameLayout.LayoutParams(-1, height))
        root.setMinimumHeight(height)
    except Exception as e:
        log.error("sheet: cannot size the console", e)
