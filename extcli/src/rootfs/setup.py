# SPDX-License-Identifier: Apache-2.0

"""Getting a rootfs ready without being asked.

Everything here was a command the user had to know to run: `rootfs probe syscalls`,
`rootfs install alpine`, `rootfs probe launch`, `rootfs probe writes`. Each of them exists
because the answer is a measurement rather than an assumption, and none of them
is something a person installing a plugin should have to have heard of. So they
run themselves, once, in the background, and `apk` works the first time the
console is opened.

The order is not arbitrary:

  syscalls  what the device's filter refuses, which the loader needs before it
            can start anything without being killed
  unpack    the bundled rootfs
  writes    whether a file can be linked into place here, which apk needs to
            know before it fetches an index it cannot put anywhere
  launch    which of the ways of starting a guest program works on this device
  dns       a resolver to ask, which a minirootfs does not come with

Nothing here imports the client. What it needs from the phone — the ABI, the
linker, the resolvers — arrives as arguments, so the whole sequence runs in the
tests against a rootfs built in a temporary directory.
"""

import os

from . import guest, install as install_module, layout, native, network
from . import sandbox, sources, writes

# in the order they have to happen
STEPS = ("syscalls", "unpack", "writes", "launch", "dns")

LABELS = {
    "syscalls": "measuring what the sandbox refuses",
    "unpack": "unpacking the rootfs",
    "writes": "measuring how a file can be written",
    "launch": "finding how guest programs start",
    "dns": "writing a resolver",
}

# what one step of the scan may take. The syscall map asks about every number
# there is, one child process each, and a slow phone is still a phone.
SCAN_TIMEOUT = 300

# roughly how long each step takes against the others, for a bar that moves at
# something like a steady speed. Measured on the device this was written for:
# the scan is a child process per syscall number and the unpacking is five
# hundred files, and the other three are over before they are noticed.
WEIGHTS = {
    "syscalls": 50.0,
    "unpack": 40.0,
    "writes": 3.0,
    "launch": 5.0,
    "dns": 2.0,
}

# the highest number the syscall map asks about; it prints the refusals in
# order, so the last one printed says roughly how far it has got
LAST_SYSCALL = 462


class Progress(object):
    """Where a bar should be, given which steps are actually being done.

    Only the pending ones count. A setup with nothing left but the resolver
    would otherwise start its bar at ninety-odd per cent and jump to full,
    which tells the person watching nothing at all.
    """

    def __init__(self, todo):
        self.todo = [step for step in STEPS if step in (todo or ())]
        self.total = sum(WEIGHTS.get(step, 1.0) for step in self.todo) or 1.0

    def at(self, step, inner=0.0):
        """0..1 overall, `inner` being how far through this step it is."""
        if inner < 0:
            inner = 0.0
        elif inner > 1:
            inner = 1.0
        position = STEPS.index(step) if step in STEPS else len(STEPS)
        before = 0.0
        for name in self.todo:
            if name == step:
                return (before + WEIGHTS.get(name, 1.0) * inner) / self.total
            if STEPS.index(name) < position:
                before += WEIGHTS.get(name, 1.0)
        # a step nobody is doing: as far as everything before it has got
        return before / self.total


class Report(object):
    """What was done, and what came of it."""

    def __init__(self):
        self.steps = []      # (name, ok, detail)
        self.skipped = []    # names that were already done

    def add(self, name, ok, detail=""):
        self.steps.append((name, bool(ok), detail or ""))

    def skip(self, name):
        self.skipped.append(name)

    @property
    def ok(self):
        return all(ok for _name, ok, _detail in self.steps)

    @property
    def did_anything(self):
        return bool(self.steps)

    def failure(self):
        """The first step that did not work, or None."""
        for name, ok, detail in self.steps:
            if not ok:
                return (name, detail)
        return None

    def lines(self):
        rows = []
        for name, ok, detail in self.steps:
            rows.append("[%s] %-38s %s" % ("+" if ok else "x",
                                           LABELS.get(name, name), detail))
        for name in self.skipped:
            rows.append("[=] %-38s already done" % LABELS.get(name, name))
        return rows


def pending(res_dir, state_dir, root):
    """Which steps still have to happen, in order.

    Each one is judged by what it leaves behind rather than by a flag, so a
    setup interrupted halfway — the app killed, the phone out of space —
    carries on from where it stopped instead of starting again.
    """
    del res_dir
    todo = []
    if sandbox.load(state_dir) is None:
        todo.append("syscalls")
    if not layout.installed(root):
        todo.append("unpack")
    if writes.load(state_dir) is None:
        todo.append("writes")
    if not layout.saved_strategy(root):
        todo.append("launch")
    if not network.servers_in(network.read(root)):
        todo.append("dns")
    return todo


def ready(res_dir, state_dir, root):
    """Can a guest program be started right now?"""
    return not [step for step in pending(res_dir, state_dir, root)
                if step in ("unpack", "launch")]


