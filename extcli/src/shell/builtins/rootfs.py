# SPDX-License-Identifier: Apache-2.0

"""`rootfs` — a Linux root filesystem inside the client.

`rootfs probe exec` comes first on purpose. Whether a rootfs can work at all is a
property of this device's SELinux policy, not of this code: the plugin can run
its own binaries through the dynamic linker, but a rootfs needs the programs it
starts to be able to start others, and that is a different permission. The check
answers it in three lines, and everything else is only worth doing if it says so.

`rootfs install alpine` unpacks the Alpine minirootfs that ships inside the
plugin — no downloading, no finding the right architecture by hand. A path to a
tarball works too, for anything else.

There is no `rootfs run`. Once one is installed the console is *inside* it, so
its programs are typed the way programs are typed anywhere: `apk info`, not
`rootfs run apk info`. What is left here is everything that is about the rootfs
rather than in it — installing it, seeing what it holds, and the measurements
that had to be made before any of it could work.
"""

import os

from ...render import blocks
from ...rootfs import backend as backend_module
from ...rootfs import exec_probe, execdirs, guest, native
from ...rootfs import install as install_module
from ...rootfs import layout, mounts as mounts_module, network
from ...rootfs import runners
from ...rootfs import packages as packages_module
from ...rootfs import toolbox
from ...rootfs import sandbox, sources, writes
from ..registry import Command, CommandError, Group, parse_flags


def _root(ctx):
    paths = ctx.require("paths")
    return paths.rootfs_dir()


class StatusCommand(Command):
    name = "status"
    summary = "whether a rootfs is installed, and what it is"
    usage = "rootfs status"
    mutating = True

    def run(self, ctx, args):
        root = _root(ctx)
        rows = layout.status_rows(root)
        if layout.installed(root):
            # Written again first, so the row is true rather than what was
            # true when the console opened. A phone that has moved between
            # wifi and mobile data since then is holding a resolver that
            # nothing can reach, and "DNS: transient error" is what a guest
            # says about it — so this is also the way to put it right without
            # closing the console.
            _write_resolver(root)
            rows.append(("dns", network.describe(root)))
        result = blocks.Result([blocks.Fields(rows, title="rootfs")])
        todo = self._pending(ctx, root)
        if todo:
            # named rather than counted: "run `rootfs setup`" is the answer
            # either way, but a person who can see what is missing can tell a
            # setup that is still running from one that gave up
            result.add(blocks.Summary(
                "not ready yet: %s — `rootfs setup`"
                % ", ".join(todo), role=blocks.WARN))
        return result

    def _pending(self, ctx, root):
        from ...rootfs import setup as rootfs_setup

        try:
            paths = ctx.require("paths")
            return rootfs_setup.pending(paths.res_dir(), paths.state_dir(),
                                        root)
        except Exception:
            return []


def _linker(ctx):
    from ...backends import linker as linker_module

    host = ctx.require("host")
    found = linker_module.find_linker(getattr(host, "abi", lambda: None)())
    if not found:
        raise CommandError(
            "no dynamic linker on this device",
            hint="without it the plugin cannot run its own binaries at all")
    return found


class CheckCommand(Command):
    """The experiment that decides whether any of this is possible."""

    name = "exec"
    summary = "can programs we start, start others"
    usage = "rootfs probe exec"

    def run(self, ctx, args):
        paths = ctx.require("paths")
        found = _linker(ctx)
        results = exec_probe.run(paths.tmp_dir(), found)
        strategy, sentence = exec_probe.verdict(results)
        role = blocks.SUCCESS if strategy == exec_probe.DIRECT else (
            blocks.WARN if strategy == exec_probe.WRAPPED else blocks.ERROR)
        lines = exec_probe.summary_lines(results)
        matrix = lines[:lines.index("")] if "" in lines else lines
        return blocks.Result([
            blocks.Text(matrix),
            blocks.Blank(),
            blocks.Summary(sentence, role=role),
        ])


