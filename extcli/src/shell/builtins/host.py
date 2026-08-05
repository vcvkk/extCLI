# SPDX-License-Identifier: Apache-2.0

"""`host` — what extCLI is running on, and what it is allowed to do.

`host class` is a development tool: it prints the real signatures of a client
class on the device, which is how compat/ gets pinned to a client version
instead of guessing from a decompiled dex.
"""

from ...render import blocks
from ..registry import Command, CommandError, Group, parse_flags


class StatusCommand(Command):
    name = "status"
    summary = "plugin, client, device"
    usage = "host status"

    def run(self, ctx, args):
        host = ctx.require("host")
        return blocks.fields(host.describe())


class PathsCommand(Command):
    name = "paths"
    summary = "where extCLI keeps its files"
    usage = "host paths"

    def run(self, ctx, args):
        paths = ctx.require("paths")
        rows = []
        for label, path, exists in paths.describe():
            rows.append((label, path if exists else "%s  (missing)" % path))
        return blocks.fields(rows)


class DoctorCommand(Command):
    name = "doctor"
    summary = "probe execution backends"
    usage = "host doctor [--refresh]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"--refresh": "bool", "-r": "bool"})
        probe = ctx.require("probe")
        force = flags.has("--refresh") or flags.has("-r")
        result = probe.result(force=force)

        rows = []
        for name in ("shell", "toybox", "pty", "data_exec", "linker"):
            check = result.get("checks", {}).get(name)
            if not check:
                continue
            status = check.get("status")
            role = {"ok": blocks.SUCCESS, "blocked": blocks.ERROR}.get(
                status, blocks.DIM
            )
            rows.append((name, "%s - %s" % (status, check.get("detail", "")), role))

        extra = probe.extra_checks() if hasattr(probe, "extra_checks") else []
        for label, ok, detail in extra:
            rows.append((label, "%s - %s" % ("ok" if ok else "failed", detail),
                         blocks.SUCCESS if ok else blocks.ERROR))

        backends = ", ".join(result.get("backends", [])) or "none"
        return blocks.Result([
            blocks.Fields(rows),
            blocks.Blank(),
            blocks.Fields([
                ("backends", backends),
                ("rootfs", probe.rootfs_verdict(result)),
            ]),
        ])


class VersionCommand(Command):
    name = "version"
    summary = "plugin version"
    usage = "host version"

    def run(self, ctx, args):
        host = ctx.require("host")
        return blocks.summary("extCLI %s" % (host.plugin_version() or "unknown"),
                              role=blocks.FG)


class ClassCommand(Command):
    name = "class"
    summary = "print signatures of a client class"
    usage = "host class <fully.qualified.Name> [filter]"

    def run(self, ctx, args):
        if not args:
            raise CommandError("host class needs a class name",
                              hint="host class com.exteragram.messenger.plugins.PluginsController")
        reflect = ctx.require("reflect")
        class_name = args[0]
        needle = args[1] if len(args) > 1 else None
        described = reflect.describe(class_name, needle)
        if described is None:
            raise CommandError("class not found: %s" % class_name)

        result = blocks.Result()
        if described["fields"]:
            result.add(blocks.Text(described["fields"], role=blocks.DIM))
            result.add(blocks.Blank())
        result.add(blocks.Text(described["methods"] or ["(no matching methods)"]))
        result.add(blocks.Summary("%d methods, %d fields" % (
            len(described["methods"]), len(described["fields"]))))
        return result


class WindowCommand(Command):
    """Reports the console's own window.

    Whether a window covers the system bars cannot be reasoned out from source:
    flags interact, the platform changes what it honours between versions, and
    two rounds of fixing a see-through strip behind the navigation bar were
    guesses made blind. This asks the device.
    """

    name = "window"
    summary = "size and flags of the console window"
    usage = "host window"

    def run(self, ctx, args):
        console = ctx.require("terminal")
        describe = getattr(console, "describe_window", None)
        if not callable(describe):
            raise CommandError("this console cannot describe its window")
        rows = describe()
        return blocks.Result([
            blocks.Fields(rows, title="console window"),
            blocks.Summary("decor should match display when the window covers "
                           "the bars"),
        ])


class SelfTestCommand(Command):
    """Runs the bundled script that exercises every command.

    A script rather than Python: it is a plain file the user can read and edit,
    and running it puts the shell itself under test — functions, "$@", $? and
    redirection all have to work for the report to come out.
    """

    name = "selftest"
    summary = "run every command and report what worked"
    usage = "host selftest"
    mutating = True

    SCRIPT = "selftest.sh"

    def run(self, ctx, args):
        import os

        paths = ctx.require("paths")
        path = os.path.join(paths.res_dir(), self.SCRIPT)
        if not os.path.isfile(path):
            raise CommandError("the self-test script is missing",
                               hint="expected it at %s" % path)
        runner = getattr(ctx, "run_script_text", None)
        if runner is None:
            raise CommandError("the self-test needs the console",
                               hint="it runs in the shell, not in a chat")
        with open(path, "r", encoding="utf-8") as handle:
            return runner(handle.read())


def build():
    return Group("host", "environment and diagnostics", [
        StatusCommand(),
        PathsCommand(),
        DoctorCommand(),
        VersionCommand(),
        ClassCommand(),
        WindowCommand(),
        SelfTestCommand(),
    ])
