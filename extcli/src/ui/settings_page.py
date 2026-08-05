# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""The plugin's page inside exteraGram settings."""

from ..backends import probe
from ..compat import host, i18n, paths
from ..term import bridge
from ..utils import log
from . import console, dialogs, prefs

# option labels come from ui/prefs.py so the stored indices cannot drift


def _s(key, fallback):
    return i18n.get(key, fallback)


def _run_diagnostics(force=True):
    """Runs the probe off the UI thread, then shows the report."""
    from android_utils import run_on_ui_thread
    from client_utils import run_on_queue

    dialogs.toast(_s("diag_running", "Running diagnostics..."))

    def work():
        # each step is named, so a failure says which one broke instead of
        # leaving a bare exception with no context
        lines = []
        step = "start"
        try:
            step = "collecting device facts"
            facts = host.probe_facts()

            step = "resolving paths"
            tmp, state = paths.tmp_dir(), paths.state_dir()

            step = "probing backends"
            result = probe.get(tmp, state, facts, force=force)

            step = "checking the renderer"
            renderer_ok, renderer_detail = bridge.self_check()

            step = "formatting the report"
            lines = probe.summary_lines(
                result, extra_checks=[("renderer", renderer_ok, renderer_detail)]
            )
            lines.append("")
            lines.append("paths")
            for label, path, exists in paths.describe():
                lines.append("  %-9s %s%s" % (label, path,
                                              "" if exists else "  (missing)"))

            # building views has to happen on the UI thread, so the console
            # self-test is collected there and the dialog waits for it
            step = "testing the console"
            lines.append("")
            lines.extend(_console_self_test())
        except Exception as e:
            log.error("settings: diagnostics failed while %s" % step, e)
            report = ["diagnostics failed", "", "step:  %s" % step,
                      "error: %s: %s" % (type(e).__name__, e), ""]
            report.extend(log.traceback_lines())
            lines = report

        run_on_ui_thread(
            lambda: dialogs.show_text(_s("diag_title", "Diagnostics"), lines)
        )

    run_on_queue(work)


def _open_console(plugin):
    from .. import main

    return main.open_console(plugin)


def _console_self_test():
    """Runs console.self_test on the UI thread and waits for the answer."""
    import threading

    from android_utils import run_on_ui_thread

    result = []
    done = threading.Event()

    def work():
        try:
            result.extend(console.self_test())
        except Exception as e:
            result.append("console self-test failed: %s: %s" % (type(e).__name__, e))
        finally:
            done.set()

    run_on_ui_thread(work)
    if not done.wait(8.0):
        return ["console self-test: timed out on the UI thread"]
    return result


def _offer_tools():
    """The question, from the settings rather than after a setup."""
    from ..rootfs import packages, toolbox

    activity = _activity()
    if activity is None:
        dialogs.toast(_s("tools_no_screen", "Open this from the app"))
        return
    root = paths.rootfs_dir()
    installed = frozenset(
        name for group in packages.GROUPS for name in group.names
        if toolbox.present(root, name))
    # asked again after something has been removed by hand, this has to notice:
    # everything about what is on offer is worked out from the container, every
    # time, rather than remembered from the first answer
    selection = packages.selection_for(installed, toolbox.usable(root))
    from . import toolsheet

    toolsheet.offer(activity, selection, install_tools, installed=installed)


def _activity():
    try:
        from client_utils import get_last_fragment

        fragment = get_last_fragment()
        return fragment.getParentActivity() if fragment else None
    except Exception:
        return None


def install_tools(selection):
    """Fetches what was ticked — in the console, where it can be watched.

    Not in the background with only a card to show for it. This is minutes of
    downloading and unpacking, and all of it has something to say: apk names
    every package as it fetches it, and that is worth seeing. So the console
    opens with the command already running, exactly the command somebody could
    have typed, and the card goes up as well for whoever leaves.

    Leaving is fine. The session outlives the screen and the command runs on
    the client's queue, so the back gesture takes the window down and nothing
    else — the console has it all waiting when it is opened again.
    """
    words = selection.command_words()
    if not words:
        return
    command = "rootfs tools add " + " ".join(words)
    from .. import main
    from ..compat import store

    plugin = store.plugin()
    try:
        main.open_console(plugin, command)
        return
    except Exception as e:
        log.error("tools: cannot open the console for the install", e)
    dialogs.toast(_s("tools_open_console", "Open the console and run: %s")
                  % command)


