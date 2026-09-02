# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""The console screen.

Modelled on Termux: the screen is the terminal and the two key rows, and
nothing else. There is no visible text field — typing goes into a transparent
EditText and the terminal draws the line at the prompt itself — and no header.

The session outlives the screen. Leaving with the back gesture takes the views
down and keeps everything else: the scrollback, the shell environment, the
running command. Only `exit` ends it. That is why the transcript lives on the
session rather than in the terminal widget — the widget belongs to a window
that can be dismissed at any moment, and the text must not go with it.

This module builds the console; ui/screen.py and ui/sheet.py are the two
windows it can appear in.

Commands run off the UI thread (`plugin reload` and the probe both take long
enough to freeze a frame otherwise) and their output is appended back on it.
"""

from .. import services as services_module
from ..compat import fonts, paths, theme
from ..render import palette as palette_module
from ..render import styles
from ..render import blocks
from ..backends import chain
from ..shell import dispatch
from ..shell.builtins import build_registry
from ..shell.context import Context
from ..shell.env import Env
from ..term import bridge, textview
from ..utils import log
from . import prefs, softkeys

HISTORY_LIMIT = 200
HISTORY_FILE = "history"

# how much scrollback the session keeps when the setting cannot be read
TRANSCRIPT_LIMIT = 1500

# Read once per session, if it is there. Named for the plugin rather than
# .profile: this is not a login shell, and a file called .profile in a home
# directory the client also uses would be a promise about a wider world.
RC_FILE = ".extclirc"

# how much the invisible field may collect before it is tidied away
RAW_FIELD_LIMIT = 200

# What the key row sends while a program owns the screen. The same bytes a
# terminal sends, so a program that reads them with terminfo finds what it
# expects; without these an editor gets nothing from the row but our own idea
# of what the keys mean.
RAW_KEYS = {
    "cancel": "\x1b",
    "complete": "\t",
    "left": "\x1b[D",
    "right": "\x1b[C",
    "history_prev": "\x1b[A",
    "history_next": "\x1b[B",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "page_up": "\x1b[5~",
    "page_down": "\x1b[6~",
}

# The session that is still alive, if any. Closing the screen does not end a
# session, so the next open has to find the old one rather than start over.
_live = None


def live_session():
    return _live


def end_live_session():
    """Forgets the running session; the next open starts clean.

    The views are not touched here — the fragment tears them down on its way
    out, and doing it early would leave `close` with nothing to finish.
    """
    global _live

    session, _live = _live, None
    return session


def current_palette():
    """Palette for the console: Termux's colors, the client theme, or Amoled."""
    name = prefs.theme_name()
    if name == "termux":
        return palette_module.termux()
    roles = theme.roles()
    if name == "amoled":
        return palette_module.amoled(roles)
    return palette_module.from_client(roles, name)


# What a program is told it is writing to. A program asks this before it asks
# anything else, and answers about colour, line drawing and cursor movement
# from it. Modest on purpose: something every terminfo has, rather than
# something that invites escape sequences this console has never parsed.
TERM = "xterm-256color"


def _new_screen():
    from ..render.styles import base

    return base.Screen()


def _with_terminal(env):
    env.set("TERM", TERM, export=True)
    return env


