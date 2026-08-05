# SPDX-License-Identifier: Apache-2.0

"""Starting a program that lives inside the rootfs.

An Alpine binary is linked against musl and names `/lib/ld-musl-aarch64.so.1`
as its interpreter. Android's linker is bionic's. Handing a musl binary to
bionic's linker and hoping is not a plan, so there are three candidate ways to
start one and the device says which of them works:

  loader        /system/bin/linker64 <our loader> <guest binary>
                our own ELF loader does the kernel's job — maps the segments,
                loads the guest's own interpreter, builds a stack and an auxv,
                jumps. The guest starts the way Linux starts programs, so its
                libc initialises normally whichever libc it is

  direct        /system/bin/linker64 <guest binary>
                bionic's linker maps the ELF and resolves its DT_NEEDED itself

  direct+path   the same, with LD_LIBRARY_PATH into the rootfs, so bionic can
                find musl's libc where the guest keeps it

  musl          /system/bin/linker64 <rootfs>/lib/ld-musl-*.so.1 <guest binary>
                bionic starts musl's loader and musl starts the program

  musl+path     the same, with the library path set

The device has already ruled on two of these. `direct` failed because a guest
path was handed over unresolved — Alpine's /bin/sh is an absolute symlink to
/bin/busybox, and the host followed it out of the rootfs. That was ours to fix.
`musl` failed with "Could not find a PHDR: broken executable?", which is
bionic's linker refusing musl's loader: musl's ld.so is a static PIE with no
PT_PHDR, because nothing was ever supposed to load it.

Whichever one answers is remembered inside the rootfs, and every guest command
afterwards is built the same way.
"""

import os

from . import layout

MARKER = "extcli-guest"

OK = "ok"
BLOCKED = "blocked"
UNKNOWN = "unknown"

LOADER = "loader"
DIRECT = "direct"
DIRECT_PATH = "direct+path"
MUSL = "musl"
MUSL_PATH = "musl+path"

# The loader first: it is the only one measured to work with a foreign libc,
# and the others are kept because they are cheaper when they do work and
# because another distribution's binaries may behave differently.
ORDER = (LOADER, DIRECT, DIRECT_PATH, MUSL, MUSL_PATH)

LOADER_NAME = "loader"
# The loader searches argv for this rather than counting from argv[1]: how many
# arguments bionic's linker leaves in front of ours is not ours to decide, and
# counting worked under qemu and opened the wrong argument on the device.
LOADER_SENTINEL = "extcli-loader-v1"

# the two that put musl's own loader in the middle
THROUGH_LOADER = (MUSL, MUSL_PATH)
# the ones that point a loader at the rootfs's libraries. The loader strategy
# needs it too: a guest's own interpreter searches /lib as the host writes it,
# and apk could not find libapk until it was told where the rootfs keeps it.
WITH_LIBRARY_PATH = (LOADER, DIRECT_PATH, MUSL_PATH)

# musl names its loader after the architecture
LOADER_NAMES = (
    "lib/ld-musl-aarch64.so.1",
    "lib/ld-musl-armhf.so.1",
    "lib/ld-musl-x86_64.so.1",
    "lib/ld-musl-i386.so.1",
)

LIB_DIRS = ("lib", "usr/lib")


def loader_in(root):
    """musl's own loader inside the rootfs, as a host path, or None."""
    for name in LOADER_NAMES:
        resolved = layout.resolve(root, "/" + name)
        if resolved is not None:
            return resolved
    return None


def library_path(root):
    return ":".join(os.path.join(root, name) for name in LIB_DIRS)


def command_for(strategy, root, linker, argv, loader=None, native_dir=None,
                argv0=None):
    """The real argv for one way of starting a guest program.

    Pure apart from following symlinks, which is the part that has to be exactly
    right: a guest path is resolved the way the guest would resolve it, so an
    absolute link inside the rootfs stays inside it.
    """
    if not argv:
        return None
    wanted = argv[0]
    if not wanted.startswith("/"):
        # A guest program is named by its place in the guest, and a caller that
        # has not looked one up has nothing to run. Passing the bare name on
        # meant the loader was handed `apk` and answered "cannot open: apk",
        # which reads as a broken rootfs rather than as a caller's omission.
        return None
    host_path = layout.resolve(root, wanted)
    if host_path is None:
        return None
    rest = list(argv[1:])

    if strategy == LOADER:
        if not native_dir:
            return None
        tool = os.path.join(native_dir, LOADER_NAME)
        if not os.path.isfile(tool):
            return None
        # argv0 is given separately because a multi-call binary is a different
        # program depending on it: busybox named `ls` lists a directory
        return [linker, tool, LOADER_SENTINEL, host_path,
                argv0 or _basename(wanted)] + rest
    if strategy in (DIRECT, DIRECT_PATH):
        return [linker, host_path] + rest
    loader = loader or loader_in(root)
    if loader is None:
        return None
    if strategy in THROUGH_LOADER:
        return [linker, loader, host_path] + rest
    return None


