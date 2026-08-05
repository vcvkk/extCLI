# SPDX-License-Identifier: Apache-2.0

"""Can a binary extCLI built itself be started on this device?

Everything measured so far used a copy of `/system/bin/toybox`. That proved
the linker will run a file from our directory, but not that it will run *our*
file — a program Android has never seen, built here, with no libc.

The answer decides whether the loader is worth writing. The loader is the plan:
a small freestanding program that maps a guest ELF, builds it a stack and an
auxv, and jumps to its entry point, so any arm64 rootfs can run whatever its
own libc is. That loader is a bigger program of exactly the same shape as the
probe, so if the probe runs, the shape is sound.

Two shapes are tried, because bionic's linker has already refused one file for
its program headers (musl's loader, for want of a PT_PHDR):

  probe         static PIE, no PT_INTERP
  probe-interp  the same, with PT_INTERP naming the linker, which is what an
                ordinary Android executable looks like
"""

import os

MARKER = "extcli-native-ok"

OK = "ok"
BLOCKED = "blocked"
MISSING = "missing"
UNKNOWN = "unknown"

DIRECTORY = "native"
SHAPES = ("probe", "probe-interp")

# the other tools built alongside the probes
LOADER = "loader"
SYSCALL_MAP = "syscalls"


def tool(res_dir, abi, name):
    return os.path.join(directory(res_dir, abi), name)


