# SPDX-License-Identifier: Apache-2.0

"""What a command is allowed to know about the world.

Commands never import compat/ themselves. They ask the Context for a service,
which on a device is the real compat module and in tests is a fake. That is the
whole reason the command tests can run without Android.
"""

from .registry import CommandError


class Services(object):
    """Everything a command may reach outside itself.

    Attributes are set to None when the client does not provide them, so a
    command can report "unavailable here" rather than raising.
    """

    def __init__(self, host=None, plugins=None, paths=None, settings=None,
                 messaging=None, probe=None, log=None, policy=None,
                 reflect=None, terminal=None):
        self.host = host
        self.plugins = plugins
        self.paths = paths
        self.settings = settings
        self.messaging = messaging
        self.probe = probe
        self.log = log
        self.policy = policy
        self.reflect = reflect
        self.terminal = terminal


class Context(object):
    def __init__(self, services=None, env=None, width=40, registry=None,
                 assume_yes=False, origin="console", backend=None):
        self.services = services or Services()
        # shell state (shell/env.Env); commands reach variables and the cwd
        # through it rather than keeping their own idea of either
        self.env = env
        self.width = int(width)
        # (columns, lines) of the real screen, for a program that formats to it
        self.screen = None
        self.registry = registry
        self.backend = backend
        # set by the executor while a builtin runs, so a builtin can read a pipe
        self.stdin = None
        # set by the console: lets `source` run a script in this same shell
        self.run_script_text = None
        # set by the console: the list `history` reads and the up arrow walks
        self.history = None
        # set by the console: takes each Result as it happens instead of at the
        # end, so a slow command shows its work
        self.live = None
        # and this one takes a program's own output as it is, for the console
        # to hand straight to its terminal
        self.live_text = None
        # set by the console: `progress(title)` puts a card on the screen for a
        # job that takes minutes, so somebody who has left the console can
        # still see it is going. None where there is no screen to put one on.
        self.progress = None
        # the other direction: set by the console, offered a way to write into
        # a running program's terminal so that what is typed while it runs
        # reaches it instead of starting a new prompt
        self.attach_input = None
        # scripts and chat commands cannot answer a prompt
        self.assume_yes = bool(assume_yes)
        # "console" | "chat" | "script" -- some commands present differently
        self.origin = origin
        self.exit_requested = False
        self.clear_requested = False

    @property
    def cwd(self):
        return self.env.cwd if self.env is not None else "~"

    def display_cwd(self):
        return self.env.display_cwd() if self.env is not None else "~"

    def require(self, name):
        service = getattr(self.services, name, None)
        if service is None:
            raise CommandError(
                "%s is not available here" % name,
                hint="this command needs the client; it does not work off device",
            )
        return service

    def has(self, name):
        return getattr(self.services, name, None) is not None

    def request_exit(self):
        self.exit_requested = True

    def request_clear(self):
        self.clear_requested = True
