# SPDX-License-Identifier: Apache-2.0

"""`send` and `search` — putting something in a chat, and finding one.

The rule that matters here: a destination is resolved against chats the client
already has, and an ambiguous one is refused. Guessing would mean a typo can
deliver a message to the wrong person, which is not a mistake a console should
be able to make quietly.

They are two commands rather than one with subcommands, so `send` never does
anything but send.
"""

import pytest

from extcli_src import policy as policy_module
from extcli_src.render import plain
from extcli_src.shell import dispatch
from extcli_src.shell.builtins import build_registry
from extcli_src.shell.context import Context, Services
from extcli_src.shell.env import Env

SELF_ID = 1000


class FakePeer(object):
    def __init__(self, peer_id, title, username=None, kind="user"):
        self.id = peer_id
        self.title = title
        self.username = username
        self.kind = kind

    @property
    def is_self(self):
        return self.id == SELF_ID

    def label(self):
        return "%s (@%s)" % (self.title, self.username) if self.username else self.title

    def as_fields(self):
        rows = [("id", str(self.id)), ("title", self.title), ("kind", self.kind)]
        if self.username:
            rows.append(("username", "@" + self.username))
        if self.is_self:
            rows.append(("note", "this is Saved Messages"))
        return rows


class FakeMessaging(object):
    def __init__(self):
        self.peers = [
            FakePeer(SELF_ID, "Saved Messages"),
            FakePeer(2001, "Pavel Durov", "durov"),
            FakePeer(2002, "Durov's News", "durovs_news"),
            FakePeer(-3001, "extCLI testing", kind="chat"),
        ]
        self.sent = []

    def dialogs(self, limit=200):
        return list(self.peers)[:limit]

    def search(self, query, limit=20):
        needle = str(query).lstrip("@").lower()
        found = []
        for peer in self.peers:
            values = [peer.title.lower(), str(peer.id)]
            if peer.username:
                values.append(peer.username.lower())
            if any(needle in value for value in values):
                found.append(peer)
        return found[:limit]

    def resolve(self, query):
        text = str(query).strip()
        if text.lower() in ("me", "self", "saved"):
            return self.peers[0]
        try:
            return FakePeer(int(text), "chat %s" % text)
        except ValueError:
            pass
        matches = self.search(text)
        exact = [p for p in matches
                 if p.username and p.username.lower() == text.lstrip("@").lower()]
        if len(exact) == 1:
            return exact[0]
        if not matches:
            raise LookupError("no loaded chat matches %r" % text)
        if len(matches) > 1:
            raise LookupError("%r matches %d chats: %s"
                              % (text, len(matches),
                                 ", ".join(p.label() for p in matches[:4])))
        return matches[0]

    def send_text(self, peer_id, text, parse_mode=None):
        self.sent.append(("text", peer_id, text, parse_mode))
        return True, "sent to %s" % peer_id

    def send_document(self, peer_id, path, caption=None, parse_mode=None):
        self.sent.append(("document", peer_id, path, caption))
        return True, "sent %s" % path

    def send_photo(self, peer_id, path, caption=None, high_quality=True,
                   parse_mode=None):
        self.sent.append(("photo", peer_id, path, caption))
        return True, "sent %s" % path


@pytest.fixture
def shell(tmp_path):
    home = str(tmp_path)
    messaging = FakeMessaging()
    ctx = Context(
        services=Services(messaging=messaging, policy=policy_module),
        registry=build_registry(),
        env=Env(cwd=home, home=home),
        width=70,
    )

    def run(line):
        return dispatch.run_line(line, ctx)

    run.messaging = messaging
    run.ctx = ctx
    run.home = home
    run.out = lambda line: plain.text(run(line))
    return run


# ----------------------------------------------------------------- resolving

def test_send_to_saved_messages(shell):
    assert shell("send me hello").ok
    assert shell.messaging.sent == [("text", SELF_ID, "hello", None)]


def test_send_by_exact_username(shell):
    shell("send @durov hi there")
    assert shell.messaging.sent[-1][:3] == ("text", 2001, "hi there")


def test_send_by_numeric_id(shell):
    shell("send 2002 direct")
    assert shell.messaging.sent[-1][1] == 2002


def test_ambiguous_destination_is_refused(shell):
    # "duro" matches two titles and no username exactly
    result = shell("send duro something")
    assert result.code != 0
    assert "matches 2 chats" in result.blocks[0].message
    assert "try `search" in (result.blocks[0].hint or "")
    assert shell.messaging.sent == []


def test_an_exact_username_wins_over_a_partial_title(shell):
    # "durov" is a username and also part of "Durov's News"; the exact one wins
    assert shell("send durov hi").ok
    assert shell.messaging.sent[-1][1] == 2001


def test_unknown_destination_is_refused(shell):
    result = shell("send @nobody hi")
    assert result.code != 0
    assert "no loaded chat matches" in result.blocks[0].message
    assert shell.messaging.sent == []


def test_missing_arguments(shell):
    result = shell("send")
    assert result.code != 0
    assert "needs a destination" in result.blocks[0].message
    result = shell("send me")
    assert result.code != 0
    assert "needs a message" in result.blocks[0].message


def test_words_are_joined_into_one_message(shell):
    shell("send me one two three")
    assert shell.messaging.sent[-1][2] == "one two three"


def test_quoted_text_survives_the_shell(shell):
    shell('send me "hello, world"')
    assert shell.messaging.sent[-1][2] == "hello, world"


def test_markdown_flag(shell):
    shell("send me --markdown *bold*")
    assert shell.messaging.sent[-1][3] == "markdown"


# ------------------------------------------------------------------- lookups

def test_search_lists_matches(shell):
    text = shell.out("search durov")
    assert "Pavel Durov" in text and "Durov's News" in text
    assert "2 chats" in text