class DirsCommand(Command):
    """Is there anywhere we can write to and then execute from?

    Asked before anything is built around the answer being no. Everything else
    here — the linker trick, the launcher, our shell doing the starting — is
    unnecessary if one directory still permits execve, and finding that out
    costs one command.
    """

    name = "dirs"
    summary = "which directories allow execve"
    usage = "rootfs probe dirs"

    def run(self, ctx, args):
        paths = ctx.require("paths")
        candidates = paths.exec_candidates()
        results = execdirs.scan(candidates, _plain_runner())
        found, sentence = execdirs.verdict(results)
        lines = execdirs.summary_lines(results,
                                       [label for label, _ in candidates])
        matrix = lines[:lines.index("")] if "" in lines else lines
        role = blocks.SUCCESS if found else blocks.DIM
        return blocks.Result([
            blocks.Text(matrix),
            blocks.Blank(),
            blocks.Summary(sentence, role=role),
        ])


class NativeCommand(Command):
    """Will the linker start a binary extCLI built itself?

    Every measurement so far used a copy of toybox, which proves the linker
    runs a file from our directory, not that it runs *our* file. The loader
    that would let any rootfs run is a bigger program of the same shape as this
    probe, so this is the question that decides whether writing it is worth it.
    """

    name = "native"
    summary = "can our own binaries run here"
    usage = "rootfs probe native"

    def run(self, ctx, args):
        paths = ctx.require("paths")
        host = ctx.require("host")
        abi = getattr(host, "abi", lambda: None)()
        found = _linker(ctx)
        results = native.probe(paths.res_dir(), abi, found, _plain_runner())
        lines = native.summary_lines(results)
        matrix = lines[:lines.index("")] if "" in lines else lines
        pick = native.chosen(results)
        return blocks.Result([
            blocks.Text(matrix),
            blocks.Blank(),
            blocks.Summary(lines[-1],
                           role=blocks.SUCCESS if pick else blocks.ERROR),
        ])


class SyscallsCommand(Command):
    """What the app's sandbox allows.

    Alpine starts and dies with SIGSYS, and a SIGSYS handler cannot say which
    syscall: Android's filter answers with SECCOMP_RET_KILL_PROCESS, so the
    kernel kills outright and nothing of ours runs afterwards. Asked from
    outside instead — a child per syscall, and the way the child ended is the
    answer.

    The answer is kept. It describes the device rather than any rootfs, the
    scan takes a while, and everything that starts a guest program needs it:
    the loader replaces a refused call before the filter can kill anyone.
    """

    name = "syscalls"
    summary = "which syscalls this app may make"
    usage = "rootfs probe syscalls [--all]"

    def run(self, ctx, args):
        flags = parse_flags(args, {"--all": "bool", "-a": "bool"})
        everything = bool(flags.get("--all") or flags.get("-a"))
        paths = ctx.require("paths")
        refused = _measure_syscalls(ctx)
        sandbox.save(paths.state_dir(), refused)
        rule_list = sandbox.rules(refused)
        real = [rule for rule in rule_list if not native.unused(rule[0])]
        shown = rule_list if everything else real
        empty = len(rule_list) - len(real)

        result = blocks.Result()
        rows = sandbox.describe(shown, native.syscall_name)
        if rows:
            result.add(blocks.Text(rows))
            result.add(blocks.Blank())
        if empty and not everything:
            result.add(blocks.Text(
                "%d more are numbers arm64 has no syscall for at all. That "
                "they are refused is the useful part: the filter kills every "
                "number it does not know, so a call cannot be got rid of by "
                "turning it into nothing — it has to be turned into another "
                "call. `rootfs probe syscalls --all` lists them." % empty))
            result.add(blocks.Blank())
        if not sandbox.can_divert(refused):
            result.add(blocks.Text(
                "this device also refuses getpid, which is the call the "
                "loader puts in place of a refused one — nothing here can be "
                "answered"))
            result.add(blocks.Blank())
        result.add(blocks.Summary(
            sandbox.sentence(rule_list),
            role=blocks.WARN if refused else blocks.SUCCESS))
        return result


def _measure_syscalls(ctx):
    """Asks the device which numbers it refuses. Raises if it cannot finish."""
    paths = ctx.require("paths")
    host = ctx.require("host")
    abi = getattr(host, "abi", lambda: None)()
    found = _linker(ctx)
    tool = native.tool(paths.res_dir(), abi, native.SYSCALL_MAP)
    if not os.path.isfile(tool):
        raise CommandError("the syscall map is not built for %s"
                           % (abi or "this device"))
    code, out, err = _plain_runner(timeout=300)([found, tool])
    refused, complete = native.read_syscall_map(out)
    if not complete:
        raise CommandError(
            "the scan did not finish, so nothing has been learned",
            hint=(err or "exit %s" % code).strip()[:200])
    return refused


