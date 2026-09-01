# SPDX-License-Identifier: Apache-2.0

"""Command tests.

Commands reach the client only through the services on their Context, so fakes
are enough to exercise them here — no device, no Android imports.
"""

import os

import pytest

from extcli_src import policy as policy_module
from extcli_src.render import palette, plain
from extcli_src.render.styles.classic import ClassicStyle
from extcli_src.shell import dispatch
from extcli_src.shell.builtins import build_registry
from extcli_src.shell.context import Context, Services


class FakePluginInfo(object):
    def __init__(self, plugin_id, name, version, enabled, path=None):
        self.id = plugin_id
        self.name = name
        self.version = version
        self.enabled = enabled
        self.author = "someone"
        self.path = path or "/data/plugins/%s" % plugin_id
        self.description = None
        self.pinned = False

    @property
    def state(self):
        return "on" if self.enabled else "off"

    def as_fields(self):
        return [("id", self.id), ("name", self.name), ("version", self.version),
                ("state", "enabled" if self.enabled else "disabled"),
                ("path", self.path)]


class FakePlugins(object):
    def __init__(self):
        self._plugins = [
            FakePluginInfo("shareui_packit", "PackIt", "0.0.0-rc.652", True),
            FakePluginInfo("extcli", "extCLI", "0.1.0", True),
            FakePluginInfo("dev_night", "NightMode", "2.1.0", False),
        ]
        self.settings = {"dev_night": {"theme": "dark", "level": 3}}
        self.calls = []

    def list_plugins(self):
        return list(self._plugins)

    def get(self, plugin_id):
        for p in self._plugins:
            if p.id == plugin_id:
                return p
        return None

    def find(self, query):
        needle = query.lower()
        return [p for p in self._plugins
                if needle in p.id.lower() or needle in p.name.lower()]

    def set_enabled(self, plugin_id, enabled):
        self.calls.append(("set_enabled", plugin_id, enabled))
        target = self.get(plugin_id)
        target.enabled = enabled
        return True, "%s %s" % (plugin_id, "enabled" if enabled else "disabled")

    def reload(self, plugin_id):
        self.calls.append(("reload", plugin_id))
        return True, "%s reloaded" % plugin_id

    def get_settings(self, plugin_id):
        return dict(self.settings.get(plugin_id, {}))

    def set_setting(self, plugin_id, key, value):
        self.calls.append(("set_setting", plugin_id, key, value))
        self.settings.setdefault(plugin_id, {})[key] = value
        return True, "%s.%s = %s" % (plugin_id, key, value)

    def clear_settings(self, plugin_id):
        self.calls.append(("clear_settings", plugin_id))
        self.settings.pop(plugin_id, None)
        return True, "settings of %s cleared" % plugin_id


class FakeHost(object):
    def describe(self):
        return [("plugin", "0.1.0"), ("client", "12.9.0"), ("sdk", "1.4.5.0")]

    def plugin_version(self):
        return "0.1.0"


class FakePaths(object):
    def describe(self):
        return [("files", "/data/files", True), ("home", "/data/extcli/home", False)]


class FakeProbe(object):
    def __init__(self):
        self.forced = 0

    def result(self, force=False):
        if force:
            self.forced += 1
        return {
            "checks": {
                "shell": {"status": "ok", "detail": "/system/bin/sh"},
                "data_exec": {"status": "blocked", "detail": "permission denied"},
            },
            "backends": ["system", "inproc"],
        }

    def rootfs_verdict(self, result):
        return "not available: execve blocked and no fallback found"

    def extra_checks(self):
        return [("renderer", True, "renderer v1")]


class FakeLog(object):
    def __init__(self):
        self.rows = [
            (1000.0, "I", "loaded in 0.2s"),
            (1001.0, "E", "probe failed: boom"),
            (1002.0, "D", "theme: fallback for accent"),
        ]
        self.cleared = False

    def tail(self, count=40, level=None):
        rows = self.rows if level is None else [r for r in self.rows if r[1] == level]
        return rows[-count:]

    def clear(self):
        self.cleared = True


