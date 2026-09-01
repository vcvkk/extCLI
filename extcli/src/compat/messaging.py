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


# ------------------------------------------------------------------- reading

# How long a history request may take before the console gives up on it. The
# request goes to Telegram, so the limit is somebody's mobile signal rather
# than anything this code does.
HISTORY_TIMEOUT = 20

# What one call may ask for. Telegram's own ceiling is 100 per request, and a
# console showing more than that at once is not being read anyway.
HISTORY_MAX = 100


def _await_request(request, timeout=HISTORY_TIMEOUT):
    """Sends a TL request and waits for the answer. (response, error)."""
    import threading

    done = threading.Event()
    box = {}

    def answered(response=None, error=None):
        box["response"] = response
        box["error"] = error
        done.set()

    try:
        _call("send_request")([
            ((request, answered), {}),
            ((request, answered, account()), {}),
        ])
    except Exception as e:
        log.error("messaging: cannot send the request", e)
        return None, "%s: %s" % (type(e).__name__, e)
    if not done.wait(timeout):
        return None, "timed out after %ss" % timeout
    return box.get("response"), box.get("error")


def _author_of(controller, raw):
    """Who wrote a message, as a Peer, or None when it does not say.

    A channel post has no author, and a message from a user carries a peer
    rather than an id — the field is `from_id.user_id` and not `from_id`.
    """
    from_id = reflect.get_field(raw, "from_id")
    if from_id is None:
        return None
    for field, sign in (("user_id", 1), ("chat_id", -1), ("channel_id", -1)):
        value = reflect.get_field(from_id, field)
        if value:
            return _peer_from_id(controller, sign * int(value))
    return None