def _blocked(ctx):
    """EXTCLI_BLOCKED for this device, measuring it once if need be.

    Measured rather than assumed, and measured here rather than asked of the
    user: which syscalls a device refuses is not something anyone can be
    expected to know about their phone, and running a guest program without
    the answer means running it into a kill.
    """
    paths = ctx.require("paths")
    state = paths.state_dir()
    if sandbox.load(state) is None:
        try:
            sandbox.save(state, _measure_syscalls(ctx))
        except CommandError:
            return ""
    return sandbox.blocked_for(state)


def _plain_runner(timeout=15):
    return runners.plain(timeout)


class TraceCommand(Command):
    """Steps a guest program syscall by syscall and reports the last few.

    The map of every syscall number came back with Android's ordinary
    refusals, and musl makes none of them while starting a program — so the
    filter is refusing something by its arguments, and only a trace can say
    what. ptrace is not among the refusals, so the loader can fork, be traced
    by its own parent, and report what the guest was doing when it died.
    """

    name = "trace"
    summary = "syscall trace of a guest program"
    usage = "rootfs trace [--errno E] [--grep TEXT] [command ...]"
    mutating = True

    def run(self, ctx, args):
        args, wanted, needle = _trace_filter(args)
        root = _root(ctx)
        if not layout.installed(root):
            raise CommandError("no rootfs is installed",
                               hint="rootfs install alpine")
        found = _linker(ctx)
        shell = layout.shell_in(root) or "/bin/sh"
        argv = list(args) or [shell, "-c", "echo %s" % guest.MARKER]
        wanted = argv[0]
        if not wanted.startswith("/"):
            # `rootfs trace apk update` means the apk that is installed, not a
            # file in this directory called apk
            found_path = _backend(ctx).which(wanted)
            if found_path is None:
                raise CommandError("%s is not in the rootfs" % wanted,
                                   hint="`rootfs commands` lists what is there")
            argv[0] = found_path
        command = guest.command_for(guest.LOADER, root, found, argv,
                                    native_dir=_native_dir(ctx),
                                    argv0=wanted.rsplit("/", 1)[-1])
        if command is None:
            raise CommandError("%s cannot be started" % argv[0],
                               hint="`rootfs probe launch` says how guest programs "
                                    "start on this device")

        environment = guest.environment_for(guest.LOADER, root,
                                            blocked=_blocked(ctx),
                                            no_tmpfile=_no_tmpfile(ctx),
                                            linker=found,
                                            native_dir=_native_dir(ctx))
        environment["EXTCLI_TRACE"] = "1"
        # A diagnostic, and the thing being diagnosed may be an install. The
        # ordinary timeout is for a command whose answer should be immediate.
        code, out, err = _runner(ctx, timeout=600)(command, environment)
        text = "%s\n%s" % (err or "", out or "")
        numbers, total, rest = native.read_trace(text)
        failures = native.read_failures(text)
        if not numbers and not rest and not failures:
            return blocks.summary("the trace produced nothing (exit %s)" % code,
                                  role=blocks.ERROR)
        result = blocks.Result()
        if wanted is not None or needle:
            # asked for by name, out of thousands
            failures = native.matching_failures(failures, wanted, needle)
        if failures:
            # What went wrong, which is a different question from what killed
            # it — a program that keeps running leaves only this behind.
            #
            # The last of them, not the first. A program looking for a file
            # tries several places and expects to miss most of them, so the
            # beginning of this list is all noise; the failure that mattered is
            # the one nearest whatever it printed afterwards.
            shown = failures[-_TRACE_FAILURES:]
            if len(failures) > len(shown):
                result.add(blocks.Text(
                    "%d calls failed; the last %d:"
                    % (len(failures), len(shown)), role=blocks.DIM))
            elif wanted is not None or needle:
                result.add(blocks.Text("%d matching calls failed:"
                                       % len(failures), role=blocks.DIM))
            result.add(blocks.Text(native.failure_lines(shown)))
            result.add(blocks.Blank())
        elif wanted is not None or needle:
            result.add(blocks.Text("no failing call matched", role=blocks.DIM))
            result.add(blocks.Blank())
        lines = native.trace_lines(numbers)
        other = [line for line in rest
                 if not line.startswith(native.FAILURE_PREFIX)]
        if other:
            if lines:
                lines.append("")
            lines.extend(other[-6:])
        if lines:
            result.add(blocks.Text(lines))
        result.add(blocks.Summary(_trace_sentence(numbers, total)))
        return result