# The aarch64 syscall table, whole. It was a short list of numbers "worth
# naming" until the device answered: it refuses 240 of them, and a report that
# is mostly question marks is a report nobody can read on a phone.
SYSCALL_NAMES = {
    0: "io_setup", 1: "io_destroy", 2: "io_submit", 3: "io_cancel",
    4: "io_getevents", 5: "setxattr", 6: "lsetxattr", 7: "fsetxattr",
    8: "getxattr", 9: "lgetxattr", 10: "fgetxattr", 11: "listxattr",
    12: "llistxattr", 13: "flistxattr", 14: "removexattr", 15: "lremovexattr",
    16: "fremovexattr", 17: "getcwd", 18: "lookup_dcookie", 19: "eventfd2",
    20: "epoll_create1", 21: "epoll_ctl", 22: "epoll_pwait", 23: "dup",
    24: "dup3", 25: "fcntl", 26: "inotify_init1", 27: "inotify_add_watch",
    28: "inotify_rm_watch", 29: "ioctl", 30: "ioprio_set", 31: "ioprio_get",
    32: "flock", 33: "mknodat", 34: "mkdirat", 35: "unlinkat", 36: "symlinkat",
    37: "linkat", 38: "renameat", 39: "umount2", 40: "mount", 41: "pivot_root",
    42: "nfsservctl", 43: "statfs", 44: "fstatfs", 45: "truncate",
    46: "ftruncate", 47: "fallocate", 48: "faccessat", 49: "chdir",
    50: "fchdir", 51: "chroot", 52: "fchmod", 53: "fchmodat", 54: "fchownat",
    55: "fchown", 56: "openat", 57: "close", 58: "vhangup", 59: "pipe2",
    60: "quotactl", 61: "getdents64", 62: "lseek", 63: "read", 64: "write",
    65: "readv", 66: "writev", 67: "pread64", 68: "pwrite64", 69: "preadv",
    70: "pwritev", 71: "sendfile", 72: "pselect6", 73: "ppoll",
    74: "signalfd4", 75: "vmsplice", 76: "splice", 77: "tee", 78: "readlinkat",
    79: "newfstatat", 80: "fstat", 81: "sync", 82: "fsync", 83: "fdatasync",
    84: "sync_file_range", 85: "timerfd_create", 86: "timerfd_settime",
    87: "timerfd_gettime", 88: "utimensat", 89: "acct", 90: "capget",
    91: "capset", 92: "personality", 93: "exit", 94: "exit_group",
    95: "waitid", 96: "set_tid_address", 97: "unshare", 98: "futex",
    99: "set_robust_list", 100: "get_robust_list", 101: "nanosleep",
    102: "getitimer", 103: "setitimer", 104: "kexec_load", 105: "init_module",
    106: "delete_module", 107: "timer_create", 108: "timer_gettime",
    109: "timer_getoverrun", 110: "timer_settime", 111: "timer_delete",
    112: "clock_settime", 113: "clock_gettime", 114: "clock_getres",
    115: "clock_nanosleep", 116: "syslog", 117: "ptrace",
    118: "sched_setparam", 119: "sched_setscheduler",
    120: "sched_getscheduler", 121: "sched_getparam", 122: "sched_setaffinity",
    123: "sched_getaffinity", 124: "sched_yield",
    125: "sched_get_priority_max", 126: "sched_get_priority_min",
    127: "sched_rr_get_interval", 128: "restart_syscall", 129: "kill",
    130: "tkill", 131: "tgkill", 132: "sigaltstack", 133: "rt_sigsuspend",
    134: "rt_sigaction", 135: "rt_sigprocmask", 136: "rt_sigpending",
    137: "rt_sigtimedwait", 138: "rt_sigqueueinfo", 139: "rt_sigreturn",
    140: "setpriority", 141: "getpriority", 142: "reboot", 143: "setregid",
    144: "setgid", 145: "setreuid", 146: "setuid", 147: "setresuid",
    148: "getresuid", 149: "setresgid", 150: "getresgid", 151: "setfsuid",
    152: "setfsgid", 153: "times", 154: "setpgid", 155: "getpgid",
    156: "getsid", 157: "setsid", 158: "getgroups", 159: "setgroups",
    160: "uname", 161: "sethostname", 162: "setdomainname", 163: "getrlimit",
    164: "setrlimit", 165: "getrusage", 166: "umask", 167: "prctl",
    168: "getcpu", 169: "gettimeofday", 170: "settimeofday", 171: "adjtimex",
    172: "getpid", 173: "getppid", 174: "getuid", 175: "geteuid",
    176: "getgid", 177: "getegid", 178: "gettid", 179: "sysinfo",
    180: "mq_open", 181: "mq_unlink", 182: "mq_timedsend",
    183: "mq_timedreceive", 184: "mq_notify", 185: "mq_getsetattr",
    186: "msgget", 187: "msgctl", 188: "msgrcv", 189: "msgsnd", 190: "semget",
    191: "semctl", 192: "semtimedop", 193: "semop", 194: "shmget",
    195: "shmctl", 196: "shmat", 197: "shmdt", 198: "socket",
    199: "socketpair", 200: "bind", 201: "listen", 202: "accept",
    203: "connect", 204: "getsockname", 205: "getpeername", 206: "sendto",
    207: "recvfrom", 208: "setsockopt", 209: "getsockopt", 210: "shutdown",
    211: "sendmsg", 212: "recvmsg", 213: "readahead", 214: "brk",
    215: "munmap", 216: "mremap", 217: "add_key", 218: "request_key",
    219: "keyctl", 220: "clone", 221: "execve", 222: "mmap", 223: "fadvise64",
    224: "swapon", 225: "swapoff", 226: "mprotect", 227: "msync", 228: "mlock",
    229: "munlock", 230: "mlockall", 231: "munlockall", 232: "mincore",
    233: "madvise", 234: "remap_file_pages", 235: "mbind",
    236: "get_mempolicy", 237: "set_mempolicy", 238: "migrate_pages",
    239: "move_pages", 240: "rt_tgsigqueueinfo", 241: "perf_event_open",
    242: "accept4", 243: "recvmmsg", 260: "wait4", 261: "prlimit64",
    262: "fanotify_init", 263: "fanotify_mark", 264: "name_to_handle_at",
    265: "open_by_handle_at", 266: "clock_adjtime", 267: "syncfs",
    268: "setns", 269: "sendmmsg", 270: "process_vm_readv",
    271: "process_vm_writev", 272: "kcmp", 273: "finit_module",
    274: "sched_setattr", 275: "sched_getattr", 276: "renameat2",
    277: "seccomp", 278: "getrandom", 279: "memfd_create", 280: "bpf",
    281: "execveat", 282: "userfaultfd", 283: "membarrier", 284: "mlock2",
    285: "copy_file_range", 286: "preadv2", 287: "pwritev2",
    288: "pkey_mprotect", 289: "pkey_alloc", 290: "pkey_free", 291: "statx",
    292: "io_pgetevents", 293: "rseq", 294: "kexec_file_load",
    424: "pidfd_send_signal", 425: "io_uring_setup", 426: "io_uring_enter",
    427: "io_uring_register", 428: "open_tree", 429: "move_mount",
    430: "fsopen", 431: "fsconfig", 432: "fsmount", 433: "fspick",
    434: "pidfd_open", 435: "clone3", 436: "close_range", 437: "openat2",
    438: "pidfd_getfd", 439: "faccessat2", 440: "process_madvise",
    441: "epoll_pwait2", 442: "mount_setattr", 443: "quotactl_fd",
    444: "landlock_create_ruleset", 445: "landlock_add_rule",
    446: "landlock_restrict_self", 447: "memfd_secret",
    448: "process_mrelease", 449: "futex_waitv",
    450: "set_mempolicy_home_node", 451: "cachestat", 452: "fchmodat2",
    453: "map_shadow_stack", 454: "futex_wake", 455: "futex_wait",
    456: "futex_requeue", 457: "statmount", 458: "listmount",
    459: "lsm_get_self_attr", 460: "lsm_set_self_attr",
    461: "lsm_list_modules",
}

