# SPDX-License-Identifier: Apache-2.0

"""`patch` — take a plugin apart, change it, build the change into a plugin.

A plugin on the phone is a zip of Python. Everything needed to open one, edit
it and put it back has been in the container for a while — an editor, a shell,
`elyb` if you want it compiled, and `plugin install` at the end — but the
first and last steps were still handwork nobody would guess at: find where the
client keeps the file, unzip it somewhere, remember what it looked like,
rewrite four lines of metadata so the result does not fight with the original,
zip it again.

So:

    patch open <plugin>     the plugin, unpacked under /patch, ready to edit
    patch diff              what you have changed since you opened it
    patch build --install   a new plugin carrying those changes

The result is a *new* plugin — a different id, `extCLI patch-62Yg28` for a
name, and the summary of what moved in its description. Not an overwrite: the
original stays installed and working, which is the entire reason to be able to
build a patch rather than just editing in place. Turning the patch off puts
the phone back where it was, and that is a property worth giving up a tidier
name for.
"""

import os

from ...render import blocks
from ..registry import Command, CommandError, Group, parse_flags, suggest

# How many files a report lists before it stops and says how many are left. A
# workspace where two hundred files changed is one where the count is the
# useful part.
LISTED = 24


def _roots():
    """(where workspaces live, where their pristine copies live)."""
    from ...compat import paths

    return paths.patch_dir(), paths.state_dir()


def _store():
    from ...patch import store

    return store


def _names(ctx, prefix=""):
    del ctx
    work_root, _state = _roots()
    return [name for name in _store().names(work_root)
            if name.startswith(prefix)]


def _one(ctx, args, strict=False):
    """The workspace an argument names, or the only one there is.

    Every subcommand takes a name and none of them insist on it: somebody with
    one workspace open should not have to type its name into every command
    about it, and somebody with three should be told that they have three.

    The first argument is only *maybe* a name — `patch diff helper.py` and
    `patch code thing.pyc` both put a filename there — so a word that is not
    a workspace is left alone rather than rejected, and the caller finds it
    still in its positional list.

    `strict` is for the one command that throws something away. Being handed
    the only workspace because a name was mistyped is fine for a command that
    prints a diff and unacceptable for one that deletes.
    """
    del ctx
    work_root, _state = _roots()
    existing = _store().names(work_root)
    if not existing:
        raise CommandError("no patch workspaces are open",
                           hint="patch open <plugin>")
    if args and args[0] in existing:
        return args[0]
    if len(existing) == 1 and not (strict and args):
        return existing[0]
    if args:
        raise CommandError("no workspace called %s" % args[0],
                           hint=suggest(args[0], existing) or
                           "open: " + ", ".join(existing[:6]))
    raise CommandError("%d workspaces are open" % len(existing),
                       hint="say which: " + ", ".join(existing[:6]))


def _guest(ctx, path):
    """A host path as the shell would name it, where the shell can see it."""
    env = getattr(ctx, "env", None)
    if env is None:
        return path
    try:
        guest = env.guest(path)
    except Exception:
        return path
    return guest or path


