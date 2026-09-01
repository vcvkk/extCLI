# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Running a command from a chat, and answering in that chat.

`.cli <command>` typed into a chat used to open the console and run it there.
That answers a different question from the one asked: somebody who types a
command into a chat wants the answer in the chat, not a screen over it.

So the command runs and one message is edited as its output arrives. What the
message says and when it is written is `shell.live.LiveText`, which is pure and
tested; this module is the part that needs a client — sending the first
message, running the shell, and editing.

If the client will not say which message it just sent, the message cannot be
edited and there is nothing to update. That is not treated as a failure: the
console opens instead, which is what used to happen every time.
"""

from .render import plain
from .shell import dispatch, live
from .ui import prefs
from .utils import log

# What the message says before anything has come back. A command that takes a
# minute should not leave a chat looking like nothing happened.
STARTING = "$ %s\n…"


def _now():
    import time

    return time.time()


def _context(plugin):
    """A shell to run in — the console's own if one is open.

    Sharing it means `cd` and the variables carry between the chat and the
    console, which is what somebody using both would expect. Without a console
    a fresh one is built, and it lasts as long as the command.
    """
    from .ui import console as console_module

    session = console_module.live_session()
    if session is not None:
        context = session.make_context(origin="chat")
        return context, session
    from .backends import chain
    from .compat import paths
    from .shell.builtins import build_registry
    from .shell.context import Context
    from .shell.env import Env
    from . import services as services_module

    home = paths.home_dir()
    context = Context(services=services_module.build(plugin),
                      env=Env(cwd=home, home=home),
                      registry=build_registry(), width=60,
                      origin="chat", backend=chain.build())
    context.assume_yes = True
    return context, None


def run(plugin, peer_id, command, messaging=None):
    """Runs `command` and keeps a message in `peer_id` up to date with it.

    Returns True when the chat is being answered; False when it cannot be and
    the caller should fall back to opening the console.
    """
    from .compat import messaging as messaging_module

    messaging = messaging or messaging_module
    ok, message_id, detail = messaging.send_for_editing(
        peer_id, STARTING % command)
    if not ok:
        log.error("relay: could not start the reply: %s" % detail)
        return False
    if message_id is None:
        log.log("relay: this client will not say which message it sent, "
                "so there is nothing to keep updating", debug=True)
        return False

    view = live.LiveText(interval=prefs.chat_interval(),
                         header="$ %s" % command)
    context, _session = _context(plugin)

    def flush(force=False):
        now = _now()
        if not force and not view.due(now):
            return
        edited, said = messaging.edit_text(peer_id, message_id, view.text())
        if edited:
            view.sent(now)
            return
        wait = live.flood_wait_seconds(said)
        if wait is not None:
            # the server said exactly how long; it knows better than any
            # interval picked in advance
            log.log("relay: told to wait %ss" % wait, debug=True)
            view.defer(now, wait)

    def show_text(text):
        view.write(text)
        flush()

    def show_result(result):
        view.write(plain.text(result) + "\n")
        flush()

    previous = context.live, context.live_text
    context.live, context.live_text = show_result, show_text
    try:
        result = dispatch.run_line(command, context)
        if result is not None:
            view.write(plain.text(result))
        view.finish()
        flush(force=True)
    except Exception as e:
        log.error("relay: %s failed" % command, e)
        view.write("\n%s: %s" % (type(e).__name__, e))
        view.finish()
        flush(force=True)
    finally:
        context.live, context.live_text = previous
    return True