# how many of a long run's failures are worth putting on a phone screen
_TRACE_FAILURES = 25


def _trace_filter(args):
    """(the command, the errno asked for, the text asked for).

    `uv tool install` failed 3997 calls and stopped on one of them. The last 25
    were a program looking for files it did not expect to find, and the call
    that mattered had scrolled off long before — so the trace has to be
    askable: `--errno EEXIST`, `--grep site-packages`.
    """
    rest = []
    code = None
    needle = None
    words = list(args)
    at = 0
    while at < len(words):
        word = words[at]
        value = None
        if word in ("--errno", "-e", "--grep", "-g"):
            value = words[at + 1] if at + 1 < len(words) else None
            at += 2
        elif word.startswith("--errno="):
            value, word = word.split("=", 1)[1], "--errno"
            at += 1
        elif word.startswith("--grep="):
            value, word = word.split("=", 1)[1], "--grep"
            at += 1
        else:
            # everything from the first ordinary word on is the command, so
            # `rootfs trace uv tool install --grep x` traces uv's own flag
            rest = words[at:]
            break
        if value is None:
            raise CommandError("%s needs a value" % word)
        if word in ("--errno", "-e"):
            code = native.errno_number(value)
            if code is None:
                raise CommandError("no errno called %s" % value,
                                   hint="a number works too: --errno 17")
        else:
            needle = value
    return rest, code, needle


def _trace_sentence(numbers, total):
    """How much of the guest's life this is.

    The count is the part that tells you where you are: a handful of calls is a
    guest that died starting up, and thousands is one that lived.
    """
    if total is None:
        return "the last calls are what the guest was doing"
    if total <= len(numbers):
        return ("%d calls in all — the guest died during its own startup"
                % total)
    return ("%d calls in all, the last %d shown"
            % (total, len(numbers)))


class LaunchCommand(Command):
    """How to start a program that lives inside the rootfs.

    A separate question from `check`, and only answerable once something is
    installed: Alpine's binaries are musl's and this device's linker is
    bionic's, so the way to start one has to be found rather than assumed.
    """

    name = "launch"
    summary = "find how guest programs can be started"
    usage = "rootfs probe launch"

    def run(self, ctx, args):
        root = _root(ctx)
        if not layout.installed(root):
            raise CommandError("no rootfs is installed",
                               hint="rootfs install alpine")
        found = _linker(ctx)
        shell = layout.shell_in(root) or "/bin/sh"
        results = guest.probe(root, found, _runner(ctx), shell=shell,
                              native_dir=_native_dir(ctx),
                              blocked=_blocked(ctx))
        pick = guest.chosen(results)
        if pick:
            # remembered in the rootfs, so the console needs no rediscovery
            layout.save_strategy(root, pick)
        lines = guest.summary_lines(results)
        matrix = lines[:lines.index("")] if "" in lines else lines
        return blocks.Result([
            blocks.Text(matrix),
            blocks.Blank(),
            blocks.Summary(lines[-1],
                           role=blocks.SUCCESS if pick else blocks.ERROR),
        ])


class WritesCommand(Command):
    """How a package manager may write a file here.

    `apk update` fetched its index and then said "Permission denied"; the trace
    named `linkat`, once per repository. apk-tools 3 writes a file the modern
    way — O_TMPFILE, then link it into place through /proc/self/fd — and
    whether that is allowed is a fact about this device rather than something
    to reason out. So it is tried, from this process, which has the same uid
    and the same SELinux domain as the guest.
    """

    name = "writes"
    summary = "how a file may be written here"
    usage = "rootfs probe writes"
    mutating = True

    def run(self, ctx, args):
        root = _root(ctx)
        paths = ctx.require("paths")
        directory = root if layout.installed(root) else paths.tmp_dir()
        results = writes.run(directory)
        writes.save(paths.state_dir(), results)
        ok, sentence = writes.verdict(results)
        return blocks.Result([
            blocks.Text(writes.summary_lines(results)),
            blocks.Blank(),
            blocks.Summary(sentence,
                           role=blocks.SUCCESS if ok else blocks.WARN),
        ])