class OpenCommand(Command):
    name = "open"
    summary = "unpack an installed plugin into a workspace"
    usage = "patch open <plugin-id> [--name <workspace>] [--force]"
    mutating = True

    def run(self, ctx, args):
        from ...patch import store as store_module

        flags = parse_flags(args, {"--name": "str", "-n": "str",
                                   "--force": "bool", "-f": "bool"})
        if not flags.positional:
            raise CommandError("patch open needs a plugin", hint=self.usage)
        target = _resolve(ctx, flags.positional[0])
        if not target.path:
            raise CommandError(
                "the client did not say where %s is kept" % target.id,
                hint="patch open also takes a directory or a .eaf path")

        from ...patch import workspace as workspace_module

        name = workspace_module.workspace_name(
            flags.get("--name") or flags.get("-n") or target.id)
        work_root, state_root = _roots()
        ok, detail = store_module.create(
            work_root, state_root, name, target.path,
            label=target.name or target.id, version=target.version,
            replace=flags.has("--force") or flags.has("-f"))
        if not ok:
            raise CommandError(detail,
                               hint="patch open %s --force replaces it"
                                    % flags.positional[0])
        files = len(workspace_module.walk(detail))
        result = [blocks.Fields([("workspace", name),
                                 ("from", "%s %s" % (target.name or target.id,
                                                     target.version or "")),
                                 ("files", str(files)),
                                 ("path", _guest(ctx, detail))],
                                title="opened")]
        warning = _mount_warning()
        if warning:
            result.append(blocks.Blank())
            result.append(blocks.Text(warning, role=blocks.WARN))
        result.append(blocks.Summary(
            "edit it, then `patch diff` and `patch build`",
            role=blocks.SUCCESS))
        return blocks.Result(result)

    def complete(self, ctx, args):
        if not ctx.has("plugins"):
            return []
        prefix = args[-1] if args else ""
        try:
            return sorted(p.id for p in ctx.services.plugins.list_plugins()
                          if p.id.startswith(prefix))
        except Exception:
            return []


def _resolve(ctx, query):
    """The installed plugin an argument names, however it was typed."""
    plugins = ctx.require("plugins")
    found = plugins.get(query)
    if found is not None:
        return found
    matches = plugins.find(query)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = [p.id for p in plugins.list_plugins()]
        raise CommandError("no such plugin: %s" % query,
                           hint=suggest(query, known))
    raise CommandError("%r matches %d plugins" % (query, len(matches)),
                       hint="be specific: " +
                            ", ".join(p.id for p in matches[:4]))


def _mount_warning():
    """Lines to print when the workspace was written somewhere the shell
    cannot open.

    `/patch` is off by default — it is a workbench that costs space to lay out,
    and most people never want one — which means the first `patch open`
    somebody runs writes a tree they cannot `cd` into. Saying so with the
    command that fixes it is the difference between a feature and a dead end.
    """
    try:
        from ...rootfs import mounts
        from ...ui import prefs

        if prefs.mount_values().get(mounts.PATCH):
            return []
    except Exception:
        return []
    return ["/patch is not mounted, so the shell cannot see the workspace.",
            "Turn it on: config set mount_patch true, then reopen the console."]


class ListCommand(Command):
    name = "list"
    summary = "the workspaces that are open"
    usage = "patch list"

    def run(self, ctx, args):
        del args
        work_root, state_root = _roots()
        store = _store()
        existing = store.names(work_root)
        if not existing:
            return blocks.summary("no patch workspaces are open")
        rows = []
        for name in existing:
            note = store.note(state_root, name)
            if store.openable(state_root, name):
                changed = store.changes(work_root, state_root, name).sentence()
            else:
                changed = "nothing to compare against"
            rows.append((name, note.get("label") or "", changed))
        return blocks.Result([
            blocks.Table(rows, header=("workspace", "from", "changed")),
            blocks.Summary("%d open" % len(rows)),
        ])


class DiffCommand(Command):
    name = "diff"
    summary = "what has changed in a workspace"
    usage = "patch diff [workspace] [file] [--stat]"

    def run(self, ctx, args):
        from ...patch import workspace as workspace_module

        flags = parse_flags(args, {"--stat": "bool", "-s": "bool"})
        rest = list(flags.positional)
        name = _one(ctx, rest)
        if rest and rest[0] == name:
            rest.pop(0)

        work_root, state_root = _roots()
        store = _store()
        if not store.openable(state_root, name):
            raise CommandError(
                "there is no copy of %s as it was opened" % name,
                hint="it went with the stored data; build it and compare by "
                     "hand, or open it again")
        changed = store.changes(work_root, state_root, name)

        if rest and not (flags.has("--stat") or flags.has("-s")):
            path = rest[0].replace(os.sep, "/")
            known = [entry.path for entry in changed]
            if path not in known:
                raise CommandError("%s has not changed" % path,
                                   hint=suggest(path, known))
            lines = workspace_module.unified(
                store.origin_dir(state_root, name),
                store.work_dir(work_root, name), path)
            return blocks.Result([blocks.Text(lines),
                                  blocks.Summary(changed.sentence())])

        if changed.empty():
            return blocks.summary("%s is exactly as it was opened" % name)
        return blocks.Result([
            blocks.Text(changed.lines(limit=LISTED)),
            blocks.Summary(changed.sentence()),
        ])

    def complete(self, ctx, args):
        return _names(ctx, args[-1] if args else "")