def make_ctx(**overrides):
    services = Services(
        host=FakeHost(),
        plugins=FakePlugins(),
        paths=FakePaths(),
        probe=FakeProbe(),
        log=FakeLog(),
        policy=policy_module,
    )
    for key, value in overrides.items():
        setattr(services, key, value)
    registry = build_registry()
    return Context(services=services, registry=registry, width=40)


def run(ctx, line):
    return dispatch.run_line(line, ctx)


def rendered(ctx, line):
    style = ClassicStyle(palette.from_client({
        "bg": 0xFF000000, "fg": 0xFFFFFFFF, "dim": 0xFF888888,
        "accent": 0xFF4EA1F3, "error": 0xFFFF0000, "success": 0xFF00FF00,
        "warn": 0xFFFFAA00, "selection": 0xFF222222, "divider": 0xFF333333,
    }), ctx.width)
    import re

    lines = style.render(run(ctx, line))
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


# ------------------------------------------------------------------ dispatch

def test_blank_and_comment_lines_do_nothing():
    ctx = make_ctx()
    assert run(ctx, "").blocks == []
    assert run(ctx, "   ").blocks == []
    assert run(ctx, "# just a note").blocks == []


def test_unknown_command_suggests_a_real_one():
    ctx = make_ctx()
    result = run(ctx, "plugni list")
    assert result.code == 127
    error = result.blocks[0]
    assert "command not found" in error.message
    assert "plugin" in (error.hint or "")


def test_unbalanced_quote_is_a_clean_syntax_error():
    # a real shell rejects this; the point is that it reports rather than raises
    ctx = make_ctx()
    result = run(ctx, 'echo "unterminated')
    assert result.code == 2
    assert "syntax error" in result.blocks[0].message
    assert "unterminated" in result.blocks[0].message


def test_command_exception_becomes_an_error_block():
    class Exploding(object):
        def list_plugins(self):
            raise RuntimeError("kaboom")

    ctx = make_ctx(plugins=Exploding())
    result = run(ctx, "plugin list")
    assert result.code == 1
    assert "kaboom" in result.blocks[0].message


def test_missing_service_is_reported_not_crashed():
    ctx = make_ctx(plugins=None)
    result = run(ctx, "plugin list")
    assert "not available here" in result.blocks[0].message


# ---------------------------------------------------------------------- help

def test_help_lists_registered_commands():
    lines = rendered(make_ctx(), "help")
    text = "\n".join(lines)
    for name in ("host", "plugin", "log", "help", "clear", "exit"):
        assert name in text


def test_help_for_a_group_shows_subcommands():
    text = "\n".join(rendered(make_ctx(), "help plugin"))
    assert "list" in text and "enable" in text


def test_help_for_a_subcommand_shows_usage():
    text = "\n".join(rendered(make_ctx(), "help plugin config"))
    assert "config" in text


def test_help_for_unknown_command_suggests():
    result = run(make_ctx(), "help plugni")
    assert "no help for" in result.blocks[0].message


# -------------------------------------------------------------------- plugin

def test_plugin_list_marks_states_and_counts():
    lines = rendered(make_ctx(), "plugin list")
    text = "\n".join(lines)
    assert "PackIt" in text
    assert "[on]" in text and "[off]" in text
    assert "3 plugins, 2 enabled, 1 off" in text


def test_plugin_list_filters():
    text = "\n".join(rendered(make_ctx(), "plugin list --disabled"))
    assert "NightMode" in text
    assert "PackIt" not in text


def test_plugin_list_rejects_unknown_flag():
    result = run(make_ctx(), "plugin list --nope")
    assert "unknown option" in result.blocks[0].message


def test_plugin_info_by_partial_name():
    text = "\n".join(rendered(make_ctx(), "plugin info night"))
    assert "dev_night" in text
    assert "disabled" in text


def test_plugin_info_ambiguous_query_explains():
    result = run(make_ctx(), "plugin info e")
    assert "matches" in result.blocks[0].message
    assert "be specific" in result.blocks[0].hint