def _basename(path):
    return path.rsplit("/", 1)[-1]


# Paths the guest must reach on the host rather than in its rootfs. musl reads
# /proc/self/fd, /dev/null and /dev/urandom before it does anything else, and a
# rootfs has nothing but empty directories there. /storage is here because it is
# where /sdcard really leads, so a program that resolves one and reopens the
# answer does not find itself back inside the rootfs.
#
# The phone's own directories are not here: they are mounts, named and switched
# in the plugin's settings, and this list is the part nobody chooses.
PASS_THROUGH = ("/proc", "/sys", "/dev", "/storage")

# What the guest's own libraries are called once / means the rootfs.
GUEST_LIBRARY_PATH = "/lib:/usr/lib"


def environment_for(strategy, root, base=None, blocked=None, translate=True,
                    mount_rows=None, no_tmpfile=False, linker=None,
                    native_dir=None, home=None):
    """The environment that goes with a strategy.

    `blocked` is what this device's filter refuses, measured by `rootfs
    syscalls`. Only the loader can act on it, and only it is told: the other
    strategies hand the guest straight to the kernel and have nowhere to stand
    between the two. The same goes for translating paths, which is why the
    library path is written two different ways here — a guest whose `/` is its
    rootfs asks for /lib, and one without translation has to be handed the host
    path or it finds nothing.
    """
    env = dict(base or {})
    mapping = bool(translate and strategy == LOADER and root)
    if strategy in WITH_LIBRARY_PATH:
        env["LD_LIBRARY_PATH"] = GUEST_LIBRARY_PATH if mapping \
            else library_path(root)
    if strategy == LOADER:
        # how the loader turns the guest's own /lib/ld-musl... into a real path
        env["EXTCLI_ROOT"] = root
        if blocked:
            env["EXTCLI_BLOCKED"] = blocked
        if no_tmpfile:
            # this device cannot link an unnamed file into place, so the guest
            # is told it has none — which is what every fallback is written for
            env["EXTCLI_NO_TMPFILE"] = "1"
        if linker and native_dir:
            # How the guest's own exec is to be done, since the device refuses
            # the way it would do it itself. Neither of these is something the
            # loader could work out from inside.
            env["EXTCLI_EXEC"] = "%s|%s" % (
                linker, os.path.join(native_dir, LOADER_NAME))
        if mapping:
            from . import mounts

            rows = list(mount_rows or [("/", root)])
            if not any(guest == "/" for guest, _ in rows):
                # the rootfs is how a program finds its own libraries, so it is
                # in the table whether or not the user can see it
                rows.insert(0, ("/", root))
            env["EXTCLI_MOUNTS"] = mounts.encode(rows)
            env["EXTCLI_PASS"] = ":".join(PASS_THROUGH)
            # A guest inherits this process's environment, and the app's own
            # HOME is a directory on the phone. uv believed it: it put its
            # tools and its cache in $HOME/.local and $HOME/.cache, which is
            # /data/user/0/<package>/files — a real path, which then went
            # through the translation like any other and built a shadow of the
            # phone's directories inside the rootfs.
            #
            # A guest's home is in the guest. So are its temporary files and
            # its idea of who it is.
            env["HOME"] = home or mounts.HOME
            env["PWD"] = home or mounts.HOME
            env["TMPDIR"] = "/tmp"
            env["USER"] = env["LOGNAME"] = "root"
            env["SHELL"] = "/bin/sh"
    return env


# the ones worth naming; a number alone says nothing about what happened
SIGNALS = {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL",
           11: "SIGSEGV", 31: "SIGSYS"}


def describe_exit(code):
    """`exit -11` is a crash, and should say so."""
    code = int(code)
    if code >= 0:
        return "exit %d" % code
    number = -code
    return "killed by %s" % SIGNALS.get(number, "signal %d" % number)


