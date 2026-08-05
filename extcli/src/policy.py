# SPDX-License-Identifier: Apache-2.0

"""Single gate for anything destructive or privacy-sensitive.

Right now extCLI is a development build and everything is allowed. That is a
deliberate, temporary state: every risky operation still has to ask this module
first, so turning on real confirmations before a public release is a change
here and nowhere else.

Commands must never decide on their own whether an action is safe.
"""

from .utils import log

# what a caller is asking to do
FS_WRITE = "fs.write"          # create/modify a file outside extCLI's own data
FS_DELETE = "fs.delete"        # remove files or directories
CLIENT_CONFIG = "client.config"  # change an exteraGram setting
PLUGIN_STATE = "plugin.state"  # enable/disable/uninstall another plugin
SEND_MESSAGE = "send.message"  # put something in a chat
SEND_TO_OTHERS = "send.foreign"  # output visible to someone other than the user
CODE_EVAL = "code.eval"        # run arbitrary Python inside the client
EXEC_EXTERNAL = "exec.external"  # spawn a process outside the interpreter
NETWORK = "net.fetch"          # fetch something from outside the phone

# Actions whose output or effect can leak into a chat with other people, or
# that cannot be undone. Kept as data so the release build only has to switch
# the mode below, not rewrite call sites.
SENSITIVE = frozenset({
    FS_DELETE,
    CLIENT_CONFIG,
    PLUGIN_STATE,
    SEND_TO_OTHERS,
    CODE_EVAL,
})

MODE_ALLOW_ALL = "allow_all"
MODE_CONFIRM = "confirm"

_mode = MODE_ALLOW_ALL

# set by main.py once the UI exists: confirm(action, detail, on_allow, on_deny)
_confirm_handler = None


class Denied(Exception):
    """Raised when an action is refused; commands report it as an error."""

    def __init__(self, action, reason):
        super().__init__("%s: %s" % (action, reason))
        self.action = action
        self.reason = reason


def set_mode(mode):
    global _mode
    _mode = mode if mode in (MODE_ALLOW_ALL, MODE_CONFIRM) else MODE_ALLOW_ALL


def mode():
    return _mode


def set_confirm_handler(handler):
    global _confirm_handler
    _confirm_handler = handler


def is_sensitive(action):
    return action in SENSITIVE


def check(action, detail="", assume_yes=False):
    """Allows or refuses an action. Returns True when the caller may proceed.

    In MODE_ALLOW_ALL nothing is blocked; sensitive actions are still logged so
    a user can see afterwards what a script did on their account.
    """
    if is_sensitive(action):
        log.log("policy: %s (%s)" % (action, detail or "-"))
    if _mode == MODE_ALLOW_ALL:
        return True
    if not is_sensitive(action) or assume_yes:
        return True
    if _confirm_handler is None:
        raise Denied(action, "confirmation required but no UI available")
    return bool(_confirm_handler(action, detail))


def require(action, detail="", assume_yes=False):
    """check(), but raises Denied instead of returning False."""
    if not check(action, detail, assume_yes):
        raise Denied(action, "refused by user")
    return True