class BuildCommand(Command):
    """`patch build` — the workspace, as a plugin that can be installed.

    The name carries a random mark rather than a number that goes up. Two
    builds of the same workspace are two different plugins and both of them
    are on the phone at once until one is removed, so what the name has to do
    is tell them apart — and a mark does that without anybody having to
    remember which of `patch 3` and `patch 4` was the one that worked.
    """

    name = "build"
    summary = "build the workspace into a plugin"
    usage = ("patch build [workspace] [--install] [--out <file>] "
             "[--mark <text>] [--empty]")
    mutating = True

    def run(self, ctx, args):
        from ...patch import pack, workspace as workspace_module

        flags = parse_flags(args, {"--install": "bool", "-i": "bool",
                                   "--out": "str", "-o": "str",
                                   "--mark": "str", "--empty": "bool"})
        rest = list(flags.positional)
        name = _one(ctx, rest)
        work_root, state_root = _roots()
        store = _store()
        work = store.work_dir(work_root, name)

        if store.openable(state_root, name):
            changed = store.changes(work_root, state_root, name)
        else:
            changed = workspace_module.Changes()
        if changed.empty() and not flags.has("--empty"):
            raise CommandError(
                "%s is exactly as it was opened" % name,
                hint="edit something first, or `patch build --empty` to "
                     "build it anyway")

        mark = flags.get("--mark") or workspace_module.token()
        note = store.note(state_root, name)
        target = self._target(ctx, flags, mark)
        ok, detail = pack.build(work, target, mark, changed,
                                source=note.get("label") or name)
        if not ok:
            raise CommandError(detail)

        rows = [("name", detail), ("id", workspace_module.plugin_id(mark)),
                ("file", _guest(ctx, target)),
                ("size", _size(target))]
        result = [blocks.Fields(rows, title="built")]
        if changed:
            result.append(blocks.Blank())
            result.append(blocks.Text(changed.lines(limit=LISTED)))

        if flags.has("--install") or flags.has("-i"):
            result.append(blocks.Blank())
            result.append(self._install(ctx, target, mark))
            return blocks.Result(result)
        result.append(blocks.Summary(
            "`plugin install %s` to put it on" % _guest(ctx, target),
            role=blocks.SUCCESS))
        return blocks.Result(result)

    def _target(self, ctx, flags, mark):
        from ...compat import paths

        chosen = flags.get("--out") or flags.get("-o")
        if chosen:
            env = getattr(ctx, "env", None)
            path = env.host(chosen) if env is not None else chosen
            if os.path.isdir(path):
                return os.path.join(path, "extcli-patch-%s.eaf" % mark)
            return path
        return os.path.join(paths.tmp_dir(), "extcli-patch-%s.eaf" % mark)

    def _install(self, ctx, target, mark):
        from ...patch import workspace as workspace_module

        plugins = ctx.require("plugins")
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.PLUGIN_INSTALL,
                           workspace_module.plugin_name(mark),
                           assume_yes=ctx.assume_yes)
        ok, detail = plugins.install(target)
        if not ok:
            raise CommandError(detail)
        return blocks.Summary("installed — `plugin list` to see it",
                              role=blocks.SUCCESS)

    def complete(self, ctx, args):
        return _names(ctx, args[-1] if args else "")


