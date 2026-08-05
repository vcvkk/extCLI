# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Sending messages and finding out who to send them to.

The SDK's client_utils takes a dialog id, not a name: send_text(peer, text).
Turning "@durov", "saved" or a partial chat title into that id is this module's
job, and it only ever looks at dialogs the client has already loaded — extCLI
does not go resolving usernames over the network behind the user's back.
"""

from ..utils import log
from . import reflect


def account():
    try:
        from client_utils import get_selected_account

        return get_selected_account()
    except Exception:
        return None


# ------------------------------------------------- calling across SDK versions

def looks_like_a_signature_error(error):
    """True when a TypeError means "wrong arguments", not "bad value".

    Pure, and worth pinning: mistaking a TypeError raised *inside* the client
    for a signature mismatch would make extCLI retry a send that already
    half-happened.
    """
    text = str(error)
    return any(needle in text for needle in (
        "positional argument", "positional arguments", "takes no arguments",
        "unexpected keyword argument", "required positional",
        "argument after", "takes exactly", "takes at most", "takes at least",
    ))


class Call(object):
    """One client_utils function, called whichever way this SDK accepts.

    SDK 1.4.5.0 takes send_text(peer, text, account, parse_mode); 1.4.5.3 takes
    send_text(peer, text) and nothing else, and calling the first way raises
    TypeError. Rather than pinning a version, each form is tried in turn and the
    one that worked is remembered — the fallbacks lose parse_mode, which is said
    out loud in the log rather than silently.
    """

    def __init__(self, name, function):
        self.name = name
        self.function = function
        self._chosen = None

    def __call__(self, variants):
        if self._chosen is not None and self._chosen < len(variants):
            args, kwargs = variants[self._chosen]
            return self.function(*args, **kwargs)
        error = None
        for index, (args, kwargs) in enumerate(variants):
            try:
                value = self.function(*args, **kwargs)
            except TypeError as e:
                if not looks_like_a_signature_error(e):
                    raise
                error = e
                continue
            self._chosen = index
            if index:
                log.log("messaging: %s wants form %d of %d"
                        % (self.name, index + 1, len(variants)))
            return value
        raise error or TypeError("%s: no call form accepted" % self.name)


_calls = {}


def _call(name):
    """The wrapper for a client_utils function, created once."""
    if name not in _calls:
        import client_utils

        _calls[name] = Call(name, getattr(client_utils, name))
    return _calls[name]


def self_id():
    """The signed-in account's own id — the peer for Saved Messages."""
    try:
        from org.telegram.messenger import UserConfig

        return int(UserConfig.getInstance(UserConfig.selectedAccount).getClientUserId())
    except Exception as e:
        log.error("messaging: cannot read the account id", e)
        return None


class Peer(object):
    """A resolved destination."""

    def __init__(self, peer_id, title=None, username=None, kind="chat"):
        self.id = int(peer_id)
        self.title = title or str(peer_id)
        self.username = username
        self.kind = kind

    @property
    def is_self(self):
        own = self_id()
        return own is not None and self.id == own

    def label(self):
        if self.username:
            return "%s (@%s)" % (self.title, self.username)
        return self.title

    def as_fields(self):
        rows = [("id", str(self.id)), ("title", self.title), ("kind", self.kind)]
        if self.username:
            rows.append(("username", "@" + self.username))
        if self.is_self:
            rows.append(("note", "this is Saved Messages"))
        return rows


def _controller():
    try:
        from client_utils import get_messages_controller

        return get_messages_controller()
    except Exception as e:
        log.error("messaging: no messages controller", e)
        return None


def available():
    return _controller() is not None


def _user_title(user):
    first = reflect.get_field(user, "first_name") or ""
    last = reflect.get_field(user, "last_name") or ""
    name = ("%s %s" % (first, last)).strip()
    return name or "user"


