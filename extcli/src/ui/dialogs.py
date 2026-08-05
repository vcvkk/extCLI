# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Small dialogs used before the console exists (and for confirmations after).

The monospace report dialog is intentionally terminal-looking: same font and
colors the console will use, so diagnostics output can be read and copied
before any of the rendering work lands.
"""

from ..compat import fonts, theme
from ..utils import log


def _activity():
    from client_utils import get_last_fragment

    fragment = get_last_fragment()
    if fragment is None:
        return None, None
    return fragment.getParentActivity(), fragment


def show_text(title, body, copy_label="Copy", close_label="Close"):
    """Scrollable monospace report with a copy button."""
    from android.util import TypedValue
    from android.view import Gravity
    from android.widget import LinearLayout, ScrollView, TextView
    from org.telegram.messenger import AndroidUtilities
    from ui.alert import AlertDialogBuilder

    activity, _ = _activity()
    if activity is None:
        log.error("dialogs: no activity, cannot show '%s'" % title)
        return False

    text = body if isinstance(body, str) else "\n".join(str(line) for line in body)
    colors = theme.roles()
    dp = AndroidUtilities.dp

    container = LinearLayout(activity)
    container.setOrientation(LinearLayout.VERTICAL)
    container.setPadding(dp(20), dp(4), dp(20), dp(4))

    view = TextView(activity)
    view.setText(text)
    view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
    view.setTextColor(colors["fg"])
    view.setGravity(Gravity.START)
    typeface = fonts.mono_typeface()
    if typeface is not None:
        view.setTypeface(typeface)
    view.setTextIsSelectable(True)

    scroll = ScrollView(activity)
    scroll.addView(view)
    container.addView(scroll)

    builder = AlertDialogBuilder(activity)
    builder.set_title(str(title))
    builder.set_view(container)

    def on_copy(dialog, which):
        try:
            from android_utils import copy_to_clipboard

            copy_to_clipboard(text)
            from ui.bulletin import BulletinHelper

            BulletinHelper.show_copied_to_clipboard()
        except Exception as e:
            log.error("dialogs: copy failed", e)
        dialog.dismiss()

    builder.set_positive_button(str(copy_label), on_copy)
    builder.set_negative_button(str(close_label), lambda d, w: d.dismiss())
    builder.show()
    return True


def confirm(title, message, on_confirm, confirm_label="Continue",
            cancel_label="Cancel", destructive=False):
    """Yes/no dialog; `on_confirm` runs on the UI thread when accepted."""
    from ui.alert import AlertDialogBuilder

    activity, _ = _activity()
    if activity is None:
        log.error("dialogs: no activity, cannot confirm '%s'" % title)
        return False

    builder = AlertDialogBuilder(activity)
    builder.set_title(str(title))
    builder.set_message(str(message))

    def on_yes(dialog, which):
        dialog.dismiss()
        try:
            on_confirm()
        except Exception as e:
            log.error("dialogs: confirm handler failed", e)

    builder.set_positive_button(str(confirm_label), on_yes)
    builder.set_negative_button(str(cancel_label), lambda d, w: d.dismiss())
    if destructive:
        try:
            builder.make_button_red(AlertDialogBuilder.BUTTON_POSITIVE)
        except Exception:
            pass
    builder.show()
    return True


def toast(message, error=False):
    try:
        from ui.bulletin import BulletinHelper

        if error:
            BulletinHelper.show_error(str(message))
        else:
            BulletinHelper.show_info(str(message))
        return True
    except Exception as e:
        log.error("dialogs: bulletin failed", e)
        return False