def _size(path):
    from ...utils import purge

    try:
        return purge.human_size(os.path.getsize(path))
    except Exception:
        return "?"


class CodeCommand(Command):
    """`patch code` — what a compiled file holds, and the one safe way in.

    Most published plugins ship compiled: `.pyc` and no source. A workspace
    opened on one of those is a tree nothing can be done with, which is where
    a patch tool stops being useful exactly when it would help most.

    Two things are honest to do about that. Read it — the disassembly is
    exact, and every string and name in the file is there, which is usually
    the question anyway. And change what can be changed exactly: a constant is
    a value in a table, and swapping one for another leaves every jump, offset
    and line number where it was. A URL, a label, a limit — most of what a
    patch is actually about is a constant.

    What it will not do is pretend to decompile. Python 3.11 has no working
    decompiler; what comes out of the ones that claim otherwise is functions
    with empty bodies, and a patch built on that would quietly delete code.
    """

    name = "code"
    summary = "read, or edit a constant in, a compiled file"
    usage = ("patch code [workspace] <file> [--dis] [--names] "
             "[--set <old> <new>]")
    # only with `--set`, but the flag says what a command *can* do, and one
    # that can rewrite a file is not one to run from a chat by accident
    mutating = True

    def run(self, ctx, args):
        from ...patch import bytecode

        flags = parse_flags(args, {"--dis": "bool", "-d": "bool",
                                   "--names": "bool", "--set": "str",
                                   "--to": "str", "--lines": "int"})
        rest = list(flags.positional)
        name = _one(ctx, rest)
        if rest and rest[0] == name:
            rest.pop(0)
        work_root, _state = _roots()
        work = _store().work_dir(work_root, name)

        if not rest:
            return self._overview(ctx, work, name)
        path = self._file(work, rest[0])

        old = flags.get("--set")
        if old is not None:
            return self._rewrite(ctx, path, old, flags, rest)

        ok, why = bytecode.readable(path)
        if not ok:
            raise CommandError("%s: %s" % (rest[0], why))
        _header, code = bytecode.load(path)
        if flags.has("--dis") or flags.has("-d"):
            return blocks.Result([
                blocks.Text(bytecode.listing(code,
                                             limit=flags.get("--lines", 400))),
                blocks.Summary("%s, as it will run" % rest[0]),
            ])
        if flags.has("--names"):
            found = bytecode.names(code)
            return blocks.Result([blocks.Text(found),
                                  blocks.Summary("%d names" % len(found))])
        found = bytecode.strings(code, minimum=2)
        return blocks.Result([
            blocks.Fields(bytecode.summary(code), title=rest[0]),
            blocks.Blank(),
            blocks.Text(found[:LISTED * 4]),
            blocks.Summary("%d strings — `--set <old> <new>` to change one"
                           % len(found)),
        ])

    def _overview(self, ctx, work, name):
        """No file named: say which ones there are to look at."""
        from ...patch import bytecode

        found = bytecode.compiled_files(work)
        if not found:
            return blocks.summary(
                "%s has no compiled files — its source is right there" % name)
        return blocks.Result([
            blocks.Text(found[:LISTED * 4]),
            blocks.Summary("%d compiled file%s — `patch code %s <file>`"
                           % (len(found), "" if len(found) == 1 else "s",
                              name)),
        ])

    def _file(self, work, given):
        relative = given.replace(os.sep, "/")
        path = os.path.join(work, relative.replace("/", os.sep))
        if os.path.isfile(path):
            return path
        from ...patch import bytecode

        found = bytecode.compiled_files(work)
        # a bare name is what somebody types after reading the list
        tail = [entry for entry in found
                if entry.endswith("/" + relative) or entry == relative]
        if len(tail) == 1:
            return os.path.join(work, tail[0].replace("/", os.sep))
        raise CommandError("no such file in the workspace: %s" % given,
                           hint=suggest(relative, found))

    def _rewrite(self, ctx, path, old, flags, rest):
        from ...patch import bytecode

        new = flags.get("--to")
        if new is None:
            # `--set old new`: the second word is what it becomes
            new = rest[1] if len(rest) > 1 else None
        if new is None:
            raise CommandError("patch code --set needs what to change it to",
                               hint=self.usage)
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.FS_WRITE,
                           "rewrite a constant in %s"
                           % os.path.basename(path),
                           assume_yes=ctx.assume_yes)
        count, detail = bytecode.rewrite(path, old, new)
        if not count:
            raise CommandError(detail,
                               hint="`patch code <file>` lists what is in it")
        return blocks.summary("%s — `patch diff` shows the file as changed"
                              % detail, role=blocks.SUCCESS)

    def complete(self, ctx, args):
        return _names(ctx, args[-1] if args else "")