def _peer_from_id(controller, dialog_id):
    """Builds a Peer from a dialog id, using whatever the client has cached."""
    dialog_id = int(dialog_id)
    try:
        if dialog_id > 0:
            user = controller.getUser(int(dialog_id))
            if user is not None:
                username = reflect.get_field(user, "username")
                return Peer(dialog_id, _user_title(user),
                            str(username) if username else None, "user")
        else:
            chat = controller.getChat(int(-dialog_id))
            if chat is not None:
                title = reflect.get_field(chat, "title")
                username = reflect.get_field(chat, "username")
                return Peer(dialog_id, str(title) if title else "chat",
                            str(username) if username else None, "chat")
    except Exception as e:
        log.log("messaging: cannot describe %s: %s" % (dialog_id, e), debug=True)
    return Peer(dialog_id)


def dialogs(limit=200):
    """Peers the client already has loaded, most recent first."""
    controller = _controller()
    if controller is None:
        return []
    raw = reflect.try_call(controller, ["getAllDialogs", "getDialogs"],
                           key="controller.dialogs")
    if raw is None:
        raw = reflect.get_field(controller, "dialogs")
    out = []
    for item in reflect.java_list_items(raw)[:limit]:
        dialog_id = reflect.get_field(item, "id")
        if dialog_id is None:
            continue
        out.append(_peer_from_id(controller, dialog_id))
    return out


def search(query, limit=20):
    """Loaded dialogs whose title, username or id matches."""
    needle = str(query).lstrip("@").lower()
    found = []
    for peer in dialogs():
        haystack = [peer.title.lower(), str(peer.id)]
        if peer.username:
            haystack.append(peer.username.lower())
        if any(needle in value for value in haystack):
            found.append(peer)
        if len(found) >= limit:
            break
    return found


def resolve(query):
    """Turns a user-typed destination into a Peer.

    Accepts `me`/`self`/`saved`, a numeric dialog id, `@username`, or enough of
    a chat title to be unambiguous. Raises LookupError with a readable reason.
    """
    text = str(query).strip()
    if not text:
        raise LookupError("no destination given")

    if text.lower() in ("me", "self", "saved"):
        own = self_id()
        if own is None:
            raise LookupError("cannot determine the current account")
        return Peer(own, "Saved Messages", None, "user")

    try:
        dialog_id = int(text)
    except ValueError:
        dialog_id = None
    if dialog_id is not None:
        controller = _controller()
        return _peer_from_id(controller, dialog_id) if controller else Peer(dialog_id)

    matches = search(text)
    exact = [p for p in matches
             if p.username and p.username.lower() == text.lstrip("@").lower()]
    if len(exact) == 1:
        return exact[0]
    if not matches:
        raise LookupError("no loaded chat matches %r" % text)
    if len(matches) > 1:
        names = ", ".join(p.label() for p in matches[:4])
        raise LookupError("%r matches %d chats: %s" % (text, len(matches), names))
    return matches[0]


# ------------------------------------------------------------------- sending

def send_text(peer_id, text, parse_mode=None):
    """Sends a message. Returns (ok, detail)."""
    try:
        peer, body = int(peer_id), str(text)
        _call("send_text")([
            ((peer, body, account(), parse_mode), {}),
            ((peer, body), {"account": account(), "parse_mode": parse_mode}),
            ((peer, body), {}),
        ])
        return True, "sent to %s" % peer_id
    except Exception as e:
        log.error("messaging: send_text failed", e)
        return False, "%s: %s" % (type(e).__name__, e)


def send_document(peer_id, path, caption=None, parse_mode=None):
    try:
        peer, name = int(peer_id), str(path)
        _call("send_document")([
            ((peer, name, caption, account(), parse_mode), {}),
            ((peer, name, caption), {}),
            ((peer, name), {}),
        ])
        return True, "sent %s" % path
    except Exception as e:
        log.error("messaging: send_document failed", e)
        return False, "%s: %s" % (type(e).__name__, e)


def send_photo(peer_id, path, caption=None, high_quality=True, parse_mode=None):
    try:
        peer, name = int(peer_id), str(path)
        _call("send_photo")([
            ((peer, name, caption, bool(high_quality), account(), parse_mode), {}),
            ((peer, name, caption, bool(high_quality)), {}),
            ((peer, name, caption), {}),
            ((peer, name), {}),
        ])
        return True, "sent %s" % path
    except Exception as e:
        log.error("messaging: send_photo failed", e)
        return False, "%s: %s" % (type(e).__name__, e)