def _linker():
    from ..backends import linker as linker_module
    from ..compat import host as compat_host

    return linker_module.find_linker(compat_host.abi())


def _delete(what):
    """Asks first, deletes off the UI thread, and says what went.

    Two of these: the container on its own, and everything extCLI has written.
    They are separate because they are different regrets — a container can be
    unpacked again in a minute, and the measurements behind it take longer than
    that and describe the phone rather than the container.
    """
    from ..utils import purge

    targets, title, question = _targets(what)
    sentence, files, _total = purge.describe(targets)
    if not files:
        dialogs.toast(_s("wipe_nothing", "There is nothing to delete"))
        return
    dialogs.confirm(
        title, "%s\n\n%s" % (question, sentence),
        lambda: _delete_now(what, targets),
        confirm_label=_s("wipe_confirm", "Delete"),
        cancel_label=_s("cancel_button", "Cancel"),
        destructive=True)


def _targets(what):
    """(paths, dialog title, what the dialog asks)."""
    if what == "everything":
        return ([paths.data_dir()],
                _s("wipe_all_label", "Delete all extCLI data"),
                _s("wipe_all_question",
                   "The Linux container, every measurement taken of this "
                   "device, and the shell history. The plugin's settings are "
                   "kept."))
    return ([paths.rootfs_dir()],
            _s("wipe_rootfs_label", "Delete the Linux container"),
            _s("wipe_rootfs_question",
               "Alpine and everything installed into it. It is unpacked "
               "again the next time the plugin loads."))


def _delete_now(what, targets):
    from client_utils import run_on_queue

    def work():
        from ..utils import purge

        try:
            keep = [paths.files_dir(), paths.storage_dir(), paths.plugin_root()]
        except Exception:
            keep = []
        result = purge.remove(targets, keep=keep)
        log.log("settings: %s (%s)" % (result.sentence(), what))
        try:
            # the tree the plugin writes into has to exist for it to keep
            # working, and a console that is open is holding a backend
            # pointing at a directory that has just gone
            paths.ensure_dirs()
            session = console.live_session()
            if session is not None:
                session.rebuild_backends()
        except Exception as e:
            log.error("settings: could not put the directories back", e)
        dialogs.toast(result.sentence(), error=not result.ok)

    try:
        run_on_queue(work)
    except Exception:
        work()


def _danger(item, **fields):
    """A row that deletes something, red if this SDK can make one red.

    Asked rather than assumed: an unknown keyword is a TypeError, and a
    settings page that fails to build is a worse outcome than a row in the
    ordinary colour.
    """
    try:
        return item(red=True, **fields)
    except TypeError:
        return item(**fields)


def _on_debug_logs_change(enabled):
    log.set_debug(bool(enabled))
    log.log("settings: debug logs %s" % ("on" if enabled else "off"))


def _on_entry_change(_enabled):
    dialogs.toast(_s("restart_needed", "Reload the plugin to apply"))


def _mount_change(setting):
    """The switch handler for one mount.

    A switch cannot be un-flipped from here — the page owns the widget — so a
    refusal puts the setting back and says why. The rule itself lives in
    `rootfs.mounts` and is the same one `config set` obeys.
    """
    def changed(enabled):
        from ..compat import store
        from ..rootfs import mounts

        values = prefs.mount_values()
        # the value has already been written, so the question is asked of the
        # state as it was before this switch moved
        values[mounts.mount(setting).key] = True
        refused = mounts.refusal(values, setting, bool(enabled))
        if refused:
            store.set(setting, True)
            dialogs.toast(refused)
            return
        dialogs.toast(_s("mounts_changed",
                         "Reopen the console to see the change"))

    return changed