def read_attempt(code, out, err):
    """Did the guest program run? Pure; the strings come from a device."""
    text = "%s\n%s" % (out or "", err or "")
    if MARKER in text:
        return OK, "guest ran"
    lowered = text.lower()
    for needle in ("permission denied", "not permitted",
                   "cannot execute", "can't execute", "exec format error",
                   "no such file", "not found", "symbol not found",
                   "cannot locate symbol", "unable to find", "segmentation",
                   "bad system call"):
        if needle in lowered:
            return BLOCKED, _first_line(text) or needle
    if code == 0:
        return UNKNOWN, _first_line(text) or "exit 0 without output"
    return BLOCKED, _first_line(text) or describe_exit(code)


# what a failure was actually about, in the order it becomes knowable
NO_LIBRARIES = "libraries"
FOREIGN_LIBC = "libc"
LOADER_REFUSED = "loader"
SANDBOX = "sandbox"


def diagnose(results):
    """Why nothing worked, in terms of what could be done instead.

    Four red lines are not four problems. They are one problem seen from four
    angles, and which angle got furthest is the thing worth reading.
    """
    def detail(strategy):
        return (results.get(strategy, {}).get("detail") or "").lower()

    # SIGSYS first: it is also a "killed by", and it means something entirely
    # different from a crash — the program was running well enough to make a
    # syscall, and Android's filter refused that one syscall
    if any("sigsys" in detail(strategy) for strategy in ORDER):
        return SANDBOX, (
            "the guest started and the sandbox refused one of its syscalls. "
            "`rootfs syscalls` lists what this app is allowed to call")
    crashed = any("killed by" in detail(strategy) for strategy in ORDER)
    if crashed:
        return FOREIGN_LIBC, (
            "the rootfs loaded and then crashed: its libc is not bionic's and "
            "cannot be started by bionic's linker. Only binaries built against "
            "bionic can run from the plugin's own directory on this device")
    if any("not found: needed by" in detail(strategy) for strategy in ORDER):
        return NO_LIBRARIES, (
            "bionic's linker read the guest binary but could not find the "
            "libraries it names")
    if all("phdr" in detail(strategy) for strategy in THROUGH_LOADER):
        return LOADER_REFUSED, (
            "bionic's linker will not start the guest's own loader")
    return None, "no way of starting a guest program worked"


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


def probe(root, linker, runner, shell="/bin/sh", native_dir=None,
          blocked=None):
    """Tries each way of starting a guest program. Returns a result per name."""
    results = {}
    loader = loader_in(root)
    for strategy in ORDER:
        if strategy in THROUGH_LOADER and loader is None:
            results[strategy] = {"status": UNKNOWN,
                                 "detail": "no musl loader in the rootfs"}
            continue
        argv = [shell, "-c", "echo %s" % MARKER]
        command = command_for(strategy, root, linker, argv, loader,
                              native_dir=native_dir, argv0="sh")
        if command is None:
            results[strategy] = {"status": UNKNOWN, "detail": "cannot be built"}
            continue
        code, out, err = runner(command,
                                environment_for(strategy, root,
                                                blocked=blocked))
        status, detail = read_attempt(code, out, err)
        results[strategy] = {"status": status, "detail": detail}
    return results


def chosen(results):
    """The strategy to use from now on, or None if none of them worked."""
    for strategy in ORDER:
        if results.get(strategy, {}).get("status") == OK:
            return strategy
    return None


def summary_lines(results):
    labels = {
        LOADER: "extCLI's own ELF loader",
        DIRECT: "bionic's linker alone",
        DIRECT_PATH: "bionic's linker, libraries pointed at",
        MUSL: "through musl's loader",
        MUSL_PATH: "musl's loader, libraries pointed at",
    }
    marks = {OK: "+", BLOCKED: "x", UNKNOWN: "?"}
    lines = []
    for strategy in ORDER:
        result = results.get(strategy, {})
        lines.append("[%s] %-34s %s"
                     % (marks.get(result.get("status"), "?"), labels[strategy],
                        result.get("detail", "")))
    lines.append("")
    pick = chosen(results)
    if pick:
        lines.append("guest programs start: %s" % labels[pick])
    else:
        lines.append(diagnose(results)[1])
    return lines