class MountsCommand(Command):
    """What the shell can see, and where it opens.

    Read-only on purpose. The switches live in the plugin's settings, where the
    rule that one has to stay on can be enforced against a widget the user is
    holding; a command that could turn the last one off from here would need
    the same rule again in a second place.
    """

    name = "mounts"
    summary = "which paths the shell can see"
    usage = "rootfs mounts"

    def run(self, ctx, args):
        values = _mount_values(ctx)
        hosts = _mount_hosts(ctx)
        rows = []
        for item in mounts_module.MOUNTS:
            on = bool(values.get(item.key))
            rows.append("[%s] %-12s %s" % ("+" if on else " ", item.guest,
                                           hosts.get(item.key) or "?"))
        result = blocks.Result([blocks.Text(rows), blocks.Blank()])
        if not values.get(mounts_module.ROOT):
            result.add(blocks.Text(
                "Alpine is not mounted, so its files are not somewhere you can "
                "go — its programs still run, because they find their own "
                "libraries without asking this shell.", role=blocks.DIM))
            result.add(blocks.Blank())
        result.add(blocks.Summary(
            "the shell opens in %s — change these in the plugin's settings"
            % mounts_module.start(values)))
        return result


class CommandsCommand(Command):
    name = "commands"
    summary = "what the rootfs offers"
    usage = "rootfs commands [prefix]"

    def run(self, ctx, args):
        backend = _backend(ctx)
        names = backend.commands()
        if args:
            names = [name for name in names if name.startswith(args[0])]
        if not names:
            return blocks.summary("nothing matches")
        return blocks.Result([
            blocks.Text(" ".join(names)),
            blocks.Summary("%d programs" % len(names)),
        ])


def _backend(ctx):
    root = _root(ctx)
    if not layout.installed(root):
        raise CommandError("no rootfs is installed",
                           hint="rootfs install <tarball> first")
    strategy = layout.saved_strategy(root)
    if not strategy:
        raise CommandError("it is not known how to start guest programs yet",
                           hint="run `rootfs probe launch` once")
    values = _mount_values(ctx)
    return backend_module.RootfsBackend(
        root, _linker(ctx), strategy, native_dir=_native_dir(ctx),
        blocked=_blocked(ctx),
        mount_rows=mounts_module.table(values, _mount_hosts(ctx)),
        start=mounts_module.start(values),
        no_tmpfile=_no_tmpfile(ctx))


def _write_resolver(root):
    """Gives the guest somewhere to ask about names.

    A minirootfs has no /etc/resolv.conf and Android has none to copy, so
    without this every fetch fails with a DNS error that sounds like a network
    fault. Written here and again whenever the console opens, because which
    resolver a phone uses changes with the network it is on.
    """
    try:
        from ...compat import network as compat_network

        return network.write(root, compat_network.dns_servers())
    except Exception:
        return network.write(root, ())


def _no_tmpfile(ctx):
    """Should the guest be told this filesystem has no unnamed files?

    Measured once and kept, like the syscall scan. apk creates its downloads
    with O_TMPFILE and links them into place, and on this device that link
    crosses a mount boundary and comes back EXDEV — so it fetches an index it
    can never put anywhere. Told there are no unnamed files, it writes a named
    temporary and renames it, which works.
    """
    paths = ctx.require("paths")
    state = paths.state_dir()
    results = writes.load(state)
    if results is None:
        root = _root(ctx)
        directory = root if layout.installed(root) else paths.tmp_dir()
        results = writes.run(directory)
        writes.save(state, results)
    return writes.needs_named_temporary(results)


def _mount_values(ctx):
    """Which of the four paths are on. Falls back to all of them where the
    plugin's settings cannot be read, which is every test that does not care."""
    try:
        from ...ui import prefs

        return prefs.mount_values()
    except Exception:
        return mounts_module.defaults()


