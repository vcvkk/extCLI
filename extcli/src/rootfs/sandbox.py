# SPDX-License-Identifier: Apache-2.0

"""What this device's syscall filter refuses, and what to tell the guest instead.

Android answers a refused syscall with SECCOMP_RET_KILL_PROCESS. Not an error —
death, and not even a legible one: the number cannot be read from inside the
process that asked, which is why `rootfs probe syscalls` asks from outside, a forked
child per number.

`rootfs trace` then showed which of them Alpine actually reaches. busybox drops
privileges at startup with setuid(getuid()) — asking to become the user it
already is — and that call is fatal here. Nothing is broken; an ordinary Linux
kernel would have returned 0 and busybox would have carried on.

So the loader intercepts. It supervises the guest with ptrace and, at the
syscall's entry stop, replaces a refused call with getpid before the kernel
gets any further, then writes the answer the guest wanted over the result.
This module decides what that answer is, and remembers the measurement so it is
made once per device rather than once per launch.

Replaced rather than cancelled, and the device's own scan is why. It refuses
240 numbers, and most of them are numbers arm64 has no syscall for at all —
nothing is there to refuse. So the filter kills whatever it does not recognise,
and -1, the number that means "tracer cancelled this", is not recognised
either. The first version of this used it, and the guest died in the same
place, one syscall later than before.

Two kinds of answer:

  the uid and gid changes    succeed
  everything else            EPERM

The first is not a lie: inside an app sandbox the ids the guest asks for are
the ids it already has, so 0 is the truthful result and the only reason the
call cannot happen is that Android will not let anyone say it.

The second is the honest answer for a call that genuinely cannot work here.
mount fails with EPERM and every program that mounts is written to cope with
that, because that is what an unprivileged process gets everywhere else. What
it is not is a kill.
"""

import os

EPERM = 1

# The privilege drops. Refusing them kills; allowing them changes nothing.
PRETEND_DONE = frozenset((
    91,   # capset
    143,  # setregid
    144,  # setgid
    145,  # setreuid
    146,  # setuid
    147,  # setresuid
    149,  # setresgid
    151,  # setfsuid
    152,  # setfsgid
))

FILE = "syscalls"
HEADER = "extcli-syscalls 1"

# The call a refused one is turned into: allowed everywhere, no arguments,
# no effect. The loader knows the number; this is here so a device that
# somehow refused it too would be reported rather than quietly killed.
DIVERSION = 172  # getpid


def can_divert(refused):
    return DIVERSION not in set(int(number) for number in (refused or ()))


def rules(refused):
    """(number, what the guest is told) for each refused syscall."""
    numbers = sorted(set(int(number) for number in refused))
    return [(number, 0 if number in PRETEND_DONE else -EPERM)
            for number in numbers]


def encode(rule_list):
    """The form the loader reads out of EXTCLI_BLOCKED.

    A bare number means "tell the guest it worked"; anything else spells out
    the return value.
    """
    parts = []
    for number, value in rule_list:
        parts.append("%d" % number if value == 0 else "%d:%d" % (number, value))
    return ",".join(parts)


def decode(text):
    """The other direction, so a round trip can be tested."""
    result = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        number, _, value = part.partition(":")
        try:
            result.append((int(number), int(value) if value else 0))
        except ValueError:
            continue
    return result


def path(state_dir):
    return os.path.join(state_dir, FILE)


def save(state_dir, refused):
    """Remembers the measurement. It describes the device, not the rootfs, so
    it outlives any particular one."""
    try:
        if not os.path.isdir(state_dir):
            os.makedirs(state_dir)
        with open(path(state_dir), "w", encoding="utf-8") as handle:
            handle.write("%s\n%s\n" % (HEADER, " ".join(
                str(int(number)) for number in sorted(set(refused)))))
        return True
    except Exception:
        return False


def load(state_dir):
    """The refused numbers, or None if nothing has been measured yet.

    An empty list is a real answer — a device that refuses nothing — and is
    told apart from never having asked.
    """
    try:
        with open(path(state_dir), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except Exception:
        return None
    if not lines or lines[0].strip() != HEADER:
        return None
    numbers = []
    for word in " ".join(lines[1:]).split():
        try:
            numbers.append(int(word))
        except ValueError:
            continue
    return numbers


def blocked_for(state_dir):
    """EXTCLI_BLOCKED for this device, or "" if there is nothing to do."""
    refused = load(state_dir)
    if not refused:
        return ""
    return encode(rules(refused))


def sentence(rule_list):
    """What the rows add up to.

    It says what happens now, not why something failed. The version of this
    report that guessed at a cause named the wrong one, and a wrong cause is
    worth less than none.
    """
    if not rule_list:
        return "nothing is refused"
    pretended = sum(1 for _, value in rule_list if value == 0)
    return ("%d refused; the loader answers %d of them itself and turns the "
            "rest into EPERM, so the guest gets an error where it used to get "
            "killed" % (len(rule_list), pretended))


def describe(rule_list, name_of=None):
    """Rows for the console: what happens to each refused call."""
    rows = []
    for number, value in rule_list:
        name = (name_of(number) if name_of else "") or "?"
        answer = "succeeds" if value == 0 else "fails with EPERM"
        rows.append("%s  %-18s %s" % (str(number).rjust(4), name, answer))
    return rows