def test_plugin_info_unknown_suggests():
    result = run(make_ctx(), "plugin info packt")
    assert "no such plugin" in result.blocks[0].message


def test_plugin_enable_calls_the_client():
    ctx = make_ctx()
    result = run(ctx, "plugin enable dev_night")
    assert result.ok
    assert ("set_enabled", "dev_night", True) in ctx.services.plugins.calls


def test_plugin_disable_calls_the_client():
    ctx = make_ctx()
    run(ctx, "plugin disable shareui_packit")
    assert ("set_enabled", "shareui_packit", False) in ctx.services.plugins.calls


def test_plugin_reload():
    ctx = make_ctx()
    assert run(ctx, "plugin reload extcli").ok
    assert ("reload", "extcli") in ctx.services.plugins.calls


def test_plugin_path():
    assert "/data/plugins/extcli" in "\n".join(rendered(make_ctx(), "plugin path extcli"))


def test_plugin_state_change_goes_through_policy():
    seen = []
    original_check = policy_module.check

    def spy(action, detail="", assume_yes=False):
        seen.append(action)
        return original_check(action, detail, assume_yes)

    policy_module.check = spy
    try:
        run(make_ctx(), "plugin disable dev_night")
    finally:
        policy_module.check = original_check
    assert policy_module.PLUGIN_STATE in seen


def test_plugin_config_list_and_get():
    ctx = make_ctx()
    text = "\n".join(rendered(ctx, "plugin config list dev_night"))
    assert "theme" in text and "dark" in text
    assert "theme = dark" in "\n".join(rendered(ctx, "plugin config get dev_night theme"))


def test_plugin_config_get_unknown_key_suggests():
    result = run(make_ctx(), "plugin config get dev_night thme")
    assert "has no setting" in result.blocks[0].message
    assert "theme" in (result.blocks[0].hint or "")


def test_plugin_config_set_parses_types():
    ctx = make_ctx()
    run(ctx, "plugin config set dev_night enabled true")
    run(ctx, "plugin config set dev_night level 7")
    run(ctx, "plugin config set dev_night label hello world")
    stored = ctx.services.plugins.settings["dev_night"]
    assert stored["enabled"] is True
    assert stored["level"] == 7
    assert stored["label"] == "hello world"


def test_plugin_config_unset_clears():
    ctx = make_ctx()
    run(ctx, "plugin config unset dev_night")
    assert "dev_night" not in ctx.services.plugins.settings


def test_plugin_group_without_subcommand_lists_them():
    text = "\n".join(rendered(make_ctx(), "plugin"))
    assert "list" in text and "subcommands" in text


def test_plugin_unknown_subcommand_suggests():
    result = run(make_ctx(), "plugin lst")
    assert "unknown subcommand" in result.blocks[0].message
    assert "list" in (result.blocks[0].hint or "")


# ---------------------------------------------------------------------- host

def test_host_status():
    assert "12.9.0" in "\n".join(rendered(make_ctx(), "host status"))


def test_host_paths_marks_missing():
    text = "\n".join(rendered(make_ctx(), "host paths"))
    assert "missing" in text


def test_host_check_reports_backends_and_rootfs():
    text = "\n".join(rendered(make_ctx(), "host check"))
    assert "system, inproc" in text
    assert "not available" in text
    assert "renderer" in text


def test_host_check_refresh_forces_a_new_probe():
    ctx = make_ctx()
    run(ctx, "host check --refresh")
    assert ctx.services.probe.forced == 1


def test_host_version():
    assert "extCLI 0.1.0" in "\n".join(rendered(make_ctx(), "host version"))


def test_host_class_needs_an_argument():
    result = run(make_ctx(), "host class")
    assert "needs a class name" in result.blocks[0].message


# ----------------------------------------------------------------------- log

def test_log_tail_shows_lines():
    text = "\n".join(rendered(make_ctx(), "log tail"))
    assert "loaded in 0.2s" in text
    assert "3 lines" in text


def test_log_tail_errors_only():
    text = "\n".join(rendered(make_ctx(), "log tail --errors"))
    assert "probe failed" in text
    assert "loaded in 0.2s" not in text