def _mount_hosts(ctx):
    paths = ctx.require("paths")
    return {
        mounts_module.ROOT: paths.rootfs_dir(),
        mounts_module.SDCARD: getattr(paths, "storage_dir", lambda: "/sdcard")(),
        mounts_module.EXTERA: paths.files_dir(),
        mounts_module.EXTCLI: paths.data_dir(),
        # was missing, so `rootfs mounts` reported four paths while the console
        # was built with five and the guest could reach one nothing listed
        mounts_module.PATCH: paths.patch_dir(),
    }


def _native_dir(ctx):
    """Where extCLI's own binaries live — the loader among them."""
    paths = ctx.require("paths")
    host = ctx.require("host")
    return native.directory(paths.res_dir(),
                            getattr(host, "abi", lambda: None)())


def _runner(ctx, timeout=20):
    """Runs a guest command, with the environment the loader needs."""
    del ctx
    return runners.with_env(timeout)


class SourcesCommand(Command):
    name = "images"
    summary = "root filesystems bundled with the plugin"
    usage = "rootfs images"

    def run(self, ctx, args):
        paths = ctx.require("paths")
        host = ctx.require("host")
        abi = getattr(host, "abi", lambda: None)()
        res = paths.res_dir()
        rows = [source.as_row(res, abi) for source in sources.BUNDLED]
        return blocks.Result([
            blocks.Fields(rows),
            blocks.Summary("rootfs install <name>, or a path to a tarball"),
        ])


class InstallCommand(Command):
    """`rootfs install alpine`, or `rootfs install <path to a tarball>`.

    A name means one of the archives that ship with the plugin, and those are
    checked against a recorded checksum before anything is unpacked: a
    truncated asset makes a rootfs that fails much later and far less clearly.
    """

    name = "install"
    summary = "unpack a bundled rootfs, or a tarball"
    usage = "rootfs install alpine\nrootfs install <tarball> [--force]"
    mutating = True

    def run(self, ctx, args):
        flags = parse_flags(args, {"--force": "bool", "-f": "bool"})
        if not flags.positional:
            raise CommandError(
                "rootfs install needs a name or a tarball",
                hint="rootfs install alpine  ·  `rootfs images` lists them")
        raw = flags.positional[0]
        tarball = self._resolve(ctx, raw)

        root = _root(ctx)
        force = flags.has("--force") or flags.has("-f")
        if layout.installed(root) and not force:
            raise CommandError("a rootfs is already installed at %s" % root,
                               hint="rootfs install %s --force to replace it"
                                    % raw)
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.FS_WRITE, "unpack %s" % raw,
                           assume_yes=ctx.assume_yes)
        try:
            report = install_module.install(tarball, root)
        except Exception as e:
            raise CommandError("could not unpack: %s: %s" % (type(e).__name__, e))

        result = blocks.Result([blocks.Text(report.lines())])
        if not layout.installed(root):
            result.add(blocks.Error(
                "the tarball unpacked but does not look like a root filesystem",
                hint="expected %s at the top level"
                     % ", ".join(layout.REQUIRED)))
            return result
        _write_resolver(root)
        result.add(blocks.Summary("installed at %s — now run `rootfs probe launch`"
                                  % root, role=blocks.SUCCESS))
        return result

    def _resolve(self, ctx, raw):
        """A bundled name, or a path. Names win, and are checked first."""
        source = sources.find(raw)
        if source is None:
            env = getattr(ctx, "env", None)
            path = env.host(raw) if env is not None else raw
            if not os.path.isfile(path):
                raise CommandError(
                    "no such file, and no bundled rootfs called %r" % raw,
                    hint="`rootfs images` lists the bundled ones")
            return path

        paths = ctx.require("paths")
        host = ctx.require("host")
        abi = getattr(host, "abi", lambda: None)()
        if not source.supports(abi):
            raise CommandError(
                "%s is built for %s, this device is %s"
                % (source.name, "/".join(source.abis), abi or "unknown"))
        ok, detail = sources.verify(source, paths.res_dir())
        if not ok:
            raise CommandError(detail,
                               hint="the plugin archive may be damaged; "
                                    "reinstall it")
        return source.path(paths.res_dir())

    def complete(self, ctx, args):
        if len(args) > 1:
            return []
        prefix = args[0] if args else ""
        return [name for name in sources.names() if name.startswith(prefix)]


