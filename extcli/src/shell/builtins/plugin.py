# SPDX-License-Identifier: Apache-2.0

"""`plugin` — inspect and control installed plugins."""

from ...render import blocks
from ..registry import Command, CommandError, Group, parse_flags, suggest


def _resolve(ctx, query):
    """Finds exactly one plugin, or explains what to type instead."""
    plugins = ctx.require("plugins")
    found = plugins.get(query)
    if found is not None:
        return found
    matches = plugins.find(query)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = [p.id for p in plugins.list_plugins()]
        raise CommandError("no such plugin: %s" % query, hint=suggest(query, known))
    raise CommandError(
        "%r matches %d plugins" % (query, len(matches)),
        hint="be specific: " + ", ".join(p.id for p in matches[:4]),
    )


def _plugin_ids(ctx, prefix=""):
    if not ctx.has("plugins"):
        return []
    try:
        return sorted(p.id for p in ctx.services.plugins.list_plugins()
                      if p.id.startswith(prefix))
    except Exception:
        return []


class ListCommand(Command):
    name = "list"
    summary = "every installed plugin"
    usage = "plugin list [--enabled|--disabled]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"--enabled": "bool", "--disabled": "bool"})
        plugins = ctx.require("plugins")
        found = plugins.list_plugins()
        if flags.has("--enabled"):
            found = [p for p in found if p.enabled]
        elif flags.has("--disabled"):
            found = [p for p in found if p.enabled is False]

        if not found:
            return blocks.summary("no plugins installed")

        entries = [(p.name, p.version or "", p.state) for p in found]
        enabled = sum(1 for p in found if p.enabled)
        disabled = sum(1 for p in found if p.enabled is False)
        return blocks.Result([
            blocks.Items(entries),
            blocks.Summary("%d plugins, %d enabled, %d off"
                           % (len(found), enabled, disabled)),
        ])


class InfoCommand(Command):
    name = "info"
    summary = "details of one plugin"
    usage = "plugin info <id>"

    def run(self, ctx, args):
        if not args:
            raise CommandError("plugin info needs an id", hint=self.usage)
        return blocks.fields(_resolve(ctx, args[0]).as_fields())

    def complete(self, ctx, args):
        return _plugin_ids(ctx, args[-1] if args else "")


class _StateCommand(Command):
    enable = True

    def run(self, ctx, args):
        if not args:
            raise CommandError("%s needs a plugin id" % self.name, hint=self.usage)
        plugins = ctx.require("plugins")
        target = _resolve(ctx, args[0])

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.PLUGIN_STATE,
                           "%s %s" % (self.name, target.id),
                           assume_yes=ctx.assume_yes)

        ok, detail = plugins.set_enabled(target.id, self.enable)
        if not ok:
            raise CommandError(detail)
        return blocks.summary(detail, role=blocks.SUCCESS)

    def complete(self, ctx, args):
        return _plugin_ids(ctx, args[-1] if args else "")


class EnableCommand(_StateCommand):
    name = "enable"
    summary = "turn a plugin on"
    usage = "plugin enable <id>"
    mutating = True
    enable = True


class DisableCommand(_StateCommand):
    name = "disable"
    summary = "turn a plugin off"
    usage = "plugin disable <id>"
    mutating = True
    enable = False


class ReloadCommand(Command):
    name = "reload"
    summary = "unload and load again"
    usage = "plugin reload <id>"
    mutating = True

    def run(self, ctx, args):
        if not args:
            raise CommandError("plugin reload needs an id", hint=self.usage)
        plugins = ctx.require("plugins")
        target = _resolve(ctx, args[0])
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.PLUGIN_STATE, "reload %s" % target.id,
                           assume_yes=ctx.assume_yes)
        ok, detail = plugins.reload(target.id)
        if not ok:
            raise CommandError(detail)
        return blocks.summary(detail, role=blocks.SUCCESS)

    def complete(self, ctx, args):
        return _plugin_ids(ctx, args[-1] if args else "")