# What the table leaves empty. arm64 stops at 294 and starts again at 424, so
# the numbers in between are not syscalls that are refused — there is nothing
# there to refuse. They appear in the refusals anyway, which is the useful
# fact: the filter kills every number it does not recognise, and that is why a
# call it dislikes has to be replaced with another one rather than cancelled.
UNUSED_RANGES = ((244, 259), (295, 423))


def unused(number):
    """Is there no syscall at this number on arm64?"""
    return any(low <= int(number) <= high for low, high in UNUSED_RANGES)


def syscall_name(number):
    return SYSCALL_NAMES.get(int(number), "")


def read_syscall_map(text):
    """(refused, complete) from the map's output.

    The program prints one number per refusal and "end" when it has asked
    everything — so a run cut short by a timeout is told apart from a device
    that refuses nothing, which look identical otherwise.
    """
    refused = []
    complete = False
    for line in (text or "").splitlines():
        line = line.strip()
        if line == "end":
            complete = True
            continue
        if line.isdigit():
            refused.append(int(line))
    return refused, complete


# The errno numbers worth naming. A number alone sends the reader to a table;
# these are the ones a guest on this device actually meets.
ERRNO_NAMES = {
    1: "EPERM", 2: "ENOENT", 5: "EIO", 9: "EBADF", 11: "EAGAIN",
    12: "ENOMEM", 13: "EACCES", 14: "EFAULT", 16: "EBUSY", 17: "EEXIST",
    19: "ENODEV", 20: "ENOTDIR", 21: "EISDIR", 22: "EINVAL", 24: "EMFILE",
    28: "ENOSPC", 30: "EROFS", 32: "EPIPE", 38: "ENOSYS", 39: "ENOTEMPTY",
    40: "ELOOP", 88: "ENOTSOCK", 92: "EPROTONOSUPPORT", 97: "EAFNOSUPPORT",
    98: "EADDRINUSE", 99: "EADDRNOTAVAIL", 101: "ENETUNREACH",
    104: "ECONNRESET", 110: "ETIMEDOUT", 111: "ECONNREFUSED",
    113: "EHOSTUNREACH", 115: "EINPROGRESS",
}

FAILURE_PREFIX = "extcli-loader: failed "


def errno_name(number):
    return ERRNO_NAMES.get(int(number), "")


def errno_number(word):
    """A number from `17` or `EEXIST`, or None.

    An install makes thousands of failing calls and almost all of them are a
    program looking for a file in the places it might be. Being able to ask for
    one errno is the difference between reading a screenful of noise and seeing
    the call that stopped the run.
    """
    if not word:
        return None
    text = str(word).strip()
    if text.isdigit():
        return int(text)
    text = text.upper()
    for number, name in ERRNO_NAMES.items():
        if name == text:
            return number
    return None


