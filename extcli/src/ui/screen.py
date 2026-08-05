# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""The console as a full-screen window of its own.

This used to be a UniversalFragment, and it fought us the whole way: the
template builds an ActionBar we do not want (it flashed on open and left its
space behind), it does not always call afterCreateView (so the console came up
with no greeting and no keyboard), and its back handling never reached us.

A Dialog has none of that. There is no header to hide because none is created,
the window is ours so its status bar color does not leak into the activity's
recents snapshot, and back is handled by the platform: it dismisses the window,
which leaves the screen without ending the session. `exit` is what ends it.
"""

from ..utils import log
from . import console, window as window_module


def open_screen(plugin, initial_command=None):
    """Shows the console full screen. Returns the session, or None."""
    try:
        from android.app import Dialog
        from client_utils import get_last_fragment
    except Exception as e:
        log.error("screen: client UI classes unavailable", e)
        return None

    fragment = get_last_fragment()
    activity = fragment.getParentActivity() if fragment else None
    if activity is None:
        log.error("screen: no activity")
        return None

    session = console.resume_or_create(plugin, activity)

    try:
        root = console.build_view(session)
    except Exception as e:
        log.error("screen: cannot build the console", e)
        try:
            root = console.error_view(
                activity, session.palette, "The console could not start.",
                "%s: %s\n\n%s" % (type(e).__name__, e,
                                  "\n".join(log.traceback_lines())))
        except Exception:
            return None

    try:
        from android.view import Window

        dialog = Dialog(activity)
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE)
        window_module.attach(dialog, root, session.palette)
        dialog.setCanceledOnTouchOutside(False)
        _detach_on_dismiss(dialog, session)
        session.window = dialog
        session.window_root = root
        dialog.show()
        # only now: a dialog applies its window attributes when it is shown,
        # so a size set beforehand is discarded and the window wraps its
        # content — which is how the console came up as a small box
        window_module.expand(dialog)
    except Exception as e:
        log.error("screen: cannot show the console", e)
        return None

    session.start(initial_command)
    log.log("screen: console opened")
    return session


def _detach_on_dismiss(dialog, session):
    """Back takes the screen down and leaves the session running."""
    try:
        from android.content import DialogInterface
        from java import dynamic_proxy

        class _OnDismiss(dynamic_proxy(DialogInterface.OnDismissListener)):
            def onDismiss(self, dismissed):
                try:
                    session.detach()
                except Exception as e:
                    log.error("screen: detach failed", e)
                return None

        dialog.setOnDismissListener(_OnDismiss())
    except Exception as e:
        log.error("screen: cannot watch for dismissal", e)