class ConsoleSession(object):
    """One console: terminal, command registry and history."""

    def __init__(self, plugin, activity):
        self.plugin = plugin
        self.activity = activity
        self.palette = current_palette()
        self.registry = build_registry()
        self.services = services_module.build(plugin)
        # `host check --window` asks the console about itself; going through Services
        # is what keeps shell/ from importing ui/
        self.services.terminal = self
        self.backend = self._build_backends()
        self.shell_env = self._new_env()
        self.context = None
        self.terminal = None
        self.renderer_kind = None
        self.style = None
        self.input_view = None
        self.status_view = None
        # the dialog showing this session — full screen or sheet, both dismiss
        self.window = None
        self.window_root = None
        # read once: a session that changed its own limit halfway through
        # would have kept some lines by one rule and some by another
        self.scrollback = self._scrollback()
        self.history = self._load_history()
        self._history_index = len(self.history)
        self._busy = False
        self._streamed = False
        # the transcript's own screen: the lines a program can still reach,
        # kept in step with the terminal's
        self._screen = _new_screen()
        # a way to write into the running program's terminal, while there is
        # one. Set from the thread the command runs on, read from the UI one.
        self._input_channel = None
        self._interrupts = 0
        self._raw_interrupts = 0
        # whether the keyboard has been going to a program rather than to us
        self._typed_raw = False
        # every line ever written, so a new screen can replay it
        self.transcript = []
        self._started = False       # greeted once, ever
        self._screen_ready = False  # a screen is currently showing this session
        # input state: the terminal draws the typed line, so the session has to
        # know what is in the field and when a change came from its own hand
        self._last_input = ""
        self._suppress = False
        self._ctrl_armed = False
        self._selection_open = False
        self.set_ctrl_indicator = lambda armed: None

    # ------------------------------------------------------------------ setup

    def _scrollback(self):
        try:
            return int(prefs.scrollback())
        except Exception:
            return TRANSCRIPT_LIMIT

    def _build_backends(self):
        """The chain, and the world its paths are in.

        Both at once, because they are the same decision: with a rootfs the
        shell lives inside it and its commands come from it, and without one
        the shell is on the phone and they come from toybox. A chain built for
        one and paths built for the other would answer about different places.
        """
        from ..compat import host

        self.shell_paths = self._build_paths()
        try:
            return chain.build(native_dir=paths.native_dir(), abi=host.abi(),
                               rootfs=self._build_rootfs(),
                               paths=self.shell_paths)
        except Exception as e:
            log.error("console: falling back to the plain backend chain", e)
            self.shell_paths = None
            return chain.build()

    def _build_paths(self):
        """The mount table, or None when there is no rootfs to be inside."""
        from ..rootfs import layout, mounts

        try:
            root = paths.rootfs_dir()
            if not layout.installed(root) or not layout.saved_strategy(root):
                return None
            values = prefs.mount_values()
            return mounts.Paths(mounts.table(values, prefs.mount_hosts()),
                                values)
        except Exception as e:
            log.error("console: cannot read the mount table", e)
            return None

    def _build_rootfs(self):
        """The backend that runs Alpine's programs, if there is an Alpine."""
        if self.shell_paths is None:
            return None
        from ..backends import linker as linker_module
        from ..compat import host
        from ..compat import network as compat_network
        from ..rootfs import backend as rootfs_backend
        from ..rootfs import layout, mounts, native, network, sandbox, writes

        try:
            root = paths.rootfs_dir()
            # which resolver the phone uses changes with the network it is on,
            # so the guest is given the current one every time rather than the
            # one that was right when the rootfs was unpacked
            network.write(root, compat_network.dns_servers())
            abi = host.abi()
            found = linker_module.find_linker(abi)
            if not found:
                return None
            return rootfs_backend.RootfsBackend(
                root, found, layout.saved_strategy(root),
                native_dir=native.directory(paths.res_dir(), abi),
                blocked=sandbox.blocked_for(paths.state_dir()),
                mount_rows=self.shell_paths.rows,
                start=mounts.start(self.shell_paths.values),
                no_tmpfile=writes.needs_named_temporary(
                    writes.load(paths.state_dir())))
        except Exception as e:
            log.error("console: cannot use the rootfs", e)
            return None

    def rebuild_backends(self):
        """The world changed underneath a session that is already open.

        A rootfs unpacking itself in the background is the case this exists
        for: a console opened during it was built without one, and would go on
        answering "not found" to `apk` until it was closed and opened again.

        The shell environment is only replaced when it has to be — the cwd,
        the variables and the aliases are the user's, and a console that
        forgets where it was standing is worse than one that is late.
        """
        was_inside = getattr(self, "shell_paths", None) is not None
        try:
            self.backend = self._build_backends()
        except Exception as e:
            log.error("console: cannot rebuild the backends", e)
            return False
        if self.context is not None:
            self.context.backend = self.backend
        if not was_inside and getattr(self, "shell_paths", None) is not None:
            # There was no rootfs to be inside before, and now there is. The
            # shell moves into it rather than being replaced — the variables,
            # the aliases and the functions are the user's work, and a fresh
            # Env would throw all of it away to change two paths.
            self._move_into_rootfs()
        return True

    def _move_into_rootfs(self):
        import os

        home = self.shell_paths.home()
        try:
            os.makedirs(self.shell_paths.host(home), exist_ok=True)
        except Exception as e:
            log.error("console: cannot prepare %s" % home, e)
        env = self.shell_env
        env.paths = self.shell_paths
        env.home = home
        env.cwd = home
        env.set("HOME", home, export=True)
        env.set("PWD", home, export=True)
        self._post(self.refresh_input_line)

    def _new_env(self):
        """A shell session, in whichever world this console is in.

        Inside the rootfs that is the guest's home, and the paths object turns
        it into somewhere real for anything that opens a file. Without a rootfs
        it is extCLI's own directory on the phone, exactly as before.
        """
        import os

        shell_paths = getattr(self, "shell_paths", None)
        if shell_paths is not None and shell_paths.active:
            home = shell_paths.home()
            try:
                os.makedirs(shell_paths.host(home), exist_ok=True)
            except Exception as e:
                log.error("console: cannot prepare %s" % home, e)
            return _with_terminal(Env(cwd=home, home=home, paths=shell_paths))
        try:
            home = paths.home_dir()
            os.makedirs(home, exist_ok=True)
        except Exception as e:
            log.error("console: cannot prepare the home directory", e)
            home = "/"
        return _with_terminal(Env(cwd=home, home=home))

    def build_terminal(self):
        """Creates the terminal widget, preferring whichever renderer is set.

        The dex renderer is faster and is what the TUI mode will need, but a
        console that might come up blank is worse than a slower one that works,
        so the view-based terminal is the default until the fast path is proven
        on a device. A failure here falls back rather than propagating.
        """
        self.terminal = None
        wanted = prefs.renderer()
        if wanted == "fast":
            try:
                self.terminal = bridge.Terminal(
                    self.activity,
                    bridge.int_array(self.palette.as_array()),
                    text_size_sp=float(prefs.text_size()),
                )
                self.renderer_kind = "dex"
            except Exception as e:
                log.error("console: the fast renderer failed, using views", e)
        if self.terminal is None:
            self.terminal = textview.TextViewTerminal(
                self.activity, self.palette,
                text_size_sp=float(prefs.text_size()),
                scrollback=self.scrollback,
            )
            self.renderer_kind = "views"
        log.log("console: renderer=%s" % self.renderer_kind)

        # the view is not laid out yet, so metrics are a guess until refresh_width
        cols = self.terminal.metrics()[0] or 40
        style_class = styles.get(prefs.style_name())
        self.style = style_class(self.palette, cols)
        return self.terminal.view

    def refresh_width(self):
        """Re-reads the real column count once the view has been measured.

        Block formatting depends on the width, and before layout the renderer
        reports zero columns; without this every table would be laid out for a
        40-column guess.
        """
        if self.terminal is None or self.style is None:
            return
        try:
            cols = self.terminal.metrics()[0]
        except Exception as e:
            log.log("console: metrics unavailable: %s" % e, debug=True)
            return
        if cols and cols != self.style.width:
            self.style.width = cols
            log.log("console: width %d columns" % cols, debug=True)

    def make_context(self, origin="console"):
        """The console's Context.

        Kept, not rebuilt: the shell session lives in it, so a new one per
        command would forget every variable, alias and `cd`.
        """
        cols = self.style.width if self.style else 40
        if self.context is None:
            self.context = Context(
                services=self.services,
                env=self.shell_env,
                registry=self.registry,
                width=cols,
                origin=origin,
                backend=self.backend,
            )
            self.context.run_script_text = self._run_script_text
            # the same list the up arrow walks, so `history clear` clears both
            self.context.history = self.history
            # output as it happens rather than at the end
            self.context.live = self._live_result
            self.context.live_text = self._live_text
            self.context.progress = self._progress_card
            # and input as it happens: what is typed while a program runs
            # belongs to that program, not to a new prompt
            self.context.attach_input = self._attach_input
        self.context.width = cols
        self.context.screen = self._screen_size()
        if self.style is not None:
            self.style.width = cols
        self.context.origin = origin
        self.context.assume_yes = (origin != "console")
        return self.context

    def _screen_size(self):
        """(columns, lines) as the screen really is.

        The terminal's own measurement rather than the style's width: a program
        given a width it does not have wraps its own lines and then the console
        wraps them again, which is what fastfetch's logo came out as.
        """
        try:
            if self.terminal is not None:
                cols, rows, _cw, _ch = self.terminal.metrics()
                if int(cols) > 0 and int(rows) > 0:
                    return (int(cols), int(rows))
        except Exception:
            pass
        return (self.style.width if self.style else 0, 0)

    def update_status(self):
        """The one line that says whether the terminal is actually alive."""
        if self.status_view is None:
            return
        try:
            version = self.services.host.plugin_version() or "?"
        except Exception:
            version = "?"
        size = "no terminal"
        try:
            if self.terminal is not None:
                cols, rows, _cell_w, _cell_h = self.terminal.metrics()
                size = "%dx%d" % (cols, rows)
                if not cols or not rows:
                    # the interesting case: the widget exists but measured to
                    # nothing, which is exactly what an empty screen looks like
                    size = self.terminal.describe()
        except Exception as e:
            size = "no metrics (%s)" % type(e).__name__
            log.error("console: metrics failed", e)
        backends = "none"
        try:
            names = [b.name for b in self.backend.backends if b.available()]
            backends = ", ".join(names) or "none"
        except Exception:
            pass
        try:
            self.status_view.setText("extCLI %s  %s  %s  %s"
                                     % (version, size, self.renderer_kind or "?",
                                        backends))
        except Exception as e:
            log.error("console: cannot update the status line", e)

    def _run_script_text(self, text):
        """Runs a script in this same session; what `source` calls."""
        return dispatch.run_line(text, self.make_context())

    # -------------------------------------------------------------- transcript

    def emit(self, text=""):
        """The one way a line reaches the screen.

        It goes into the transcript first and the terminal second, so output
        that arrives while the console is closed is not lost — it is waiting
        there when the screen comes back.
        """
        self.transcript.append(text)
        if len(self.transcript) > self.scrollback:
            del self.transcript[:-self.scrollback]
        if self.terminal is not None:
            try:
                self.terminal.write_line(text)
            except Exception as e:
                log.error("console: cannot write to the terminal", e)

    def emit_lines(self, lines):
        for line in lines:
            self.emit(line)

    def replay(self):
        """Fills a freshly built terminal with everything said so far."""
        if self.terminal is None:
            return
        try:
            self.terminal.clear()
            if self._busy and self._screen.alt:
                # a program is drawing a screen of its own. None of the
                # scrollback belongs on it, and nobody but the program knows
                # what it looks like — so the new terminal is put back on the
                # alternate screen and the program is asked to draw it again.
                self.terminal.append("\x1b[?1049h")
                self.refresh_input_line()
                self._ask_for_a_redraw()
                return
            lines = self.lines()
            if lines:
                self.terminal.write_lines(lines)
            self.refresh_input_line()
            self.terminal.scroll_to_bottom()
        except Exception as e:
            log.error("console: cannot replay the transcript", e)

    def _ask_for_a_redraw(self):
        """Gets a full-screen program to draw itself again.

        There is no "please redraw" a terminal can send that every program
        understands, but there is one every one of them already listens for:
        the screen changing size. So it is told a size one row short and then
        the real one, and what comes back is a whole screen — which is the only
        way a console that has just been rebuilt can find out what was on it.
        """
        channel = self._input_channel
        resize = getattr(channel, "resize", None) if channel else None
        if resize is None:
            return
        cols, rows = self._screen_size()
        if not cols or not rows:
            return
        try:
            resize(int(cols), max(int(rows) - 1, 1))
            resize(int(cols), int(rows))
        except Exception as e:
            log.error("console: cannot ask for a redraw", e)

    def wipe(self):
        """`clear`: the transcript goes too, or it would come back on reopen."""
        self.transcript = []
        if self.terminal is not None:
            self.terminal.clear()

    def greet(self):
        """The welcome text, written once per session.

        The guard lives here rather than in the caller: a session that has been
        away and come back must not say hello over its own scrollback, and
        start() is not the only thing that could ask.
        """
        from ..render.styles import base

        if self._started:
            return
        self._started = True
        version = "?"
        try:
            version = self.services.host.plugin_version() or "?"
        except Exception:
            pass
        accent = self.palette.role("accent")
        fg = self.palette.role("fg")
        dim = self.palette.role("dim")

        self.emit("%s %s" % (base.colored("extCLI", accent),
                             base.colored(version, dim)))
        self.emit()
        self.emit("%s %s" % (base.colored("help", fg),
                             base.colored("the command list", dim)))
        self.emit("%s %s" % (base.colored("exit", fg),
                             base.colored("end this session", dim)))
        waiting = self._setup_pending()
        if waiting:
            # a console opened during the first setup has no rootfs behind it
            # yet, and `apk` answering "not found" needs a reason
            self.emit()
            self.emit(base.colored("preparing the rootfs (%s) — guest "
                                   "commands work once it is done"
                                   % ", ".join(waiting), dim))
        self.emit()
        log.log("console: greeted", debug=True)

    def _setup_pending(self):
        """What the first-run setup has still to do, if anything."""
        try:
            from ..rootfs import setup as rootfs_setup

            if not prefs.auto_setup():
                return []
            return rootfs_setup.pending(paths.res_dir(), paths.state_dir(),
                                        paths.rootfs_dir(),
                                        prefs.tool_profiles())
        except Exception:
            return []

    # ------------------------------------------------------------------ input

    @property
    def echoes_input(self):
        """True when the terminal draws the typed line, so the field can hide."""
        return bool(getattr(self.terminal, "echoes_input", False))

    def focus_input(self):
        """Puts the caret in the field and brings the keyboard up.

        The field is one pixel tall and transparent, which is enough to hold
        focus but not enough for the system to offer the keyboard on its own —
        so tapping the terminal has to ask for it explicitly.
        """
        field = self.input_view
        if field is None:
            return
        try:
            field.setFocusable(True)
            field.setFocusableInTouchMode(True)
            field.requestFocus()
        except Exception as e:
            log.error("console: cannot focus the input", e)
            return
        try:
            from org.telegram.messenger import AndroidUtilities

            AndroidUtilities.showKeyboard(field)
            return
        except Exception as e:
            log.log("console: showKeyboard unavailable: %s" % e, debug=True)
        try:
            from android.content import Context
            from android.view.inputmethod import InputMethodManager

            manager = self.activity.getSystemService(Context.INPUT_METHOD_SERVICE)
            manager.showSoftInput(field, InputMethodManager.SHOW_IMPLICIT)
        except Exception as e:
            log.error("console: cannot show the keyboard", e)

    def _selection(self):
        try:
            return int(self.input_view.getSelectionStart())
        except Exception:
            return len(self._last_input)

    def _set_field(self, text, position=None):
        """Writes the field without the watcher treating it as typing."""
        field = self.input_view
        self._last_input = text
        if field is None:
            return
        self._suppress = True
        try:
            field.setText(text)
            try:
                field.setSelection(len(text) if position is None
                                   else max(0, min(position, len(text))))
            except Exception:
                pass
        finally:
            self._suppress = False
        self.refresh_input_line()

    def on_input_changed(self, text):
        """Every keystroke: either a control combination, or a redraw."""
        if self._suppress:
            return
        text = str(text)
        if self._ctrl_armed and len(text) > len(self._last_input):
            # CTRL is latched, so the character just typed is the combination;
            # take it back out of the field and act on it instead
            position = self._selection()
            char = text[position - 1:position]
            self.arm_ctrl(False)
            self._set_field(self._last_input)
            self.on_ctrl(char)
            return
        if self._program_channel() is not None:
            self._type_difference(text)
            return
        self._last_input = text
        self.refresh_input_line()

    def refresh_input_line(self):
        """Redraws the prompt and the line being typed inside the terminal."""
        if self.terminal is None or self.style is None or not self.echoes_input:
            return
        from ..render.styles import base

        if self._busy:
            # A shell shows no prompt while a command runs. Drawing one right
            # after the echo meant the output arrived *between* the command and
            # its own prompt, which reads as the console answering out of order.
            #
            # The cursor stays, though. It sits on the line under whatever has
            # been printed so far and moves down with it, which is where a
            # terminal keeps it — an empty input line made it vanish instead.
            try:
                self.terminal.set_input_line("", cursor=0)
            except Exception as e:
                # drawing is not what submitting a command depends on
                log.error("console: cannot blank the input line", e)
            return
        if self.selecting():
            # a redraw would drop the highlight out from under the user
            return

        text = self._last_input
        position = max(0, min(self._selection(), len(text)))
        prompt = self.style.colored_prompt(self.shell_env.display_cwd())
        try:
            # the cursor is a column the terminal paints, not a character in
            # the line — otherwise copying a line would bring it along
            self.terminal.set_input_line(
                prompt + base.colored(text, self.palette.role("fg")),
                cursor=base.visible_length(prompt) + position,
            )
        except Exception as e:
            log.error("console: cannot draw the input line", e)

    def move_caret(self, delta=None, absolute=None):
        if self.input_view is None:
            return
        text = self._last_input
        position = absolute if absolute is not None else self._selection() + delta
        position = max(0, min(position, len(text)))
        try:
            self.input_view.setSelection(position)
        except Exception:
            pass
        self.refresh_input_line()

    def arm_ctrl(self, armed):
        self._ctrl_armed = bool(armed)
        try:
            self.set_ctrl_indicator(self._ctrl_armed)
        except Exception:
            pass

    def on_ctrl(self, char):
        """The combinations a shell user reaches for without thinking."""
        key = (char or "").lower()
        if self._program_channel() is not None and len(key) == 1:
            code = ord(key.upper()) - 64
            if 0 < code < 32 and self.type_raw(chr(code)):
                # ^C belongs to the program too — nano asks "save?" with it —
                # but a program that has stopped listening would leave the
                # console with no way out, so the third one is ours
                if key == "c":
                    self._raw_interrupts += 1
                    if self._raw_interrupts >= 3:
                        # counted apart from the escalation below, and asking
                        # again rather than killing: three presses mean the
                        # program is not listening, not that it must die
                        self._raw_interrupts = self._interrupts = 0
                        self.interrupt()
                return
        if self._busy and self._input_channel is not None and key in "cd":
            if key == "c":
                if self.interrupt():
                    return
            # ^D is how you tell a program its input has ended, and on a
            # terminal there is nothing else that would
            elif self._input_channel("\x04"):
                self._live_text("^D\n")
                return
        if key == "c":
            self.emit(self.style.echo(self._last_input + "^C",
                                      self.shell_env.display_cwd()))
            self._set_field("")
        elif key == "l":
            self.wipe()
            self.refresh_input_line()
        elif key == "u":
            self._set_field("")
        elif key == "a":
            self.move_caret(absolute=0)
        elif key == "e":
            self.move_caret(absolute=len(self._last_input))
        elif key == "k":
            self._set_field(self._last_input[:self._selection()])
        elif key == "d":
            if not self._last_input:
                self.submit("exit")
        elif key == "w":
            head = self._last_input[:self._selection()].rstrip()
            cut = head.rfind(" ") + 1
            self._set_field(head[:cut] + self._last_input[self._selection():], cut)

    # -------------------------------------------------------------- execution

    def on_terminal_resized(self, cols, rows):
        """The screen is a different size now — usually the keyboard.

        A program that draws a screen measured it once, when it started, and
        has had no way of hearing about it since. Telling the pty makes the
        kernel signal it, which is how every one of them already learns that a
        window has been dragged about.
        """
        try:
            if self.context is not None:
                self.context.screen = (int(cols), int(rows))
            if self.style is not None:
                self.style.width = int(cols)
            # the transcript's own screen measures the same screen
            self._screen.resize(self._screen.height, rows=int(rows))
        except Exception:
            pass
        channel = self._input_channel
        resize = getattr(channel, "resize", None) if channel else None
        if resize is None:
            return
        try:
            resize(int(cols), int(rows))
        except Exception as e:
            log.error("console: cannot resize the running program", e)

    def _attach_input(self, channel):
        """A running program's terminal, or None when it has ended."""
        self._input_channel = channel
        self._interrupts = 0
        self._raw_interrupts = 0

    def _program_channel(self):
        """The running program's terminal, if it is reading keys not lines.

        A program that draws a screen — an editor, a pager, anything with
        curses under it — turns the line discipline off and expects every key
        as it is pressed. Everything else expects lines, and still gets them at
        Enter. The pty is asked rather than the program guessed at: it is the
        same flag `stty -icanon` clears, and it becomes true the moment nano
        starts and false again the moment it leaves.
        """
        channel = self._input_channel
        if not self._busy or channel is None:
            return None
        try:
            return channel if channel.raw() else None
        except Exception:
            return None

    def type_raw(self, text):
        """Hands keys straight to the program, with nothing added."""
        channel = self._program_channel()
        if channel is None or not text:
            return False
        try:
            sent = bool(channel(text))
        except Exception:
            return False
        self._typed_raw = self._typed_raw or sent
        return sent

    def _type_difference(self, text):
        """Sends what changed in the field, and leaves the field alone.

        Emptying it after every key looked tidier and lost them: a soft
        keyboard edits a region of its own while a word is being typed, and
        clearing that region from under it drops whatever it had not committed
        yet — which is what typing quickly is made of. So the field is left as
        it is and only the difference goes to the program, worked out from the
        front. An autocorrection is the same shape of thing: a few characters
        taken back, a few put in.

        Nothing of it is ever seen — the field is one pixel tall and holds no
        prompt — so it is emptied only when it has grown long enough to be
        worth tidying, and at the end of the program either way.
        """
        old, new = self._last_input, str(text)
        common = 0
        while (common < len(old) and common < len(new)
               and old[common] == new[common]):
            common += 1
        self._last_input = new
        keys = "\x7f" * (len(old) - common) + new[common:]
        if keys:
            self.type_raw(keys)
        if len(new) >= RAW_FIELD_LIMIT:
            self._set_field("")

    def _forget_raw_field(self):
        """After a program that took the keyboard: what is in the field was
        for it, not for the shell, and must not turn up on the next prompt."""
        if self._typed_raw:
            self._typed_raw = False
            if self._last_input:
                self._set_field("")

    def interrupt(self):
        """^C. Says whether there was anything to stop.

        Twice means it: the first asks, and a program that ignores SIGINT — or
        one wedged where it cannot answer — would otherwise leave the console
        with nothing to do but wait out the timeout. The second is not
        refusable.
        """
        channel = self._input_channel
        if channel is None or not hasattr(channel, "interrupt"):
            return False
        self._interrupts += 1
        if self._interrupts == 1:
            if not channel.interrupt():
                return False
            self._live_text("^C\n")
            return True
        if not channel.stop():
            return False
        self._live_text("^C\n")
        return True

    def _type_at_program(self, text):
        """Hands a line to the program that is running, as a terminal does.

        Pressing send while something runs used to print a new prompt, which no
        terminal does — what is typed then belongs to the program, and an empty
        line is a blank line for it, not a prompt for us. The console echoes it
        because the program's terminal has echo turned off.
        """
        channel = self._input_channel
        if channel is None:
            return False
        if not channel(text + "\n"):
            return False
        self._live_text(text + "\n")
        return True

    def submit(self, line):
        """Echoes a line and runs it off the UI thread."""
        if self._program_channel() is not None:
            # Enter is a key like any other in an editor, and what is in the
            # field is whatever the keyboard has not handed over yet
            self.type_raw((line or "") + "\r")
            self._set_field("")
            return
        text = (line or "").strip()
        if self.style is None:
            return
        cwd = self.shell_env.display_cwd()
        # The line goes onto the screen before the field is cleared, always.
        #
        # Clearing the field redraws the terminal there and then, while output
        # is drawn on a timer a frame or two later — so clearing first left one
        # frame with the typed line gone from the field and not yet echoed
        # above it, and the prompt blinked out of existence on every command.
        if self._busy:
            if self._type_at_program(text):
                self._set_field("")
                self._scroll_to_bottom()
                return
            if not text:
                # nothing to type at and nothing to run: a blank line, which is
                # what a terminal gives you while it is busy
                self.emit()
                self._set_field("")
                self._scroll_to_bottom()
                return
            self.emit(self.style.echo(text, cwd))
            self._set_field("")
            self._write_result_now(blocks.error(
                "still running the previous command"))
            return
        if not text:
            self.emit(self.style.echo("", cwd))
            self._set_field("")
            self._scroll_to_bottom()
            return

        # busy first, so the redraw below leaves the prompt off the screen
        # until the command has finished and _finish puts it back
        self._busy = True
        self._streamed = False
        self._screen = _new_screen()
        self.emit(self.style.echo(text, cwd))
        self._set_field("")
        self._remember(text)

        ctx = self.make_context()

        def work():
            result = dispatch.run_line(text, ctx)
            self._post(lambda: self._finish(ctx, result))

        try:
            from client_utils import run_on_queue

            run_on_queue(work)
        except Exception:
            # no client queue: run inline rather than losing the command
            result = dispatch.run_line(text, ctx)
            self._finish(ctx, result)

    def _finish(self, ctx, result):
        self._busy = False
        self._forget_raw_field()
        # whatever it was writing into has been closed by now
        self._input_channel = None
        # the Context outlives the command, so its one-shot flags have to be
        # taken and cleared, or `clear` would wipe the screen forever after
        clear_requested = ctx.clear_requested
        exit_requested = ctx.exit_requested
        ctx.clear_requested = False
        ctx.exit_requested = False
        try:
            if clear_requested:
                self.wipe()
                return
            if self._streamed:
                self._settle_output()
            if not self._write_result_now(result) and self._streamed:
                # the output has already gone by, but the blank line that
                # separates it from the next prompt has not
                self.emit()
            if exit_requested:
                self.close(end_session=True)
        except Exception as e:
            log.error("console: rendering result failed", e)
        finally:
            self.refresh_input_line()

    def _write_result_now(self, result, spacer=True):
        """Renders one result. Says whether it had anything to show."""
        lines = self.style.render(result)
        if lines:
            self.emit_lines(lines)
            if spacer:
                self.emit()
        # a reader who has scrolled up to read something is not brought back
        # by output arriving; a command they just typed does bring them back
        self._scroll_to_bottom(only_if_following=spacer is False)
        return bool(lines)

    def _scroll_to_bottom(self, only_if_following=False):
        if self.terminal is not None:
            try:
                self.terminal.scroll_to_bottom(only_if_following)
            except TypeError:
                # a renderer that does not know about following
                self.terminal.scroll_to_bottom()

    def _progress_card(self, title=None):
        """A card outside the console, for something that takes minutes.

        The console shows the work as it happens, but a console has to be
        looked at. Anything long enough to walk away from puts one of these up
        as well, and it is the same card the first-run setup uses.
        """
        try:
            from . import progress as progress_module

            card = progress_module.SetupBulletin()
            if title:
                card.title = title
            return card if card.show() else None
        except Exception as e:
            log.error("console: cannot show a progress card", e)
            return None

    def _live_text(self, text):
        """A program's own output, as it wrote it.

        Straight to the terminal: it is terminal text already, with its own
        colours, its own wrapping to the width we told it about, and its own
        carriage returns. The transcript is kept in step by hand, because the
        terminal is not always there — output can arrive while the console is
        closed, and it has to be waiting when the screen comes back.
        """
        def show():
            self._streamed = True
            self._record(text)
            if self.terminal is not None:
                try:
                    self.terminal.append(text)
                except Exception as e:
                    log.error("console: cannot draw output", e)

        self._post(show)

    def lines(self):
        """Everything the screen is showing: what has scrolled past, and what a
        program can still go back and rewrite.

        The two are kept apart because only the first can never change again.
        Anything that shows the transcript — a screen coming back, a copy —
        wants both, or a progress bar in flight would be missing from it.
        """
        live = [line for line in self._screen.lines if line]
        return list(self.transcript) + live

    def _record(self, text):
        """The transcript, kept by the rules the terminal is drawing by.

        The same model, not a second reading of it: what a reopened screen
        replays should be what the screen showed, and three lines a program
        redrew forty times should be three lines in both.
        """
        for line in self._screen.write(str(text)):
            self._keep(line)

    def _keep(self, line):
        self.transcript.append(line)
        if len(self.transcript) > self.scrollback:
            del self.transcript[:-self.scrollback]

    def _settle_output(self):
        """Ends the last line and takes back the blank ones after it."""
        from ..render.styles import base

        for line in self._screen.finish():
            if line:
                self._keep(line)
        while self.transcript and base.is_blank(self.transcript[-1]):
            self.transcript.pop()
        if self.terminal is not None:
            try:
                self.terminal.trim_trailing_blanks()
            except Exception:
                pass

    def _live_result(self, result):
        """One piece of output, the moment there is one.

        Called from whichever thread the command is running on, which is not
        the one that may touch views — so it goes back through the UI thread,
        in order, and lands in the same place the finished result would have.
        """
        def show():
            # no blank line between the pieces of one command's output: the
            # spacer belongs before the next prompt, and _finish adds it there
            self._streamed = True
            self._write_result_now(result, spacer=False)

        self._post(show)

    def _post(self, fn):
        try:
            from android_utils import run_on_ui_thread

            run_on_ui_thread(fn)
        except Exception:
            fn()

    # ------------------------------------------------------------- lifecycle

    def start(self, initial_command=None):
        """Everything that has to happen once the screen exists.

        Called from the fragment's callback and again from a posted runnable
        after presentFragment, because not every client version invokes
        afterCreateView — and when it does not, the console came up empty with
        no prompt and no keyboard, which is exactly how this looked. Hence the
        once-per-screen guard: asking twice has to be free, or the initial
        command would run twice.
        """
        if self._screen_ready:
            return
        self._screen_ready = True
        if self._started:
            self.replay()
        else:
            try:
                self.greet()
            except Exception as e:
                log.error("console: greeting failed", e)
            self.run_rc()
            self.refresh_input_line()
        after_layout(self, initial_command)

    def run_rc(self):
        """Reads ~/.extclirc, the way a shell reads its rc.

        Everything set in a console died with it: the aliases, the exports, the
        functions — so anybody who used this twice set them up twice. The file
        is read once per session rather than once per screen, because coming
        back from the back gesture is not a new shell.

        It is sourced rather than executed, so what it defines lands in this
        shell and not in a child that exits immediately after.
        """
        import os

        env = self.shell_env
        path = os.path.join(env.home or "", RC_FILE)
        try:
            if not os.path.isfile(env.host(path)):
                return
            with open(env.host(path), "r", encoding="utf-8") as handle:
                text = handle.read()
        except Exception as e:
            log.error("console: cannot read %s" % RC_FILE, e)
            return
        try:
            result = dispatch.run_line(text, self.make_context(origin="script"))
        except Exception as e:
            log.error("console: %s failed" % RC_FILE, e)
            return
        # only when it has something to say: a working rc file should be
        # silent, and printing "ok" above the first prompt is noise forever
        if result is not None and not result.ok:
            self._write_result_now(result)

    def detach(self):
        """The screen is gone; the session is not.

        The views belong to a window that has been dismissed, so they are
        dropped — but the transcript, the shell environment and any running
        command are untouched, and a later open replays into a new terminal.
        """
        self._screen_ready = False
        terminal = self.terminal
        self.terminal = None
        self.input_view = None
        self.status_view = None
        self.window = None
        self.window_root = None
        self.set_ctrl_indicator = lambda armed: None
        if terminal is not None:
            try:
                terminal.release()
            except Exception as e:
                log.log("console: release failed: %s" % e, debug=True)

    def close(self, end_session=False):
        """Takes the screen down. Only `exit` also ends the session."""
        if end_session:
            end_live_session()
        try:
            from org.telegram.messenger import AndroidUtilities

            if self.input_view is not None:
                AndroidUtilities.hideKeyboard(self.input_view)
        except Exception:
            pass
        # both surfaces are dialogs, and dismissing one calls detach() through
        # its own listener — so there is nothing else to unwind here
        if self.window is None:
            return
        try:
            self.window.dismiss()
        except Exception as e:
            log.error("console: cannot dismiss the console window", e)

    # ---------------------------------------------------------------- history

    def _history_path(self):
        import os

        return os.path.join(paths.state_dir(), HISTORY_FILE)

    def _load_history(self):
        try:
            with open(self._history_path(), "r", encoding="utf-8") as f:
                return [line.rstrip("\n") for line in f][-HISTORY_LIMIT:]
        except Exception:
            return []

    def _remember(self, text):
        if self.history and self.history[-1] == text:
            self._history_index = len(self.history)
            return
        self.history.append(text)
        del self.history[:-HISTORY_LIMIT]
        self._history_index = len(self.history)
        try:
            import os

            os.makedirs(paths.state_dir(), exist_ok=True)
            with open(self._history_path(), "w", encoding="utf-8") as f:
                f.write("\n".join(self.history))
        except Exception as e:
            log.log("console: cannot save history: %s" % e, debug=True)

    def history_step(self, delta):
        if not self.history or self.input_view is None:
            return
        index = self._history_index + delta
        index = max(0, min(index, len(self.history)))
        self._history_index = index
        text = "" if index >= len(self.history) else self.history[index]
        self._set_field(text)

    # ------------------------------------------------------------- completion

    def complete(self):
        if self.input_view is None:
            return
        text = self._last_input
        trailing_space = text.endswith(" ") or not text
        words = text.split()
        candidates = self.registry.complete(self.make_context(), words,
                                            trailing_space)
        if not candidates:
            return
        if len(candidates) == 1:
            self._apply_completion(words, trailing_space, candidates[0])
            return
        shared = _common_prefix(candidates)
        last = "" if trailing_space else (words[-1] if words else "")
        if shared and len(shared) > len(last):
            self._apply_completion(words, trailing_space, shared)
            return
        self._write_result_now(blocks.Result([blocks.Text("  ".join(candidates))]))

    def _apply_completion(self, words, trailing_space, completion):
        if trailing_space:
            new_words = words + [completion]
        else:
            new_words = words[:-1] + [completion]
        self._set_field(" ".join(new_words))

    # ---------------------------------------------------------------- softkey

    def on_softkey(self, action):
        if self.selecting():
            self.on_terminal_tap()
        if action in RAW_KEYS and self._program_channel() is not None:
            # the row is a keyboard while a program owns the screen: ESC is an
            # escape, the arrows move its cursor and not our history
            self.type_raw(RAW_KEYS[action])
            return
        if action == "complete":
            self.complete()
        elif action == "history_prev":
            self.history_step(-1)
        elif action == "history_next":
            self.history_step(1)
        elif action in ("clear", "cancel"):
            self._set_field("")
        elif action == "home":
            self.move_caret(absolute=0)
        elif action == "end":
            self.move_caret(absolute=len(self._last_input))
        elif action == "left":
            self.move_caret(delta=-1)
        elif action == "right":
            self.move_caret(delta=1)
        elif action == "page_up":
            self.scroll_page(-1)
        elif action == "page_down":
            self.scroll_page(1)
        elif action == "ctrl":
            self.arm_ctrl(not self._ctrl_armed)
        elif action == "alt":
            # here for the layout; nothing in the shell reads meta keys yet
            pass
        elif action.startswith("insert:"):
            self._insert(action.split(":", 1)[1])

    # -------------------------------------------------------- selecting text

    # The terminal is a selectable TextView, so selection is the platform's
    # own: long press, drag the handles, copy, carry on typing. The catch is
    # that a selectable TextView is focusable in touch mode and takes focus
    # from the input — so the console hands it straight back, except while a
    # selection is actually happening. No mode to be stuck in either: a tap and
    # any soft key both put focus on the input unconditionally.

    def selecting(self):
        terminal = self.terminal
        if terminal is None:
            return False
        if self._selection_open:
            return True
        has_selection = getattr(terminal, "has_selection", None)
        return bool(has_selection()) if callable(has_selection) else False

    def on_terminal_long_press(self):
        """Lets the press through to the view, which knows how to select."""
        self._selection_open = True
        return False

    def on_terminal_focus(self, has_focus):
        if has_focus and not self.selecting():
            self.focus_input()

    def on_selection_started(self):
        self._selection_open = True

    def on_selection_ended(self):
        self._selection_open = False
        self.refresh_input_line()
        self.focus_input()

    def on_terminal_tap(self):
        # unconditional, so a tap is always a way back to typing
        self._selection_open = False
        clear = getattr(self.terminal, "clear_selection", None)
        if callable(clear):
            clear()
        self.focus_input()

    def copy_transcript(self):
        """The whole scrollback on the clipboard; what `copy` runs."""
        from ..render.styles import base

        text = "\n".join(base.strip_codes(line) for line in self.lines())
        try:
            from android.content import ClipData, Context

            clipboard = self.activity.getSystemService(Context.CLIPBOARD_SERVICE)
            clipboard.setPrimaryClip(ClipData.newPlainText("extCLI", text))
        except Exception as e:
            log.error("console: cannot copy the scrollback", e)
            raise
        return len(self.transcript)

    def text_size(self):
        return prefs.text_size()

    # ---------------------------------------------------------- diagnostics

    def describe_window(self):
        """What `host check --window` prints: the window as it actually turned out.

        Two rounds of fixing the strip behind the navigation bar were guesses
        made without being able to see the result. This makes the device
        answer: if the decor is as tall as the display, the window covers the
        bars and the fault is elsewhere.
        """
        from . import window as window_module

        rows = window_module.describe(self.window, self.window_root)
        rows.append(("renderer", self.renderer_kind or "none"))
        try:
            if self.terminal is not None:
                rows.append(("terminal", self.terminal.describe()))
        except Exception as e:
            rows.append(("terminal", "unreadable: %s" % e))
        rows.append(("surface", prefs.console_surface()))
        rows.append(("theme", prefs.theme_name()))
        return rows

    def scroll_page(self, direction):
        view = getattr(self.terminal, "view", None)
        if view is None:
            return
        try:
            view.smoothScrollBy(0, int(view.getHeight() * 0.8) * direction)
        except Exception as e:
            log.log("console: cannot scroll: %s" % e, debug=True)

    def _insert(self, text):
        if self.input_view is None:
            return
        current = self._last_input
        position = max(0, min(self._selection(), len(current)))
        self._set_field(current[:position] + text + current[position:],
                        position + len(text))


