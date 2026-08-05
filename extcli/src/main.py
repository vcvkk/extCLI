# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Plugin lifecycle.

BasePlugin.py holds only the class the loader needs; everything real happens
here so that file can stay uncompiled.
"""

import time

from .backends import probe
from .compat import host, menus, paths, store
from .ui import console, prefs, screen, settings_page, sheet
from .utils import log

CHAT_COMMAND = ".cli"

_started_at = None


def start_init(plugin):
    """Constructor-time work: cheap only. The client is still booting."""
    global _started_at
    _started_at = time.time()
    plugin.extcli_probe = None
    # settings are read through BasePlugin's own accessors, which need the
    # instance; do this first so anything below can read a setting
    store.bind(plugin)
    log.log("init", debug=True)


def load_plugin(plugin):
    try:
        created = paths.ensure_dirs()
        if created:
            log.log("created %d directories" % len(created), debug=True)
    except Exception as e:
        log.error("could not create data directories", e)

    try:
        host.log_environment()
    except Exception as e:
        log.error("could not read device info", e)

    _register_entry_points(plugin)

    try:
        plugin.add_on_send_message_hook()
    except Exception as e:
        log.error("could not register the %s command" % CHAT_COMMAND, e)

    _probe_in_background(plugin)

    took = time.time() - (_started_at or time.time())
    log.log("loaded in %.3fs" % took)


def _register_entry_points(plugin):
    """Menu entries that open the console.

    SDK 1.4.5.0 has an official menu API, so no hooks are involved: the drawer
    entry and the one in a chat's overflow menu are both MenuItemData.
    """
    if prefs.entry_enabled("drawer"):
        menus.add(
            plugin, "drawer", "extCLI",
            on_click=lambda *args: open_console(plugin),
            icon="msg_photo_settings",
            item_id="extcli_drawer",
        )
    if prefs.entry_enabled("chat"):
        menus.add(
            plugin, "chat", "extCLI",
            on_click=lambda *args: open_console(plugin),
            icon="msg_photo_settings",
            item_id="extcli_chat",
        )


def open_console(plugin, command=None):
    """Opens the console on the surface the user picked.

    Full screen by default. Either way it shows the session that is already
    running, if there is one — the surface is a window onto the session, not
    the session itself.
    """
    if prefs.console_surface() == "screen":
        return screen.open_screen(plugin, command)
    return sheet.open_sheet(plugin, command)


def _probe_in_background(plugin):
    """The probe spawns processes; never do that on the load path."""

    def work():
        try:
            result = probe.get(paths.tmp_dir(), paths.state_dir(), host.probe_facts())
            plugin.extcli_probe = result
            log.log("backends: %s" % ", ".join(result.get("backends", [])))
            log.log("rootfs: %s" % probe.rootfs_verdict(result), debug=True)
        except Exception as e:
            log.error("probe failed", e)
        # after the probe, on the same thread: it is the longer of the two and
        # there is no reason for two of them
        _prepare_rootfs(plugin)

    try:
        from client_utils import run_on_queue

        run_on_queue(work)
    except Exception:
        # no client queue (unit test / early failure): run inline
        work()


def _prepare_rootfs(plugin):
    """Unpacks and measures whatever is not ready yet.

    Everything this does was a command the user had to know to run — `rootfs
    syscalls`, `rootfs install alpine`, `rootfs launch`, `rootfs writes` — and
    somebody who has just installed a plugin has heard of none of them. It runs
    once: the second load finds nothing to do and says nothing.

    Never on the load path itself. Unpacking a rootfs is thousands of files and
    the syscall scan is a child process per number.
    """
    del plugin
    from .compat import network as compat_network
    from .rootfs import setup as rootfs_setup
    from .ui import progress

    bulletin = None
    try:
        if not prefs.auto_setup():
            return
        res, state = paths.res_dir(), paths.state_dir()
        root = paths.rootfs_dir()
        todo = rootfs_setup.pending(res, state, root)
        if not todo:
            _offer_the_tools()
            return
        log.log("rootfs: preparing (%s)" % ", ".join(todo))
        # something on screen: this is minutes of work that the user did not
        # ask for and cannot otherwise see, and a phone that is busy for no
        # visible reason is a phone that looks broken
        bulletin = progress.SetupBulletin()
        if not bulletin.show():
            bulletin = None
        report = rootfs_setup.prepare(
            res, state, root, abi=host.abi(),
            linker=_linker(), dns=compat_network.dns_servers(),
            on_step=lambda name, label: log.log("rootfs: %s" % label,
                                                debug=True),
            on_progress=None if bulletin is None else bulletin.update)
        for line in report.lines():
            log.log("rootfs: %s" % line, debug=True)
        failed = report.failure()
        if failed:
            log.log("rootfs: %s did not work — %s" % failed)
        else:
            log.log("rootfs: ready")
        if bulletin is not None:
            bulletin.finish(ok=not failed)
            bulletin = None
        # a console opened while this was running was built without a rootfs
        _refresh_console()
        if not failed:
            _offer_the_tools()
    except Exception as e:
        log.error("rootfs: could not prepare", e)
    finally:
        if bulletin is not None:
            # it never got to say anything; do not leave it on screen
            bulletin.close()


def _offer_the_tools():
    """Asks what to put in the container, once, when it is ready to hold it.

    After the setup rather than during it: what setup does is not optional and
    nobody is asked about it, and this is a question with a real answer of no.
    """
    from .rootfs import packages, toolbox
    from .ui import toolsheet

    try:
        if prefs.tools_offered():
            return
        root = paths.rootfs_dir()
        # what is already in there decides two things: what is not worth
        # offering, and what makes something else possible
        installed = frozenset(
            name for group in packages.GROUPS for name in group.names
            if toolbox.present(root, name))
        selection = packages.selection_for(installed, toolbox.usable(root))
        if not toolbox.anything_to_do(root, selection):
            prefs.remember_tools_offered()
            return

        def ask():
            activity = _activity()
            if activity is None:
                # no screen to ask on; it will be asked at the next load
                return
            shown = toolsheet.offer(
                activity, selection,
                lambda chosen: settings_page.install_tools(chosen),
                on_decline=prefs.remember_tools_offered,
                installed=installed)
            if shown:
                prefs.remember_tools_offered()

        _on_ui(ask)
    except Exception as e:
        log.error("tools: could not ask", e)


def _activity():
    try:
        from client_utils import get_last_fragment

        fragment = get_last_fragment()
        return fragment.getParentActivity() if fragment else None
    except Exception:
        return None


def _on_ui(function):
    try:
        from android_utils import run_on_ui_thread

        run_on_ui_thread(function)
    except Exception:
        function()


def _linker():
    from .backends import linker as linker_module

    return linker_module.find_linker(host.abi())


def _refresh_console():
    """Tells a console that is already open that the world has changed."""
    session = console.live_session()
    if session is not None:
        session.rebuild_backends()


def on_send_message_hook(plugin, account, params):
    """Intercepts `.cli` typed into a chat.

    Bare `.cli` opens the console. `.cli <command>` currently opens the console
    and runs the command there; sending the output back as a live-updated
    message is the next stage, and until it exists the message is not sent
    rather than silently going out as plain text.
    """
    from base_plugin import HookResult, HookStrategy

    try:
        message = params.message
    except Exception:
        return HookResult()
    if not isinstance(message, str):
        return HookResult()

    text = message.strip()
    if text != CHAT_COMMAND and not text.startswith(CHAT_COMMAND + " "):
        return HookResult()

    command = text[len(CHAT_COMMAND):].strip()
    _open_console_on_ui(plugin, command or None)
    return HookResult(strategy=HookStrategy.CANCEL)


def _open_console_on_ui(plugin, command):
    def show():
        try:
            open_console(plugin, command)
        except Exception as e:
            log.error("could not open the console", e)

    try:
        from android_utils import run_on_ui_thread

        run_on_ui_thread(show)
    except Exception:
        show()


def unload_plugin(plugin):
    try:
        menus.remove_all(plugin)
    except Exception as e:
        log.error("could not remove menu items", e)
    # a console session normally outlives its screen; it must not outlive the
    # plugin, or a reload would leave a session holding stale command objects
    try:
        session = console.end_live_session()
        if session is not None:
            session.detach()
    except Exception as e:
        log.error("could not end the console session", e)
    log.log("unloaded")


def create_settings(plugin):
    try:
        return settings_page.build(plugin)
    except Exception as e:
        log.error("settings page failed", e)
        return []