def prepare(res_dir, state_dir, root, abi=None, linker=None, dns=(),
            source="alpine", on_step=None, on_progress=None,
            run=None, run_with_env=None):
    """Does whatever is not done yet. Never raises.

    Returns a Report. Steps that were already done are not repeated: this is
    called every time the plugin loads, and the second time there is nothing
    to do.

    `on_progress(fraction, label)` is called as it goes, including from inside
    the two steps that take long enough to look stuck — the scan reports where
    it has got to by the numbers it prints, and the unpacking by the files it
    has written.
    """
    from . import runners

    run = run or runners.watching(SCAN_TIMEOUT)
    run_with_env = run_with_env or runners.with_env()
    report = Report()
    todo = pending(res_dir, state_dir, root)
    progress = Progress(todo)

    def say(step, inner=0.0):
        if on_progress is None:
            return
        try:
            on_progress(progress.at(step, inner), LABELS.get(step, step))
        except Exception:
            pass

    for step in STEPS:
        if step not in todo:
            report.skip(step)
            continue
        if on_step is not None:
            try:
                on_step(step, LABELS.get(step, step))
            except Exception:
                pass
        say(step, 0.0)
        try:
            ok, detail = _do(step, res_dir, state_dir, root, abi, linker, dns,
                             source, run, run_with_env, say)
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        report.add(step, ok, detail)
        say(step, 1.0)
        if not ok and step in ("unpack",):
            # nothing after this can mean anything without a rootfs
            break
    if on_progress is not None and report.did_anything:
        try:
            on_progress(1.0, LABELS.get(STEPS[-1], ""))
        except Exception:
            pass
    return report


def _do(step, res_dir, state_dir, root, abi, linker, dns, source, run,
        run_with_env, say):
    if step == "syscalls":
        return _syscalls(res_dir, state_dir, abi, linker, run, say)
    if step == "unpack":
        return _unpack(res_dir, root, abi, source, say)
    if step == "writes":
        return _writes(state_dir, root)
    if step == "launch":
        return _launch(res_dir, state_dir, root, abi, linker, run_with_env)
    if step == "dns":
        return _dns(root, dns)
    return False, "no such step"


def _run_watching(run, command, watch):
    """Runs a command, showing each line to `watch` as it arrives if it can.

    A runner that was given to us — the tests give one — has no way to report
    lines, so this falls back to running it whole and showing the output at the
    end. Nothing depends on the difference except how often the bar moves.
    """
    if watch is None:
        return run(command)
    if getattr(run, "streams", False):
        return run(command, watch)
    code, out, err = run(command)
    for line in (out or "").splitlines():
        try:
            watch(line)
        except Exception:
            pass
    return code, out, err


def _syscalls(res_dir, state_dir, abi, linker, run, say=None):
    """Which numbers the device's filter refuses.

    A failure here is not fatal. Without the answer the loader cannot turn a
    refused call into an answer, and a guest that makes one is killed — but
    plenty of guests never make one, and a rootfs that mostly works beats no
    rootfs at all.
    """
    if not linker:
        return False, "no dynamic linker on this device"
    tool = native.tool(res_dir, abi, native.SYSCALL_MAP)
    if not tool or not os.path.isfile(tool):
        # say which of the two it is: an ABI we do not ship a binary for, or a
        # res/ directory the client did not put where we looked. The second was
        # a real bug — the client serves assets through the SDK rather than
        # unpacking them next to the code — and "not built for arm64-v8a" sent
        # everyone looking in the wrong direction.
        base = native.directory(res_dir, abi)
        if os.path.isdir(base):
            return False, "no syscall map built for %s" % (abi or "this device")
        return False, ("cannot find the bundled binaries (looked in %s)"
                       % base)
    # the tool prints the refused numbers in order, so the last one printed
    # says roughly how far through the table it is — the only progress a scan
    # of one child process per number can report without being asked
    watch = None
    if say is not None:
        def watch(line):
            text = line.strip()
            if text.isdigit():
                say("syscalls", int(text) / float(LAST_SYSCALL))

    code, out, err = _run_watching(run, [linker, tool], watch)
    refused, complete = native.read_syscall_map(out)
    if not complete:
        return False, (err or "exit %s" % code).strip()[:120] or "did not finish"
    sandbox.save(state_dir, refused)
    return True, "%d refused" % len(refused)


def _unpack(res_dir, root, abi, source, say=None):
    found = sources.find(source)
    if found is None:
        return False, "no bundled rootfs called %r" % source
    if not found.supports(abi):
        return False, "%s is built for %s, this device is %s" % (
            found.name, "/".join(found.abis), abi or "unknown")
    ok, detail = sources.verify(found, res_dir)
    if not ok:
        return False, detail
    expected = float(found.entries or 0)
    watch = None
    if say is not None and expected:
        def watch(done):
            say("unpack", done / expected)
    report = install_module.install(found.path(res_dir), root, watch)
    if not layout.installed(root):
        return False, "unpacked, but it is not a root filesystem"
    return True, "%d files" % report.total


def _writes(state_dir, root):
    results = writes.run(root if layout.installed(root) else state_dir)
    writes.save(state_dir, results)
    _ok, sentence = writes.verdict(results)
    return True, sentence


def _launch(res_dir, state_dir, root, abi, linker, run_with_env):
    if not linker:
        return False, "no dynamic linker on this device"
    results = guest.probe(root, linker, run_with_env,
                          shell=layout.shell_in(root) or "/bin/sh",
                          native_dir=native.directory(res_dir, abi),
                          blocked=sandbox.blocked_for(state_dir))
    pick = guest.chosen(results)
    if not pick:
        return False, "no way of starting a guest program works here"
    layout.save_strategy(root, pick)
    return True, pick


def _dns(root, dns):
    """A minirootfs has no /etc/resolv.conf and Android has none to copy, so
    without this every fetch fails with something that sounds like a network
    fault."""
    _written, found = network.write(root, dns or ())
    if not found:
        return False, "no resolver to give the guest"
    return True, ", ".join(found)
