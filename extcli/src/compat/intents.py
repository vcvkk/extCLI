# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Handing a file to the rest of the phone.

The share sheet is Android's, and it already knows how to pick a chat, several
chats, or something that is not Telegram at all. Building a chat picker here
would be a worse copy of a thing the user already knows.

A file in the app's own directory cannot simply be named to another app —
Android stopped allowing that — so it goes through the client's FileProvider,
which is what turns a path into a content:// URI somebody else may open.
"""

from ..utils import log

# The client declares one; the name follows the package, and the package
# differs between the builds (beta, full). Asking the context is right on all
# of them, and this is the answer when the question cannot be asked.
FALLBACK_AUTHORITY = "com.exteragram.messenger.provider"

DEFAULT_MIME = "application/gzip"


def _activity():
    try:
        from client_utils import get_last_fragment

        fragment = get_last_fragment()
        return fragment.getParentActivity() if fragment else None
    except Exception:
        return None


def authority(context):
    """The FileProvider to publish through."""
    try:
        return "%s.provider" % str(context.getPackageName())
    except Exception:
        return FALLBACK_AUTHORITY


def share_file(path, mime=DEFAULT_MIME, title=None, activity=None):
    """Opens the share sheet for one file. Returns (ok, detail)."""
    activity = activity or _activity()
    if activity is None:
        return False, "no screen to share from"
    try:
        from android.content import Intent
        from androidx.core.content import FileProvider
        from java.io import File

        uri = FileProvider.getUriForFile(activity, authority(activity),
                                         File(str(path)))
        intent = Intent(Intent.ACTION_SEND)
        intent.setType(str(mime))
        intent.putExtra(Intent.EXTRA_STREAM, uri)
        # without this the receiving app gets a URI it is not allowed to read
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        activity.startActivity(Intent.createChooser(intent, title or "Share"))
        return True, "shared"
    except Exception as e:
        log.error("intents: cannot share %s" % path, e)
        return False, "%s: %s" % (type(e).__name__, e)