def _media_kind(raw):
    """A word for what is attached, or None. The class name is the honest
    answer: TL_messageMediaPhoto is a photo however this client renders it."""
    media = reflect.get_field(raw, "media")
    if media is None:
        return None
    try:
        name = str(media.getClass().getSimpleName())
    except Exception:
        return "media"
    for prefix in ("TL_messageMedia", "TLRPC$TL_messageMedia"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.lower() or "media"


def _as_message(controller, raw):
    """One message as plain data.

    Everything above this module works on these dicts, so the shape is the
    boundary: no Java object gets past here, and the command and its tests
    never need a device.
    """
    author = _author_of(controller, raw)
    return {
        "id": int(reflect.get_field(raw, "id") or 0),
        "date": int(reflect.get_field(raw, "date") or 0),
        "text": str(reflect.get_field(raw, "message") or ""),
        "author": author.title if author is not None else None,
        "author_id": author.id if author is not None else None,
        "out": bool(reflect.get_field(raw, "out")),
        "media": _media_kind(raw),
    }


def _raw_history(peer_id, limit=20, before=0):
    """The messages as the client holds them, oldest first.

    Kept inside this module: a TL message is a Java object, and letting one
    past here is what would make everything above need a device.
    """
    controller = _controller()
    if controller is None:
        return []
    try:
        from org.telegram.tgnet import TLRPC
    except Exception as e:
        log.error("messaging: TLRPC is not available here", e)
        return []
    try:
        request = TLRPC.TL_messages_getHistory()
        request.peer = controller.getInputPeer(int(peer_id))
        request.limit = max(1, min(int(limit), HISTORY_MAX))
        request.offset_id = int(before)
    except Exception as e:
        log.error("messaging: cannot build the history request", e)
        return []

    response, error = _await_request(request)
    if error is not None:
        log.log("messaging: getHistory said %s" % error, debug=True)
    if response is None:
        return []
    raw = list(reflect.java_list_items(reflect.get_field(response, "messages")))
    raw.reverse()
    return raw


def history(peer_id, limit=20, before=0):
    """The last messages of a chat, oldest first, as plain data.

    Oldest first because that is reading order, and because a shell pipes what
    it is given: `tg read | tail` should be the end of the conversation, the
    way `cat` and `tail` agree about a file.
    """
    controller = _controller()
    if controller is None:
        return []
    out = []
    for raw in _raw_history(peer_id, limit, before):
        try:
            out.append(_as_message(controller, raw))
        except Exception as e:
            log.log("messaging: skipped a message: %s" % e, debug=True)
    return out


# --------------------------------------------------------------- attachments

# How long one file may take before the console stops waiting for it. Long,
# because it is a download over somebody's mobile signal, and the alternative
# to waiting is a file that arrives after the command has said it did not.
DOWNLOAD_TIMEOUT = 120

# How often the loader is asked whether the file has landed. Polling because
# the loader reports through the client's notification centre, and subscribing
# to that from here would mean holding a Java listener alive across threads for
# the sake of a progress bar nobody asked for.
POLL_SECONDS = 0.4


def _attachment_name(raw, message_id):
    """What to call the file on disk.

    A document carries its own name and that is what somebody expects to see.
    A photo carries none, so it is named after the message it came from —
    unique, and it says where to look if the picture turns out to be the wrong
    one.
    """
    document = reflect.get_field(raw, "media")
    document = reflect.get_field(document, "document") if document else None
    if document is not None:
        for attribute in reflect.java_list_items(
                reflect.get_field(document, "attributes")):
            name = reflect.get_field(attribute, "file_name")
            if name:
                return str(name)
    return "%d.jpg" % int(message_id)


def _local_path(raw):
    """Where the client already keeps this message's file, if anywhere."""
    import os

    attach = reflect.get_field(raw, "attachPath")
    if attach and os.path.isfile(str(attach)):
        return str(attach)
    try:
        from org.telegram.messenger import FileLoader

        loader = FileLoader.getInstance(int(account() or 0))
        found = reflect.try_call(loader, ["getPathToMessage"], raw,
                                 key="loader.path", default=None)
        if found is not None:
            path = str(found.getAbsolutePath())
            return path if os.path.isfile(path) else None
    except Exception as e:
        log.log("messaging: cannot locate the file: %s" % e, debug=True)
    return None


def _ask_for(raw):
    """Tells the client to fetch a message's file. Says whether it could ask."""
    try:
        from org.telegram.messenger import FileLoader

        loader = FileLoader.getInstance(int(account() or 0))
        result = reflect.try_call(
            loader, ["loadFile", "loadFileFromMessage", "loadMessageFile"],
            raw, key="loader.load", default="__missing__")
        return result != "__missing__"
    except Exception as e:
        log.log("messaging: cannot ask for the file: %s" % e, debug=True)
        return False


def _wait_for_file(raw, timeout):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        path = _local_path(raw)
        if path:
            return path
        time.sleep(POLL_SECONDS)
    return None


def download(peer_id, limit=20, before=0, target=".",
             timeout=DOWNLOAD_TIMEOUT):
    """Saves the attachments of the last messages into `target`.

    Returns a record per attachment, so the command can report what happened
    to each one rather than one verdict for the lot: on a phone, some of a
    batch arriving is the normal outcome, not a failure.
    """
    import os
    import shutil

    controller = _controller()
    if controller is None:
        return []
    out = []
    for raw in _raw_history(peer_id, limit, before):
        if reflect.get_field(raw, "media") is None:
            continue
        message_id = int(reflect.get_field(raw, "id") or 0)
        name = _attachment_name(raw, message_id)
        record = {"id": message_id, "name": name, "path": None,
                  "size": 0, "ok": False, "detail": ""}
        source = _local_path(raw)
        if source is None:
            if not _ask_for(raw):
                record["detail"] = "the client would not fetch it"
                out.append(record)
                continue
            source = _wait_for_file(raw, timeout)
        if source is None:
            record["detail"] = "did not arrive within %ss" % timeout
            out.append(record)
            continue
        try:
            os.makedirs(str(target), exist_ok=True)
            destination = os.path.join(str(target), name)
            shutil.copyfile(source, destination)
            record.update(path=destination, ok=True,
                          size=os.path.getsize(destination))
        except Exception as e:
            record["detail"] = "%s: %s" % (type(e).__name__, e)
        out.append(record)
    return out