class RemoveCommand(Command):
    """Deletes the container, or everything extCLI has ever written.

    The second is here because there is nowhere else for it: the plugin keeps
    its data outside its own directory so an update does not throw away an
    Alpine somebody has spent an evening on, and the price of that is that
    removing the plugin leaves it behind. The same two are in the settings.
    """

    name = "remove"
    summary = "delete the rootfs, or everything extCLI has written"
    usage = "rootfs remove [--all]"
    mutating = True

    def run(self, ctx, args):
        from ...utils import purge

        flags = parse_flags(args, {"--all": "bool"})
        everything = flags.has("--all")
        paths = ctx.require("paths")
        targets = [paths.data_dir() if everything else _root(ctx)]
        sentence, files, _total = purge.describe(targets)
        if not files:
            return blocks.summary("nothing to delete in %s" % targets[0])
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.FS_DELETE, "%s (%s)" % (targets[0], sentence),
                           assume_yes=ctx.assume_yes)
        result = purge.remove(targets, keep=_never_delete(paths))
        if not result.ok:
            raise CommandError(result.sentence())
        try:
            # the plugin still has to be able to write; the tree goes back
            paths.ensure_dirs()
        except Exception:
            pass
        return blocks.summary(result.sentence(), role=blocks.SUCCESS)


def _never_delete(paths):
    """Directories a bug must not be able to take with it."""
    found = []
    for name in ("files_dir", "storage_dir", "plugin_root"):
        try:
            found.append(getattr(paths, name)())
        except Exception:
            continue
    return found


class SetupCommand(Command):
    """Everything a fresh install needs, in one command.

    The same sequence the plugin runs by itself in the background when it is
    first loaded. It is here for the times that did not work — no space on the
    phone, the app killed halfway through — and to say plainly what it did.
    """

    name = "setup"
    summary = "unpack and measure everything a rootfs needs"
    usage = "rootfs setup [name]"
    mutating = True

    def run(self, ctx, args):
        from ...rootfs import setup as rootfs_setup

        paths = ctx.require("paths")
        host = ctx.require("host")
        res, state = paths.res_dir(), paths.state_dir()
        root = paths.rootfs_dir()
        todo = rootfs_setup.pending(res, state, root)
        if not todo:
            return blocks.Result([
                blocks.Text(rootfs_setup.Report().lines()),
                blocks.Summary("everything is already done", role=blocks.DIM),
            ])
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.FS_WRITE, "prepare the rootfs",
                           assume_yes=ctx.assume_yes)
        source = args[0] if args else "alpine"
        # each step as it starts, not all of them at the end: unpacking a
        # rootfs and scanning every syscall number take long enough that a
        # console with nothing on it looks like a console that has hung
        say = ctx.live_text
        report = rootfs_setup.prepare(
            res, state, root, abi=getattr(host, "abi", lambda: None)(),
            linker=_linker(ctx), dns=_dns_servers(), source=source,
            on_step=None if say is None else
            (lambda name, label: say("... %s\n" % label)))
        result = blocks.Result([blocks.Text(report.lines())])
        failed = report.failure()
        if failed:
            result.add(blocks.Summary("%s: %s" % failed, role=blocks.ERROR))
        else:
            result.add(blocks.Summary(
                "ready — guest commands work now", role=blocks.SUCCESS))
        return result

    def complete(self, ctx, args):
        if len(args) > 1:
            return []
        prefix = args[0] if args else ""
        return [name for name in sources.names() if name.startswith(prefix)]


def _dns_servers():
    try:
        from ...compat import network as compat_network

        return compat_network.dns_servers()
    except Exception:
        return ()