def _common_prefix(values):
    if not values:
        return ""
    shortest = min(values, key=len)
    for i, char in enumerate(shortest):
        for value in values:
            if value[i] != char:
                return shortest[:i]
    return shortest


def error_view(activity, palette, title, detail):
    """A screen that explains itself.

    Returning None from beforeCreateView leaves the fragment showing an empty
    list, which is indistinguishable from a broken terminal. Whatever went
    wrong, the user should be able to read it and send it back.
    """
    from android.util import TypedValue
    from android.widget import FrameLayout, ScrollView, TextView

    from ..compat import fonts

    root = FrameLayout(activity)
    root.setBackgroundColor(palette.role("bg"))
    view = TextView(activity)
    view.setText("%s\n\n%s" % (title, detail))
    view.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 12)
    view.setTextColor(palette.role("error"))
    view.setTextIsSelectable(True)
    view.setPadding(24, 24, 24, 24)
    typeface = fonts.mono_typeface()
    if typeface is not None:
        view.setTypeface(typeface)
    scroll = ScrollView(activity)
    scroll.addView(view)
    root.addView(scroll, FrameLayout.LayoutParams(-1, -1))
    return root


def build_view(session):
    """The console layout: a terminal, an invisible input, and the key rows.

    Modelled on Termux, deliberately. There is no visible text field — typing
    goes into a transparent EditText and is echoed by the terminal at the
    prompt, so the screen is nothing but terminal and keys, and the caret sits
    where the text does.
    """
    from android.text import InputType
    from android.util import TypedValue
    from android.view.inputmethod import EditorInfo
    from android.widget import EditText, FrameLayout, LinearLayout, TextView
    from android_utils import OnClickListener
    from org.telegram.messenger import AndroidUtilities

    from ..compat import proxies

    activity = session.activity
    palette = session.palette
    dp = AndroidUtilities.dp

    root = FrameLayout(activity)
    root.setBackgroundColor(palette.role("bg"))

    column = LinearLayout(activity)
    column.setOrientation(LinearLayout.VERTICAL)
    # no padding for the status bar: the console lives in its own window, which
    # is already laid out below the system bars

    terminal_params = LinearLayout.LayoutParams(-1, 0)
    terminal_params.weight = 1.0
    try:
        terminal_view = session.build_terminal()
        column.addView(terminal_view, terminal_params)
    except Exception as e:
        # the renderer lives in a dex; if it will not load, the rest of the
        # console still works and should say what happened rather than show
        # an empty black area
        log.error("console: terminal renderer unavailable", e)
        notice = TextView(activity)
        notice.setText("terminal renderer unavailable\n%s: %s\n%s"
                       % (type(e).__name__, e, bridge.load_error() or ""))
        notice.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 11)
        notice.setTextColor(palette.role("error"))
        notice.setPadding(dp(14), dp(14), dp(14), dp(14))
        notice.setTextIsSelectable(True)
        column.addView(notice, terminal_params)

    # ------------------------------------------------------------------ input
    # With a renderer that can draw the typed line, the field is invisible and
    # only holds focus; with one that cannot, it has to be readable instead.
    echoes = session.echoes_input
    field = EditText(activity)
    field.setBackground(None)
    field.setSingleLine(True)
    field.setImeOptions(EditorInfo.IME_ACTION_DONE
                        | EditorInfo.IME_FLAG_NO_FULLSCREEN
                        | EditorInfo.IME_FLAG_NO_EXTRACT_UI)
    field.setInputType(
        InputType.TYPE_CLASS_TEXT
        | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
    )
    field.setFocusable(True)
    field.setFocusableInTouchMode(True)
    if echoes:
        field.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 1)
        field.setTextColor(0)
        field.setPadding(0, 0, 0, 0)
        try:
            field.setCursorVisible(False)
        except Exception:
            pass
    else:
        field.setTextSize(TypedValue.COMPLEX_UNIT_DIP, float(prefs.text_size()))
        field.setTextColor(palette.role("fg"))
        field.setHint("$")
        field.setHintTextColor(palette.role("dim"))
        field.setPadding(dp(12), dp(6), dp(12), dp(6))
        typeface = fonts.mono_typeface()
        if typeface is not None:
            field.setTypeface(typeface)
    session.input_view = field

    field.addTextChangedListener(
        proxies.text_watcher(session.on_input_changed))
    field.setOnEditorActionListener(proxies.editor_action_listener(
        lambda view: session.submit(str(view.getText()))))
    field.setOnKeyListener(proxies.key_listener(_raw_key(session)))
    column.addView(field, LinearLayout.LayoutParams(-1, dp(1) if echoes else -2))

    # a half-typed line survives a trip out of the console
    if session._last_input:
        session._set_field(session._last_input)

    # tapping anywhere in the terminal brings the keyboard back; long-press
    # copies, which is what the removed text selection used to be for
    try:
        listener = OnClickListener(lambda view: session.on_terminal_tap())
        terminal_view.setOnClickListener(listener)
        attach_tap = getattr(session.terminal, "set_on_tap", None)
        if attach_tap is not None:
            attach_tap(listener)
        attach_long = getattr(session.terminal, "set_on_long_press", None)
        if attach_long is not None:
            attach_long(_long_press_listener(session.on_terminal_long_press))
        watch_focus = getattr(session.terminal, "set_focus_watcher", None)
        if callable(watch_focus):
            watch_focus(session.on_terminal_focus)
        watch_selection = getattr(session.terminal, "set_selection_watcher", None)
        if callable(watch_selection):
            watch_selection(session.on_selection_started,
                            session.on_selection_ended)
        watch_size = getattr(session.terminal, "set_size_watcher", None)
        if callable(watch_size):
            watch_size(session.on_terminal_resized)
    except Exception as e:
        log.log("console: terminal is not clickable: %s" % e, debug=True)

    # -------------------------------------------------------------- soft keys
    try:
        keys = softkeys.build(activity, palette, session)
        column.addView(keys, LinearLayout.LayoutParams(-1, -2))
    except Exception as e:
        log.error("console: soft keys unavailable", e)

    # ------------------------------------------------------------ status line
    # Termux has no status line; ours only appears when debug logs are on,
    # where it is the fastest way to see the grid size and the backends
    if prefs.debug_logs():
        status = TextView(activity)
        status.setTextSize(TypedValue.COMPLEX_UNIT_DIP, 10)
        status.setTextColor(palette.role("dim"))
        status.setPadding(dp(10), 0, dp(10), dp(4))
        typeface = fonts.mono_typeface()
        if typeface is not None:
            status.setTypeface(typeface)
        session.status_view = status
        session.update_status()
        column.addView(status, LinearLayout.LayoutParams(-1, -2))

    root.addView(column, FrameLayout.LayoutParams(-1, -1))
    return root


