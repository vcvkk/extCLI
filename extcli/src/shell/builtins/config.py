# SPDX-License-Identifier: Apache-2.0

"""`config` — the client's own settings.

exteraGram stores its preferences in a SharedPreferences file that can list
itself, so these commands work from what the device actually has rather than
from a table of keys that would rot with every client release.

Writing goes through policy.CLIENT_CONFIG: it changes the user's client, and
when confirmations are turned on before release, this is where they appear.
"""

import json

from ...render import blocks
from ..registry import Command, CommandError, Group, parse_flags, suggest

MAX_LISTED = 200


def _settings(ctx):
    return ctx.require("settings")


def _format(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _parse(text):
    lowered = text.strip().lower()
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


def _store(flags, default=None):
    return flags.get("--store", default or "exteraconfig")


class ListCommand(Command):
    name = "list"
    summary = "every stored setting"
    usage = "config list [--store <name>] [prefix]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"--store": "str", "-s": "str"})
        settings = _settings(ctx)
        store = flags.get("--store") or flags.get("-s") or "exteraconfig"
        values = settings.all_values(store)
        if not values:
            return blocks.summary("%s has no stored settings" % store)

        prefix = flags.positional[0].lower() if flags.positional else ""
        keys = sorted(k for k in values if k.lower().startswith(prefix))
        if not keys:
            return blocks.summary("no settings start with %r" % prefix)

        shown = keys[:MAX_LISTED]
        rows = [(key, _format(values[key])) for key in shown]
        result = blocks.Result([blocks.Fields(rows, title=store)])
        if len(keys) > len(shown):
            result.add(blocks.Summary("%d of %d shown; narrow with a prefix"
                                      % (len(shown), len(keys))))
        else:
            result.add(blocks.Summary("%d settings" % len(keys)))
        return result

    def complete(self, ctx, args):
        return _complete_keys(ctx, args)


class GetCommand(Command):
    name = "get"
    summary = "read one setting"
    usage = "config get <key> [--store <name>]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"--store": "str", "-s": "str"})
        if not flags.positional:
            raise CommandError("config get needs a key", hint=self.usage)
        settings = _settings(ctx)
        store = _store(flags)
        key = flags.positional[0]
        values = settings.all_values(store)
        if key not in values:
            raise CommandError("%s has no setting %r" % (store, key),
                               hint=suggest(key, values.keys()))
        value = values[key]
        return blocks.text("%s = %s   (%s)" % (key, _format(value),
                                               settings.type_name(value)))

    def complete(self, ctx, args):
        return _complete_keys(ctx, args)


def _mount_refusal(key, value):
    """The one rule `config set` shares with the settings page: the last
    mounted path cannot be turned off.

    Checked here as well as there because a setting is a setting whichever door
    it is changed through, and a console with nowhere to open is not something
    to find out about after the fact.
    """
    from ...rootfs import mounts

    if key not in mounts.SETTINGS:
        return ""
    try:
        from ...ui import prefs

        values = prefs.mount_values()
    except Exception:
        return ""
    return mounts.refusal(values, key, bool(value))


class SetCommand(Command):
    name = "set"
    summary = "change a setting"
    usage = "config set <key> <value> [--store <name>]"
    mutating = True

    def run(self, ctx, args):
        flags = parse_flags(args, {"--store": "str", "-s": "str",
                                   "--new": "bool"})
        if len(flags.positional) < 2:
            raise CommandError("config set needs a key and a value",
                               hint=self.usage)
        settings = _settings(ctx)
        store = _store(flags)
        key = flags.positional[0]
        raw = " ".join(flags.positional[1:])
        value = _parse(raw)

        values = settings.all_values(store)
        existing = values.get(key)
        if key not in values and not flags.has("--new"):
            # a typo here writes a key the client never reads; say so instead
            raise CommandError(
                "%s has no setting %r" % (store, key),
                hint=(suggest(key, values.keys())
                      or "pass --new to create it anyway"),
            )

        refused = _mount_refusal(key, value)
        if refused:
            raise CommandError(refused, hint="turn another one on first")

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.CLIENT_CONFIG, "%s.%s = %s" % (store, key, value),
                           assume_yes=ctx.assume_yes)

        ok, detail = settings.set_value(key, value, store, existing)
        if not ok:
            raise CommandError(detail)
        return blocks.Result([
            blocks.Summary(detail, role=blocks.SUCCESS),
            blocks.Text("restart the client for it to take effect everywhere",
                        role=blocks.DIM),
        ])

    def complete(self, ctx, args):
        return _complete_keys(ctx, args)