def test_log_tail_rejects_bad_count():
    assert "positive count" in run(make_ctx(), "log tail -n 0").blocks[0].message
    assert "needs a number" in run(make_ctx(), "log tail -n abc").blocks[0].message


def test_log_grep():
    text = "\n".join(rendered(make_ctx(), "log grep probe"))
    assert "probe failed" in text
    assert "1 matching lines" in text


def test_log_grep_without_match():
    assert "no log lines match" in "\n".join(rendered(make_ctx(), "log grep zzz"))


def test_log_clear():
    ctx = make_ctx()
    run(ctx, "log clear")
    assert ctx.services.log.cleared


# --------------------------------------------------------------- session/tab

def test_exit_and_clear_set_flags():
    ctx = make_ctx()
    run(ctx, "exit")
    assert ctx.exit_requested
    ctx = make_ctx()
    run(ctx, "clear")
    assert ctx.clear_requested


def test_completion_of_command_names():
    ctx = make_ctx()
    assert "plugin" in ctx.registry.complete(ctx, ["plug"], False)


def test_completion_of_subcommands():
    ctx = make_ctx()
    assert "list" in ctx.registry.complete(ctx, ["plugin", ""], False)
    assert ctx.registry.complete(ctx, ["plugin", "ena"], False) == ["enable"]


def test_completion_of_plugin_ids():
    ctx = make_ctx()
    assert "dev_night" in ctx.registry.complete(ctx, ["plugin", "info", "dev"], False)


def test_completion_after_trailing_space_moves_to_arguments():
    ctx = make_ctx()
    assert "list" in ctx.registry.complete(ctx, ["plugin"], True)


# ------------------------------------------------ found by the command sweep

def test_plugin_config_with_only_an_id_lists_settings():
    """`plugin config <id>` used to answer "unknown subcommand: config <id>",
    naming the plugin the user had just typed correctly."""
    ctx = make_ctx()
    text = "\n".join(rendered(ctx, "plugin config shareui_packit"))
    assert "unknown subcommand" not in text
    assert "shareui_packit" in text


def test_plugin_config_still_takes_an_explicit_subcommand():
    ctx = make_ctx()
    assert "shareui_packit" in "\n".join(
        rendered(ctx, "plugin config list shareui_packit"))
    assert run(ctx, "plugin config get shareui_packit nothing").code != 0


def test_a_group_without_a_default_still_reports_unknown_subcommands():
    ctx = make_ctx()
    result = run(ctx, "plugin nonsense")
    assert result.code != 0
    assert "unknown subcommand" in result.blocks[0].message


def test_source_of_a_missing_file_reads_like_a_shell():
    ctx = make_ctx()
    result = run(ctx, "source /nonexistent.sh")
    assert result.code != 0
    # not a raw OSError repr with an errno in it
    assert "no such file" in result.blocks[0].message
    assert "Errno" not in result.blocks[0].message


def test_source_of_a_directory_is_refused():
    ctx = make_ctx()
    assert run(ctx, "source /").code != 0


def test_history_lists_what_was_run():
    ctx = make_ctx()
    ctx.history = ["help", "plugin list", "send me hi"]
    text = "\n".join(rendered(ctx, "history"))
    assert "plugin list" in text and "3 of 3" in text


def test_history_takes_a_count():
    ctx = make_ctx()
    ctx.history = [str(i) for i in range(10)]
    text = "\n".join(rendered(ctx, "history 3"))
    assert "2 of 10" not in text and "3 of 10" in text


def test_history_clear_empties_the_console_list():
    ctx = make_ctx()
    ctx.history = ["one", "two"]
    assert run(ctx, "history clear").ok
    # the same list the up arrow walks, so it has to be emptied in place
    assert ctx.history == []
    assert "no history yet" in "\n".join(rendered(ctx, "history"))


def test_history_without_a_console_says_so():
    ctx = make_ctx()
    ctx.history = None
    result = run(ctx, "history")
    assert result.code != 0
    assert "not available here" in result.blocks[0].message