def test_search_without_matches(shell):
    assert "no loaded chat matches" in shell.out("search zzzz")


def test_search_needs_a_query(shell):
    assert shell("search").code != 0


def test_search_id_shows_the_resolved_peer(shell):
    text = shell.out("search --id @durov")
    assert "2001" in text and "durov" in text


def test_search_id_marks_saved_messages(shell):
    assert "Saved Messages" in shell.out("search --id me")


def test_search_id_refuses_an_ambiguous_name(shell):
    result = shell("search --id duro")
    assert result.code != 0
    assert "matches 2 chats" in result.blocks[0].message


# --------------------------------------------------------------------- files

def test_send_file(shell):
    shell("echo content > note.txt")
    assert shell("send me --file note.txt").ok
    kind, peer, path, caption = shell.messaging.sent[-1]
    assert kind == "document" and peer == SELF_ID
    assert path.endswith("note.txt") and caption is None


def test_the_rest_of_the_line_is_the_caption(shell):
    shell("echo fake > picture.jpg")
    shell("send @durov --photo picture.jpg look at this")
    kind, peer, path, caption = shell.messaging.sent[-1]
    assert kind == "photo" and peer == 2001
    assert caption == "look at this"


def test_caption_can_also_be_a_flag(shell):
    shell("echo content > note.txt")
    shell("send me --file note.txt --caption look")
    assert shell.messaging.sent[-1][3] == "look"


def test_send_photo(shell):
    shell("echo fake > picture.jpg")
    shell("send me --photo picture.jpg")
    assert shell.messaging.sent[-1][0] == "photo"


def test_a_file_and_a_photo_together_are_refused(shell):
    shell("echo fake > picture.jpg")
    result = shell("send me --photo picture.jpg --file picture.jpg")
    assert result.code != 0
    assert shell.messaging.sent == []


def test_send_missing_file(shell):
    result = shell("send me --file nothing.txt")
    assert result.code != 0
    assert "no such file" in result.blocks[0].message
    assert shell.messaging.sent == []


# -------------------------------------------------------------------- policy

def test_sending_to_others_is_a_different_policy_action(shell):
    seen = []
    original = policy_module.check

    def spy(action, detail="", assume_yes=False):
        seen.append(action)
        return original(action, detail, assume_yes)

    policy_module.check = spy
    try:
        shell("send me to myself")
        shell("send @durov to someone else")
    finally:
        policy_module.check = original
    assert policy_module.SEND_MESSAGE in seen
    assert policy_module.SEND_TO_OTHERS in seen


def test_without_the_client_send_explains_itself():
    ctx = Context(registry=build_registry(), env=Env(cwd="/", home="/"), width=60)
    result = dispatch.run_line("send me hi", ctx)
    assert "messaging is not available here" in result.blocks[0].message


# ---------------------------------------------------------------- completion

def test_completion_offers_chats(shell):
    candidates = shell.ctx.registry.complete(shell.ctx, ["send", ""], False)
    assert any("durov" in name for name in candidates)


def test_send_and_search_are_separate_commands(shell):
    names = shell.ctx.registry.names()
    assert "send" in names and "search" in names
    # `send search ...` used to work and must not quietly mean something else
    assert shell("send search durov").code != 0


# ------------------------------------------------------ calling the real SDK

class Recorder(object):
    """Stands in for a client_utils function with a fixed signature."""

    def __init__(self, accepts):
        self.accepts = accepts
        self.calls = []

    def __call__(self, *args, **kwargs):
        if len(args) != self.accepts or kwargs:
            raise TypeError("send_text() takes exactly %d positional arguments "
                            "(%d given)" % (self.accepts, len(args)))
        self.calls.append(args)
        return True


def test_a_call_finds_the_form_this_sdk_accepts():
    """SDK 1.4.5.0 takes four arguments, 1.4.5.3 takes two.

    Pinning either one means the other raises TypeError and the message never
    goes out, which is exactly what happened on the device.
    """
    from extcli_src.compat import messaging

    function = Recorder(accepts=2)
    call = messaging.Call("send_text", function)
    call([((1, "hi", 0, None), {}), ((1, "hi"), {})])
    assert function.calls == [(1, "hi")]


def test_the_working_form_is_remembered():
    from extcli_src.compat import messaging

    function = Recorder(accepts=2)
    call = messaging.Call("send_text", function)
    variants = [((1, "hi", 0, None), {}), ((1, "hi"), {})]
    call(variants)
    call(variants)
    assert len(function.calls) == 2


def test_a_type_error_from_inside_the_client_is_not_retried():
    """Retrying it would send the same message twice."""
    from extcli_src.compat import messaging

    attempts = []

    def angry(*args, **kwargs):
        attempts.append(args)
        raise TypeError("cannot convert 'NoneType' to a peer")

    call = messaging.Call("send_text", angry)
    with pytest.raises(TypeError):
        call([((1, "hi", 0, None), {}), ((1, "hi"), {})])
    assert len(attempts) == 1


def test_signature_errors_are_told_apart_from_value_errors():
    from extcli_src.compat import messaging

    assert messaging.looks_like_a_signature_error(
        TypeError("send_text() takes exactly 2 positional arguments (4 given)"))
    assert messaging.looks_like_a_signature_error(
        TypeError("send_text() got an unexpected keyword argument 'account'"))
    assert not messaging.looks_like_a_signature_error(
        TypeError("cannot convert 'NoneType' to a peer"))


def test_when_no_form_works_the_last_error_surfaces():
    from extcli_src.compat import messaging

    call = messaging.Call("send_text", Recorder(accepts=9))
    with pytest.raises(TypeError):
        call([((1, "hi"), {})])