def matching_failures(failures, code=None, text=None):
    """The failures worth looking at: by errno, by what the path says, or both."""
    found = []
    for number, errno, path in failures:
        if code is not None and errno != code:
            continue
        if text and text not in path:
            continue
        found.append((number, errno, path))
    return found


def read_failures(text):
    """(syscall, errno, path) for each call the loader saw fail."""
    found = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith(FAILURE_PREFIX):
            continue
        parts = line[len(FAILURE_PREFIX):].split()
        if len(parts) < 3 or parts[1] != "errno":
            continue
        try:
            found.append((int(parts[0]), int(parts[2]),
                          " ".join(parts[3:])))
        except ValueError:
            continue
    return found


def failure_lines(failures):
    """One legible line each: what was called, what came back, about what."""
    lines = []
    for number, code, path in failures:
        name = syscall_name(number) or str(number)
        reason = errno_name(code) or "errno %d" % code
        lines.append("%-16s %-14s %s" % (name, reason, path))
    return lines


TRACE_MARKER = " syscalls, last:"


def read_trace(text):
    """(numbers, how many in all, the rest of what the loader said).

    The count matters as much as the numbers: four calls means the guest died
    during its startup, and four hundred means it got through it and died doing
    something. A list on its own cannot tell those apart, and the first version
    of this report printed one.
    """
    numbers = []
    total = None
    rest = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if TRACE_MARKER in line:
            head, _, tail = line.partition(TRACE_MARKER)
            words = head.split()
            if words and words[-1].isdigit():
                total = int(words[-1])
            numbers = [int(word) for word in tail.split() if word.isdigit()]
            continue
        rest.append(line)
    return numbers, total, rest


def trace_lines(numbers):
    """The trace with names, oldest first, the last one marked.

    A bare number is a lookup the reader has to do on a phone, and the last one
    is the whole point of the trace.
    """
    lines = []
    for index, number in enumerate(numbers):
        mark = "  <- last" if index == len(numbers) - 1 else ""
        lines.append("%s  %s%s" % (str(number).rjust(4),
                                   syscall_name(number) or "?", mark))
    return lines


LABELS = {
    "probe": "static PIE, no interpreter",
    "probe-interp": "with PT_INTERP",
}


def directory(res_dir, abi):
    return os.path.join(res_dir, DIRECTORY, abi or "arm64-v8a")


def read_attempt(code, out, err):
    """Pure; the strings come from the device."""
    text = "%s\n%s" % (out or "", err or "")
    if MARKER in text:
        return OK, "ran"
    lowered = text.lower()
    for needle in ("permission denied", "not permitted", "cannot execute",
                   "phdr", "cannot link", "not found", "exec format error",
                   "bad system call", "segmentation"):
        if needle in lowered:
            return BLOCKED, _first_line(text) or needle
    if code == 0:
        return UNKNOWN, _first_line(text) or "exit 0 without the marker"
    return BLOCKED, _first_line(text) or ("exit %s" % code)


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:150]
    return ""


def probe(res_dir, abi, linker, runner):
    """Runs each shape through the linker. Returns a result per shape."""
    results = {}
    base = directory(res_dir, abi)
    for shape in SHAPES:
        path = os.path.join(base, shape)
        if not os.path.isfile(path):
            results[shape] = {"status": MISSING,
                              "detail": "not built for %s" % (abi or "?")}
            continue
        code, out, err = runner([linker, path])
        status, detail = read_attempt(code, out, err)
        results[shape] = {"status": status, "detail": detail}
    return results


def chosen(results):
    for shape in SHAPES:
        if results.get(shape, {}).get("status") == OK:
            return shape
    return None


def summary_lines(results):
    marks = {OK: "+", BLOCKED: "x", MISSING: "-", UNKNOWN: "?"}
    lines = []
    for shape in SHAPES:
        result = results.get(shape, {})
        lines.append("[%s] %-28s %s" % (marks.get(result.get("status"), "?"),
                                        LABELS[shape],
                                        result.get("detail", "")))
    lines.append("")
    pick = chosen(results)
    if pick:
        lines.append("our own binaries run (%s) — the loader can be written"
                     % LABELS[pick])
    else:
        lines.append("the linker will not start a binary we built, so a loader "
                     "of our own cannot run either")
    return lines