class PathCommand(Command):
    name = "path"
    summary = "where a plugin is installed"
    usage = "plugin path <id>"

    def run(self, ctx, args):
        if not args:
            raise CommandError("plugin path needs an id", hint=self.usage)
        target = _resolve(ctx, args[0])
        if not target.path:
            raise CommandError("client did not report a path for %s" % target.id)
        return blocks.text(target.path)

    def complete(self, ctx, args):
        return _plugin_ids(ctx, args[-1] if args else "")


class ConfigCommand(Group):
    """`plugin config` — read and write another plugin's settings."""

    # `plugin config <id>` reads as "show me that plugin's settings", so it
    # does that rather than reporting an unknown subcommand named after a
    # plugin the user just typed correctly
    default_subcommand = "list"

    def __init__(self):
        Group.__init__(self, "config", "read or write plugin settings", [
            _ConfigList(), _ConfigGet(), _ConfigSet(), _ConfigUnset(),
        ])


class _ConfigList(Command):
    name = "list"
    summary = "all settings of a plugin"
    usage = "plugin config list <id>"

    def run(self, ctx, args):
        if not args:
            raise CommandError("plugin config list needs an id", hint=self.usage)
        plugins = ctx.require("plugins")
        target = _resolve(ctx, args[0])
        values = plugins.get_settings(target.id)
        if not values:
            return blocks.summary("%s has no stored settings" % target.id)
        rows = [(key, _format_value(values[key])) for key in sorted(values)]
        return blocks.Result([
            blocks.Fields(rows, title=target.id),
            blocks.Summary("%d settings" % len(rows)),
        ])

    def complete(self, ctx, args):
        return _plugin_ids(ctx, args[-1] if args else "")


class _ConfigGet(Command):
    name = "get"
    summary = "one setting"
    usage = "plugin config get <id> <key>"

    def run(self, ctx, args):
        if len(args) < 2:
            raise CommandError("plugin config get needs an id and a key",
                               hint=self.usage)
        plugins = ctx.require("plugins")
        target = _resolve(ctx, args[0])
        values = plugins.get_settings(target.id)
        key = args[1]
        if key not in values:
            raise CommandError("%s has no setting %r" % (target.id, key),
                               hint=suggest(key, values.keys()))
        return blocks.text("%s = %s" % (key, _format_value(values[key])))


class _ConfigSet(Command):
    name = "set"
    summary = "write a setting"
    usage = "plugin config set <id> <key> <value>"
    mutating = True

    def run(self, ctx, args):
        if len(args) < 3:
            raise CommandError("plugin config set needs an id, a key and a value",
                               hint=self.usage)
        plugins = ctx.require("plugins")
        target = _resolve(ctx, args[0])
        key, raw = args[1], " ".join(args[2:])
        value = _parse_value(raw)

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.CLIENT_CONFIG,
                           "%s.%s = %s" % (target.id, key, value),
                           assume_yes=ctx.assume_yes)

        ok, detail = plugins.set_setting(target.id, key, value)
        if not ok:
            raise CommandError(detail)
        return blocks.summary(detail, role=blocks.SUCCESS)


class _ConfigUnset(Command):
    name = "unset"
    summary = "clear all settings of a plugin"
    usage = "plugin config unset <id>"
    mutating = True

    def run(self, ctx, args):
        if not args:
            raise CommandError("plugin config unset needs an id", hint=self.usage)
        plugins = ctx.require("plugins")
        target = _resolve(ctx, args[0])
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.CLIENT_CONFIG, "clear settings of %s" % target.id,
                           assume_yes=ctx.assume_yes)
        ok, detail = plugins.clear_settings(target.id)
        if not ok:
            raise CommandError(detail)
        return blocks.summary(detail, role=blocks.SUCCESS)


def _format_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _parse_value(raw):
    """Turns typed text into a real type, since settings are typed."""
    text = raw.strip()
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def build():
    return Group("plugin", "inspect and control plugins", [
        ListCommand(),
        InfoCommand(),
        EnableCommand(),
        DisableCommand(),
        ReloadCommand(),
        PathCommand(),
        ConfigCommand(),
    ])