# Keys the input field would otherwise swallow while a program owns them.
# Backspace is the one that matters: in raw mode the field is emptied after
# every keystroke, so there is never anything in it to delete and no text
# change is reported — the key event is the only sign it was pressed. The rest
# are here for a real keyboard: arrows, escape and tab never reach a text
# watcher either.
RAW_CODES = {
    67: "\x7f",        # DEL
    66: "\r",          # ENTER
    61: "\t",          # TAB
    111: "\x1b",       # ESCAPE
    19: "\x1b[A", 20: "\x1b[B", 22: "\x1b[C", 21: "\x1b[D",
}


def _raw_key(session):
    """Sends a key straight to a running program, or lets the field have it."""

    def pressed(code, event):
        if event is not None and event.getAction() != 0:   # ACTION_DOWN
            return False
        if session._program_channel() is None:
            return False
        sequence = RAW_CODES.get(code)
        if sequence is None:
            return False
        return bool(session.type_raw(sequence))

    return pressed


def _long_press_listener(function):
    # False lets the press fall through to the view's own handler, which is
    # how the system's text selection ever gets to run
    from ..compat import proxies

    return proxies.long_click_listener(lambda view: function())


def after_layout(session, initial_command):
    """Waits for the first layout pass, then takes the real width, opens the
    keyboard and runs any command the caller passed in."""

    def apply():
        session.refresh_width()
        session.refresh_input_line()
        session.focus_input()
        if initial_command:
            session.submit(initial_command)

    try:
        from android_utils import run_on_ui_thread

        run_on_ui_thread(apply, 250)
    except Exception:
        apply()