def test_history_rejects_a_word_where_a_count_goes():
    ctx = make_ctx()
    ctx.history = ["one"]
    assert run(ctx, "history sideways").code != 0


def test_the_window_report_needs_a_console():
    ctx = make_ctx()
    result = run(ctx, "host check --window")
    assert result.code != 0
    assert "not available here" in result.blocks[0].message


def test_the_window_report_says_what_the_console_says():
    class FakeConsole(object):
        def describe_window(self):
            return [("decor", "1080 x 2400"), ("display", "1080 x 2400")]

    ctx = make_ctx(terminal=FakeConsole())
    text = "\n".join(rendered(ctx, "host check --window"))
    assert "1080 x 2400" in text and "decor" in text


def test_clip_puts_the_scrollback_on_the_clipboard():
    class FakeConsole(object):
        def __init__(self):
            self.copied = 0

        def copy_transcript(self):
            self.copied += 1
            return 42

    console = FakeConsole()
    ctx = make_ctx(terminal=console)
    text = "\n".join(rendered(ctx, "clip"))
    assert console.copied == 1
    assert "42 lines" in text


def test_clip_without_a_console_says_so():
    ctx = make_ctx()
    result = run(ctx, "clip")
    assert result.code != 0
    assert "not available here" in result.blocks[0].message


def test_the_self_test_script_is_bundled_and_runnable():
    """`host check --self` reads a plain file; if it stops shipping, say so."""
    import os

    from extcli_src.shell.builtins.host import CheckCommand

    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "extcli", "res")
    path = os.path.join(root, CheckCommand.SCRIPT)
    assert os.path.isfile(path), "the self-test script is not in res/"
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    # it must not write anywhere but the one chat named at the top
    assert 'chat="@JettaXP"' in text
    sends = [line.strip() for line in text.splitlines()
             if line.strip().startswith("check tg send")]
    assert sends and all("$chat" in line for line in sends)


def test_self_test_needs_a_shell_to_run_in():
    ctx = make_ctx()
    ctx.run_script_text = None
    result = run(ctx, "host check --self")
    assert result.code != 0


# --------------------------------------------------- deleting what we wrote

class WipePaths(object):
    """Paths pointing at a temporary tree, the way the plugin's do."""

    def __init__(self, base):
        self.base = str(base)
        self.made = 0

    def files_dir(self):
        return self.base

    def storage_dir(self):
        return os.path.join(self.base, "sdcard")

    def plugin_root(self):
        return os.path.join(self.base, "plugins/extcli")

    def data_dir(self):
        return os.path.join(self.base, "extcli")

    def rootfs_dir(self):
        return os.path.join(self.data_dir(), "rootfs")

    def state_dir(self):
        return os.path.join(self.data_dir(), "state")

    def ensure_dirs(self):
        self.made += 1
        os.makedirs(self.data_dir(), exist_ok=True)
        return []

    def describe(self):
        return []


def _written(paths):
    os.makedirs(paths.rootfs_dir() + "/bin", exist_ok=True)
    os.makedirs(paths.state_dir(), exist_ok=True)
    with open(paths.rootfs_dir() + "/bin/busybox", "w") as handle:
        handle.write("elf")
    with open(paths.state_dir() + "/syscalls", "w") as handle:
        handle.write("146\n")


def test_removing_the_rootfs_leaves_the_measurements(tmp_path):
    """They describe the phone, not the container, and cost minutes."""
    paths = WipePaths(tmp_path)
    _written(paths)
    ctx = make_ctx(paths=paths)
    result = run(ctx, "rootfs remove")
    assert result.code == 0
    assert not os.path.exists(paths.rootfs_dir())
    assert os.path.isfile(paths.state_dir() + "/syscalls")


def test_removing_everything_is_asked_for_explicitly(tmp_path):
    """Removing the plugin does not take its data with it — the data lives
    outside the plugin's directory on purpose, so an update cannot throw away
    an Alpine somebody has spent an evening on."""
    paths = WipePaths(tmp_path)
    _written(paths)
    ctx = make_ctx(paths=paths)
    result = run(ctx, "rootfs remove --all")
    assert result.code == 0
    assert not os.path.exists(paths.rootfs_dir())
    assert not os.path.exists(paths.state_dir())
    # and the plugin can still write afterwards
    assert paths.made == 1