class ToolsCommand(Command):
    """What is in the container besides Alpine.

    The tools are not bundled with the plugin: the smallest group is thirteen
    megabytes compressed and the plugin is four, and the same bytes would come
    down again on every update. They are offered once, when the container is
    ready — this is here to see what is there and to fetch a group later.
    """

    name = "pkg"
    summary = "the toolsets in the container"
    usage = "rootfs pkg [add <group>...]"
    mutating = True

    def run(self, ctx, args):
        if args and args[0] == "add":
            return self._add(ctx, args[1:])
        rows = toolbox.describe(_root(ctx))
        return blocks.Result([
            blocks.Text(rows),
            blocks.Blank(),
            blocks.Summary("`rootfs pkg add python` fetches one"),
        ])

    def _add(self, ctx, names):
        """`rootfs pkg add <toolset or package>...`

        Both, because both are what somebody means: `add python` is a toolset
        and `add nano` is one program, and having to know which of the two a
        word is would be a rule to learn for no reason.
        """
        if not names:
            raise CommandError("name a toolset or a package",
                               hint="toolsets: %s"
                                    % ", ".join(packages_module.NAMES))
        paths = ctx.require("paths")
        host = ctx.require("host")
        root = _root(ctx)
        chosen, unknown = _wanted_groups(names)
        if unknown:
            raise CommandError("never heard of: %s" % ", ".join(unknown),
                               hint="toolsets: %s"
                                    % ", ".join(packages_module.NAMES))
        selection = packages_module.Selection(
            chosen, satisfied=toolbox.usable(root))
        missing = [name for name in chosen if not selection.is_on(name)]
        if missing:
            needs = set()
            for name in missing:
                needs.update(selection.needs_of(name))
            raise CommandError(
                "%s cannot be installed yet" % ", ".join(missing),
                hint="install %s first" % " or ".join(sorted(needs))
                     if needs else None)
        if not toolbox.anything_to_do(root, selection):
            return blocks.summary("everything in %s is already there"
                                  % ", ".join(names))
        policy = ctx.services.policy
        if policy is not None:
            policy.require(policy.NETWORK, selection.sentence(),
                           assume_yes=ctx.assume_yes)
        card = self._card(ctx)
        outcome = toolbox.install(
            paths.res_dir(), paths.state_dir(), root,
            getattr(host, "abi", lambda: None)(), _linker(ctx), selection,
            on_output=ctx.live_text,
            on_progress=None if card is None else card.update,
            runner=_backend(ctx))
        if card is not None:
            try:
                card.finish(text=outcome.sentence(), ok=outcome.ok)
            except Exception:
                pass
        if not outcome.ok:
            raise CommandError(outcome.sentence())
        return blocks.summary(outcome.sentence(), role=blocks.SUCCESS)

    def _card(self, ctx):
        """The progress card, where there is a screen to put one on.

        This takes minutes over a phone's connection, and whoever started it is
        entitled to walk away from the console and still know it is going.
        """
        if ctx.progress is None:
            return None
        try:
            return ctx.progress("Installing tools")
        except Exception:
            return None

    def complete(self, ctx, args):
        if not args:
            return ["add"]
        words = list(packages_module.NAMES)
        for item in packages_module.GROUPS:
            words.extend(item.names)
        return [word for word in words if word.startswith(args[-1])]


def _wanted_groups(names):
    """({group: [packages]}, what was not recognised) for a list of words."""
    chosen = {}
    unknown = []
    for word in names:
        item = packages_module.group(word)
        if item is not None:
            chosen[item.name] = list(item.names)
            continue
        holder = None
        for candidate in packages_module.GROUPS:
            if candidate.package(word) is not None:
                holder = candidate
                break
        if holder is None:
            unknown.append(word)
            continue
        picked = chosen.setdefault(holder.name, [])
        if word not in picked:
            # kept in the order the group lists them, however they were asked
            # for, so what is printed and what is installed read the same
            picked[:] = [name for name in holder.names
                         if name in picked or name == word]
    return chosen, unknown


def build():
    # The six probes are one subject and belong under one word. They also read
    # as questions rather than as things to look at, which `rootfs writes` did
    # not — it sounded like a listing and is a measurement.
    probe = Group("probe", "measure what this device allows", [
        CheckCommand(),
        DirsCommand(),
        NativeCommand(),
        SyscallsCommand(),
        LaunchCommand(),
        WritesCommand(),
    ])
    return Group("rootfs", "a Linux root filesystem", [
        StatusCommand(),
        SetupCommand(),
        ToolsCommand(),
        probe,
        TraceCommand(),
        CommandsCommand(),
        MountsCommand(),
        SourcesCommand(),
        InstallCommand(),
        RemoveCommand(),
    ])