def resume_or_create(plugin, activity):
    """The session for this screen: the one still running, or a new one.

    Going back does not end a session, so opening the console again has to find
    it — with its scrollback, its shell environment and whatever it is still
    running — rather than start from an empty screen.
    """
    global _live

    if _live is not None:
        _live.activity = activity
        # the theme may have changed while the console was away; everything
        # built from the palette is about to be rebuilt anyway
        _live.palette = current_palette()
        log.log("console: resuming the running session (%d lines)"
                % len(_live.transcript), debug=True)
        return _live
    _live = ConsoleSession(plugin, activity)
    return _live


def self_test(activity=None):
    """Builds a console off-screen, measures it, and reports what happened.

    The console had been coming up empty with nothing in the logs, and asking
    for a screenshot each round is slow. This runs the whole construction path
    without a fragment or a sheet, forces a measure/layout pass, and returns
    lines for the diagnostics report — so one dialog says whether the renderer
    produces a grid, whether text reaches the buffer, and how big the view is.
    """
    lines = ["console self-test"]
    step = "finding an activity"
    try:
        if activity is None:
            from client_utils import get_last_fragment

            fragment = get_last_fragment()
            activity = fragment.getParentActivity() if fragment else None
        if activity is None:
            return lines + ["  no activity available"]

        step = "creating the session"
        session = ConsoleSession(None, activity)
        lines.append("  palette   bg=%08x fg=%08x" % (
            session.palette.role("bg") & 0xFFFFFFFF,
            session.palette.role("fg") & 0xFFFFFFFF))

        step = "building the view"
        root = build_view(session)
        lines.append("  view      %s" % _describe_view(root))

        step = "measuring"
        width, height = _measure(root)
        lines.append("  measured  %dx%d px" % (width, height))

        step = "writing to the terminal"
        session.greet()
        session.submit("echo self-test")

        if session.terminal is None:
            lines.append("  terminal  not created")
            return lines
        lines.append("  renderer  %s (%s)" % (session.terminal.describe(),
                                              session.renderer_kind))
        lines.append("  metrics   %s" % (session.terminal.metrics(),))
        buffered = session.terminal.text().strip().split("\n")
        lines.append("  buffer    %d lines" % len(buffered))
        for text in buffered[:3]:
            lines.append("    | %s" % text[:60])
    except Exception as e:
        from ..utils import log as log_module

        lines.append("  failed while %s: %s: %s" % (step, type(e).__name__, e))
        lines.extend("    %s" % line for line in log_module.traceback_lines()[-6:])
    return lines


def _describe_view(view):
    try:
        name = str(view.getClass().getSimpleName())
        children = view.getChildCount() if hasattr(view, "getChildCount") else 0
        return "%s with %d children" % (name, children)
    except Exception as e:
        return "unreadable (%s)" % e


def _measure(root):
    """Forces a layout pass so the terminal reports real numbers."""
    from android.view import View as AndroidView

    width, height = 1080, 1920
    try:
        from org.telegram.messenger import AndroidUtilities

        size = AndroidUtilities.displaySize
        width, height = int(size.x) or width, int(size.y) or height
    except Exception:
        pass
    spec_w = AndroidView.MeasureSpec.makeMeasureSpec(width,
                                                     AndroidView.MeasureSpec.EXACTLY)
    spec_h = AndroidView.MeasureSpec.makeMeasureSpec(height,
                                                     AndroidView.MeasureSpec.EXACTLY)
    root.measure(spec_w, spec_h)
    root.layout(0, 0, root.getMeasuredWidth(), root.getMeasuredHeight())
    return int(root.getMeasuredWidth()), int(root.getMeasuredHeight())