def _items(kind):
    """One of the settings modules, imported where it is used.

    `ui.settings` is the client's, so it cannot be imported at the top of a
    module that the tests load without a device.
    """
    from ui import settings as ui_settings

    return getattr(ui_settings, kind)


def build(plugin):
    """The page itself: what is used often, and doors to the rest.

    Everything was one long list — appearance, entry points, four mounts,
    deleting things, debug — and a page you have to scroll past four
    switches you have never touched to reach the one you want is a page
    that has stopped being a settings page. What is left here is what
    somebody opens it for; the rest is a tap away and grouped by what it
    is about.
    """
    Divider = _items("Divider")
    Header = _items("Header")
    Selector = _items("Selector")
    Switch = _items("Switch")
    Text = _items("Text")

    version = host.plugin_version() or "0.1.0"

    return [
        Header(text=_s("settings_header", "extCLI")),
        Text(
            text=_s("open_console", "Open console"),
            subtext=_s("open_console_desc",
                       "The terminal; .cli also works in any chat"),
            icon="msg_photo_settings",
            on_click=lambda view: _open_console(plugin),
        ),
        Text(
            text=_s("diag_item", "Diagnostics"),
            subtext=_s("diag_item_desc",
                       "Check which execution backends this device allows"),
            icon="msg_info",
            on_click=lambda view: _run_diagnostics(force=True),
        ),
        Divider(),

        Header(text=_s("settings_look_header", "Appearance")),
        Selector(
            key="theme",
            text=_s("theme_item", "Theme"),
            default=prefs.DEFAULT_THEME_INDEX,
            items=[_s("theme_termux", "Terminal (black)"),
                   _s("theme_default", "Follow client"),
                   _s("theme_amoled", "Amoled")],
            icon="msg_colors",
        ),
        Selector(
            key="text_size_index",
            text=_s("text_size_item", "Text size"),
            default=prefs.DEFAULT_TEXT_SIZE_INDEX,
            items=[str(size) for size in prefs.TEXT_SIZES],
            icon="msg_text_size",
        ),
        Selector(
            key="console_surface",
            text=_s("surface_item", "Open console as"),
            default=prefs.DEFAULT_SURFACE_INDEX,
            items=[_s("surface_screen", "Full screen"),
                   _s("surface_sheet", "Sheet over the chat")],
            icon="msg_photo_settings",
        ),
        Divider(text=_s("appearance_note",
                        "The console follows your Telegram theme by default")),

        Header(text=_s("settings_rootfs_header", "Linux")),
        Switch(
            key="auto_setup",
            text=_s("auto_setup_label", "Set up Alpine automatically"),
            subtext=_s("auto_setup_desc",
                       "Unpack it and measure this device on first run, so "
                       "apk and the rest work straight away"),
            default=True,
            icon="msg_photo_settings",
        ),
        Text(
            text=_s("tools_item", "Tools in the container"),
            subtext=_s("tools_item_desc",
                       "git, python and the rest, fetched when you say so"),
            icon="msg_download",
            on_click=lambda view: _offer_tools(),
        ),
        _page(Text, _s("mounts_item", "What the shell can see"),
              _s("mounts_item_desc", "Which of the four paths a guest opens"),
              "msg_folders", _mount_items),
        _page(Text, _s("data_item", "Stored data"),
              _s("data_item_desc",
                 "The container, the measurements, and how to delete them"),
              "msg_download", _data_items),
        Divider(),

        _page(Text, _s("advanced_item", "Advanced"),
              _s("advanced_item_desc",
                 "Entry points, the terminal renderer, logging"),
              "msg_settings", _advanced_items),
        Divider(text="extCLI v%s · %s" % (version, prefs.style_name())),
    ]


def _page(item, text, subtext, icon, builder):
    """A row that opens a page of its own.

    `create_sub_fragment` is the client's; where it is not understood the row
    is still there and still says what it is for, it just cannot be opened —
    better than a page that fails to build at all.
    """
    try:
        return item(text=text, subtext=subtext, icon=icon,
                    create_sub_fragment=builder)
    except TypeError:
        return item(text=text, subtext=subtext, icon=icon)