class RevertCommand(Command):
    name = "revert"
    summary = "put a workspace back the way it was opened"
    usage = "patch revert [workspace] [file]"
    mutating = True

    def run(self, ctx, args):
        import shutil

        flags = parse_flags(args, {})
        rest = list(flags.positional)
        name = _one(ctx, rest)
        if rest and rest[0] == name:
            rest.pop(0)
        work_root, state_root = _roots()
        store = _store()
        if not store.openable(state_root, name):
            raise CommandError("there is no copy of %s to go back to" % name)
        changed = store.changes(work_root, state_root, name)
        if changed.empty():
            return blocks.summary("%s is already as it was opened" % name)

        origin = store.origin_dir(state_root, name)
        work = store.work_dir(work_root, name)
        wanted = [entry for entry in changed
                  if not rest or entry.path == rest[0].replace(os.sep, "/")]
        if rest and not wanted:
            raise CommandError("%s has not changed" % rest[0],
                               hint=suggest(rest[0],
                                            [e.path for e in changed]))

        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.FS_DELETE,
                           "undo %d file%s in %s"
                           % (len(wanted), "" if len(wanted) == 1 else "s",
                              name),
                           assume_yes=ctx.assume_yes)

        done = 0
        for entry in wanted:
            source = os.path.join(origin, entry.path.replace("/", os.sep))
            destination = os.path.join(work, entry.path.replace("/", os.sep))
            try:
                if os.path.exists(source):
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copy2(source, destination)
                elif os.path.exists(destination):
                    os.remove(destination)
                done += 1
            except Exception as e:
                raise CommandError("could not put %s back: %s"
                                   % (entry.path, e))
        return blocks.summary("%d file%s back as it was"
                              % (done, "" if done == 1 else "s"),
                              role=blocks.SUCCESS)

    def complete(self, ctx, args):
        return _names(ctx, args[-1] if args else "")


class DropCommand(Command):
    name = "drop"
    summary = "throw a workspace away"
    usage = "patch drop <workspace>"
    mutating = True

    def run(self, ctx, args):
        flags = parse_flags(args, {})
        name = _one(ctx, list(flags.positional), strict=True)
        work_root, state_root = _roots()
        store = _store()
        changed = (store.changes(work_root, state_root, name)
                   if store.openable(state_root, name) else None)
        policy = ctx.services.policy
        if policy is not None:
            policy.require(
                policy.FS_DELETE,
                "%s (%s)" % (name, changed.sentence() if changed
                             else "nothing recorded"),
                assume_yes=ctx.assume_yes)
        ok, detail = store.drop(work_root, state_root, name)
        if not ok:
            raise CommandError(detail)
        return blocks.summary(detail, role=blocks.SUCCESS)

    def complete(self, ctx, args):
        return _names(ctx, args[-1] if args else "")


def build():
    return Group("patch", "edit a plugin and build the change into one", [
        OpenCommand(),
        ListCommand(),
        DiffCommand(),
        CodeCommand(),
        BuildCommand(),
        RevertCommand(),
        DropCommand(),
    ])
