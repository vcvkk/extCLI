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


class InstallCommand(Command):
    """`plugin install <file.eaf>` — the last step of writing one.

    It closes a loop that was open at both ends: the container can already
    install elyxbuilder, so a plugin can be edited and built on the phone, and
    the console could already reload one — but the archive still had to be
    carried out to the client by hand. Now the whole round trip is commands:
    edit, `elyb build`, `plugin install`, `plugin reload`.

    The archive is read here before the client is handed anything. A mistyped
    path or somebody's photo should be refused with a reason, not passed on to
    fail somewhere with less to say.
    """

    name = "install"
    summary = "install a plugin from a .eaf file"
    usage = "plugin install <file.eaf> [--force]"
    mutating = True

    def run(self, ctx, args):
        import os

        flags = parse_flags(args, {"--force": "bool", "-f": "bool"})
        if not flags.positional:
            raise CommandError("plugin install needs a file", hint=self.usage)
        plugins = ctx.require("plugins")
        raw = flags.positional[0]
        env = getattr(ctx, "env", None)
        path = env.host(raw) if env is not None else raw
        if os.path.isdir(path):
            raise CommandError("%s is a directory" % raw,
                               hint="build it first: elyb build -c 2 -nf")
        if not os.path.isfile(path):
            raise CommandError("no such file: %s" % raw)

        data = plugins.read_archive(path)
        if data is None:
            raise CommandError("%s is not a plugin archive" % raw,
                               hint="a .eaf is a zip with refmap.yml in it")
        plugin_id = data.get("id")

        # An install that lands on top of a plugin already there is an update,
        # and saying so is the difference between "it worked" and "what did I
        # just overwrite". Refusing it by default is the same courtesy `rootfs
        # install` extends to a container somebody has set up.
        existing = plugins.get(plugin_id)
        force = flags.has("--force") or flags.has("-f")
        if existing is not None and not force:
            raise CommandError(
                "%s is already installed (version %s)"
                % (plugin_id, existing.version or "unknown"),
                hint="plugin install %s --force to replace it with %s"
                     % (raw, data.get("version") or "this build"))

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.PLUGIN_INSTALL,
                           "%s %s" % (plugin_id, data.get("version") or ""),
                           assume_yes=ctx.assume_yes)

        ok, detail = plugins.install(path)
        if not ok:
            raise CommandError(detail)
        rows = [(key, data[key]) for key in ("id", "name", "version", "author")
                if data.get(key)]
        return blocks.Result([
            blocks.Fields(rows, title="installed"),
            blocks.Summary(
                "replaced %s" % plugin_id if existing is not None
                else "%s installed — `plugin list` to see it" % plugin_id,
                role=blocks.SUCCESS),
        ])

    def complete(self, ctx, args):
        return _archive_paths(ctx, args[-1] if args else "")


def _archive_paths(ctx, prefix):
    """Completes towards .eaf files, since that is the only thing this takes."""
    import os

    env = getattr(ctx, "env", None)
    if env is None:
        return []
    directory, _, tail = prefix.rpartition("/")
    try:
        entries = os.listdir(env.host(directory or "."))
    except Exception:
        return []
    out = []
    for entry in sorted(entries):
        if not entry.startswith(tail):
            continue
        full = "%s/%s" % (directory, entry) if directory else entry
        if os.path.isdir(env.host(full)):
            out.append(full + "/")
        elif entry.endswith(".eaf"):
            out.append(full)
    return out


def build():
    return Group("plugin", "inspect and control plugins", [
        ListCommand(),
        InfoCommand(),
        InstallCommand(),
        EnableCommand(),
        DisableCommand(),
        ReloadCommand(),
        PathCommand(),
        ConfigCommand(),
    ])