def _mount_items():
    """The four paths a guest can see."""
    Divider = _items("Divider")
    Header = _items("Header")
    Switch = _items("Switch")

    from ..rootfs import mounts as mounts_module

    rows = [Header(text=_s("settings_mounts_header", "What the shell can see"))]
    for setting, label, description, icon in (
        ("mount_root", _s("mount_root_label", "Alpine"),
         _s("mount_root_desc", "The rootfs — where the programs live"),
         "msg_folders"),
        ("mount_sdcard", _s("mount_sdcard_label", "Storage"),
         _s("mount_sdcard_desc", "The phone's own files, under /sdcard"),
         "msg_download"),
        ("mount_extera", _s("mount_extera_label", "exteraGram"),
         _s("mount_extera_desc", "The client's files, under /exteraGram"),
         "msg_media"),
        ("mount_extcli", _s("mount_extcli_label", "extCLI"),
         _s("mount_extcli_desc", "This plugin's files, under /extCLI"),
         "msg_settings"),
        ("mount_patch", _s("mount_patch_label", "Patches"),
         _s("mount_patch_desc",
            "The client's own code, taken apart, under /patch"),
         "msg_edit"),
    ):
        item = mounts_module.mount(setting)
        rows.append(Switch(key=setting, text=label, subtext=description,
                           default=item.key in mounts_module.DEFAULT_ON,
                           icon=icon, on_change=_mount_change(setting)))
    rows.append(Divider(text=_s(
        "mounts_note",
        "One has to stay on. With Alpine off the shell opens in the next one "
        "and its programs still run.")))
    return rows


def _data_items():
    """What extCLI has written, and the two ways to be rid of it."""
    Divider = _items("Divider")
    Header = _items("Header")
    Text = _items("Text")

    return [
        Header(text=_s("data_header", "Stored data")),
        _danger(
            Text,
            text=_s("wipe_rootfs_label", "Delete the Linux container"),
            subtext=_s("wipe_rootfs_desc",
                       "Alpine and everything installed into it"),
            icon="msg_delete",
            on_click=lambda view: _delete("container"),
        ),
        _danger(
            Text,
            text=_s("wipe_all_label", "Delete all extCLI data"),
            subtext=_s("wipe_all_desc",
                       "Removing the plugin leaves this behind; this is how "
                       "it goes"),
            icon="msg_clear",
            on_click=lambda view: _delete("everything"),
        ),
        Divider(text=_s(
            "data_note",
            "It lives outside the plugin's own folder so that updating the "
            "plugin does not throw away a container you have set up.")),
    ]


def _advanced_items():
    """The settings somebody changes once, if ever."""
    Divider = _items("Divider")
    Header = _items("Header")
    Selector = _items("Selector")
    Switch = _items("Switch")

    return [
        Header(text=_s("settings_entries_header", "Where to show extCLI")),
        Switch(
            key="entry_drawer",
            text=_s("entry_drawer_label", "Side menu"),
            default=True,
            icon="msg_list",
            on_change=_on_entry_change,
        ),
        Switch(
            key="entry_chat",
            text=_s("entry_chat_label", "Chat menu"),
            default=True,
            icon="msg_message",
            on_change=_on_entry_change,
        ),
        Divider(),

        Header(text=_s("settings_terminal_header", "Terminal")),
        Selector(
            key="renderer",
            text=_s("renderer_item", "Terminal renderer"),
            default=prefs.DEFAULT_RENDERER_INDEX,
            items=[_s("renderer_views", "Compatible"),
                   _s("renderer_fast", "Fast (experimental)")],
            icon="msg_photo_settings",
        ),
        Divider(text=_s("renderer_note",
                        "The compatible one is drawn with ordinary views. "
                        "The fast one is newer and less proven.")),

        Header(text=_s("settings_debug_header", "Debug")),
        Switch(
            key="debug_logs",
            text=_s("debug_logs", "Debug logs"),
            subtext=_s("debug_logs_desc",
                       "Log routine activity, not just failures"),
            default=False,
            icon="msg_log",
            on_change=_on_debug_logs_change,
        ),
    ]