def test_deleting_nothing_says_so_rather_than_claiming_success(tmp_path):
    paths = WipePaths(tmp_path)
    ctx = make_ctx(paths=paths)
    assert "nothing to delete" in plain.text(run(ctx, "rootfs remove"))


def test_the_app_s_own_directory_is_never_deleted(tmp_path):
    """`data_dir` is built from the files directory. A bug that returned that
    directory instead would take the client's own data with it."""
    from extcli_src.utils import purge

    paths = WipePaths(tmp_path)
    _written(paths)
    result = purge.remove([paths.files_dir()],
                          keep=[paths.files_dir(), paths.storage_dir()])
    assert not result.ok
    assert os.path.isdir(paths.rootfs_dir())


# ------------------------------------------------- the forms a shell expects

def test_help_flag_answers_for_any_command():
    """`help <name>` always worked, and nobody arriving from a shell tries it
    before `--help`."""
    text = "\n".join(rendered(make_ctx(), "log tail --help"))
    assert "usage" in text and "log tail" in text
    # and the short form is the same command, as everywhere else
    assert "\n".join(rendered(make_ctx(), "log tail -h")) == text


def test_help_on_a_group_lists_its_subcommands():
    text = "\n".join(rendered(make_ctx(), "log --help"))
    assert "tail" in text and "grep" in text and "clear" in text


def test_help_goes_to_the_subcommand_that_was_named():
    """`plugin list --help` is the list's help, not the group's — the flags
    travel with the subcommand, however deep it is."""
    text = "\n".join(rendered(make_ctx(), "plugin config get --help"))
    assert "plugin config get" in text
    assert "enable" not in text, "that is the group's help, not the get's"


def test_a_file_can_still_be_called_dash_h():
    """After `--` the words are the command's own."""
    ctx = make_ctx()
    assert "-h" in "\n".join(rendered(ctx, "echo -- -h"))


# ----------------------------------------------------------- option parsing

def _spec():
    return {"--name": "str", "--count": "int", "--loud": "bool",
            "-l": "bool", "-a": "bool", "-n": "int"}


def test_long_option_takes_its_value_either_way():
    from extcli_src.shell.registry import parse_flags

    for line in (["--name", "x"], ["--name=x"]):
        assert parse_flags(line, _spec())["--name"] == "x"


def test_a_switch_given_a_value_is_refused():
    from extcli_src.shell.registry import CommandError, parse_flags

    with pytest.raises(CommandError) as caught:
        parse_flags(["--loud=1"], _spec())
    assert "takes no value" in str(caught.value)


def test_short_options_can_be_run_together():
    from extcli_src.shell.registry import parse_flags

    flags = parse_flags(["-la"], _spec())
    assert flags.has("-l") and flags.has("-a")


def test_a_value_can_be_stuck_to_its_short_option():
    from extcli_src.shell.registry import parse_flags

    assert parse_flags(["-n5"], _spec())["-n"] == 5
    assert parse_flags(["-ln5"], _spec())["-n"] == 5


def test_an_unknown_letter_blames_the_whole_word():
    """`-lz` is not "unknown option: -z" — nobody typed -z on its own."""
    from extcli_src.shell.registry import CommandError, parse_flags

    with pytest.raises(CommandError) as caught:
        parse_flags(["-lz"], _spec())
    assert "-lz" in str(caught.value)


def test_a_lone_dash_and_a_negative_number_are_arguments():
    from extcli_src.shell.registry import parse_flags

    assert parse_flags(["-", "-5"], _spec()).positional == ["-", "-5"]


def test_double_dash_ends_the_options():
    from extcli_src.shell.registry import parse_flags

    assert parse_flags(["--loud", "--", "--name"], _spec()).positional == ["--name"]


def test_the_program_answers_for_itself():
    text = "\n".join(rendered(make_ctx(), "extcli --version"))
    assert "extCLI" in text