class UnsetCommand(Command):
    name = "unset"
    summary = "remove a setting"
    usage = "config unset <key> [--store <name>]"
    mutating = True

    def run(self, ctx, args):
        flags = parse_flags(args, {"--store": "str", "-s": "str"})
        if not flags.positional:
            raise CommandError("config unset needs a key", hint=self.usage)
        settings = _settings(ctx)
        store = _store(flags)
        key = flags.positional[0]
        values = settings.all_values(store)
        if key not in values:
            raise CommandError("%s has no setting %r" % (store, key),
                               hint=suggest(key, values.keys()))

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.CLIENT_CONFIG, "remove %s.%s" % (store, key),
                           assume_yes=ctx.assume_yes)

        ok, detail = settings.remove(key, store)
        if not ok:
            raise CommandError(detail)
        return blocks.summary(detail, role=blocks.SUCCESS)

    def complete(self, ctx, args):
        return _complete_keys(ctx, args)


class SearchCommand(Command):
    name = "search"
    summary = "find settings by name or value"
    usage = "config search <text> [--store <name>]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"--store": "str", "-s": "str"})
        if not flags.positional:
            raise CommandError("config search needs something to look for",
                               hint=self.usage)
        settings = _settings(ctx)
        store = _store(flags)
        query = " ".join(flags.positional)
        found = settings.search(query, store)
        if not found:
            return blocks.summary("nothing matches %r in %s" % (query, store))
        rows = [(key, _format(found[key])) for key in sorted(found)]
        return blocks.Result([
            blocks.Fields(rows, title=store),
            blocks.Summary("%d matches" % len(rows)),
        ])


class StoresCommand(Command):
    name = "stores"
    summary = "which preference files exist"
    usage = "config stores"

    def run(self, ctx, args):
        settings = _settings(ctx)
        return blocks.fields(settings.describe())


class ExportCommand(Command):
    name = "export"
    summary = "write settings to a file as JSON"
    usage = "config export <file> [--store <name>]"
    mutating = True

    def run(self, ctx, args):
        flags = parse_flags(args, {"--store": "str", "-s": "str"})
        if not flags.positional:
            raise CommandError("config export needs a file", hint=self.usage)
        settings = _settings(ctx)
        store = _store(flags)
        values = settings.all_values(store)
        path = _resolve(ctx, flags.positional[0])
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"store": store, "values": values}, handle, indent=2,
                          ensure_ascii=False, sort_keys=True)
        except Exception as e:
            raise CommandError("cannot write %s: %s" % (path, e))
        return blocks.summary("%d settings written to %s" % (len(values), path),
                              role=blocks.SUCCESS)


class ImportCommand(Command):
    name = "import"
    summary = "apply settings from a JSON file"
    usage = "config import <file> [--store <name>] [--new]"
    mutating = True

    def run(self, ctx, args):
        flags = parse_flags(args, {"--store": "str", "-s": "str",
                                   "--new": "bool"})
        if not flags.positional:
            raise CommandError("config import needs a file", hint=self.usage)
        settings = _settings(ctx)
        path = _resolve(ctx, flags.positional[0])
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as e:
            raise CommandError("cannot read %s: %s" % (path, e))

        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            raise CommandError("%s is not a settings export" % path,
                               hint="expected an object with a 'values' key")
        store = flags.get("--store") or (data.get("store") if isinstance(data, dict)
                                         else None) or "exteraconfig"

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.CLIENT_CONFIG,
                           "import %d settings into %s" % (len(values), store),
                           assume_yes=ctx.assume_yes)

        current = settings.all_values(store)
        applied, skipped, failed = 0, 0, []
        for key in sorted(values):
            if key not in current and not flags.has("--new"):
                skipped += 1
                continue
            ok, detail = settings.set_value(key, values[key], store,
                                            current.get(key))
            if ok:
                applied += 1
            else:
                failed.append(detail)

        result = blocks.Result([
            blocks.Summary("%d applied, %d skipped" % (applied, skipped),
                           role=blocks.SUCCESS if not failed else blocks.WARN),
        ])
        if skipped:
            result.add(blocks.Text("skipped keys the client does not have; "
                                   "pass --new to write them anyway",
                                   role=blocks.DIM))
        for detail in failed[:5]:
            result.add(blocks.Text(detail, role=blocks.ERROR))
        return result


def _resolve(ctx, path):
    env = getattr(ctx, "env", None)
    return env.host(path) if env is not None else path


def _complete_keys(ctx, args):
    if not ctx.has("settings"):
        return []
    prefix = args[-1] if args else ""
    try:
        keys = ctx.services.settings.all_values("exteraconfig")
    except Exception:
        return []
    return sorted(key for key in keys if key.startswith(prefix))


def build():
    return Group("config", "the client's own settings", [
        ListCommand(), GetCommand(), SetCommand(), UnsetCommand(),
        SearchCommand(), StoresCommand(), ExportCommand(), ImportCommand(),
    ])
