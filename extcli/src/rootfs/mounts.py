# SPDX-License-Identifier: Apache-2.0

"""The places a guest can see, and which of them are mounted.

A guest under the loader has its own `/`. That is the rootfs, and everything
else the phone holds would be unreachable from inside it — so four more
directories are grafted on at names of their own:

    /            the rootfs, Alpine's own files
    /sdcard      the phone's storage
    /exteraGram  the client's files
    /extCLI      this plugin's files
    /patch       the client, taken apart

Only the rootfs is mounted to begin with. The others are somebody's files —
the phone's, the client's — and a shell that can reach them by default is a
shell that can ruin them by accident; whoever wants them says so.

Each can be turned off, and one must always be on: a filesystem with nothing
in it is not a place anyone can stand. Turning one off makes it disappear —
`cd /sdcard` answers "no such file", exactly as an unmounted directory does on
any other Linux.

`/` is the exception, and it has to be. The programs are in there: busybox
reads its own /lib and /etc before it can do anything at all, and a rootfs that
vanished would take every command with it. So turning `/` off does not hide it
from the programs — it hides it from the *user*. Alpine's own files stop being
somewhere you can `cd` to or point `ls` at, the console starts in the first
mount that is on instead, and everything still runs.

That line can be drawn because the two halves ask through different doors. A
path the user typed arrives as an argument to this shell, which knows what is
mounted; a path a program resolves for itself arrives at the syscall, where the
loader answers without asking. Nothing here is a security boundary — the
plugin has full access by design, and this is about what is in the way.
"""

HOME = "/root"


class Mount(object):
    def __init__(self, key, guest, setting, label, description):
        self.key = key
        self.guest = guest
        self.setting = setting
        self.label = label
        self.description = description


ROOT = "root"
SDCARD = "sdcard"
EXTERA = "extera"
EXTCLI = "extcli"
PATCH = "patch"

# The order is the order they are offered in, and the order the console falls
# back through when the one before it is off.
MOUNTS = (
    Mount(ROOT, "/", "mount_root", "Alpine",
          "the rootfs — where the programs live"),
    Mount(SDCARD, "/sdcard", "mount_sdcard", "Storage",
          "the phone's own files"),
    Mount(EXTERA, "/exteraGram", "mount_extera", "exteraGram",
          "the client's files"),
    Mount(EXTCLI, "/extCLI", "mount_extcli", "extCLI",
          "this plugin's files"),
    Mount(PATCH, "/patch", "mount_patch", "Patches",
          "the client's own code, taken apart"),
)

# What is mounted for somebody who has changed nothing. Only the rootfs: the
# rest is other people's data, and /patch is a workbench that costs time and
# space to lay out.
DEFAULT_ON = (ROOT,)

ORDER = tuple(mount.key for mount in MOUNTS)
SETTINGS = tuple(mount.setting for mount in MOUNTS)


def mount(key):
    for item in MOUNTS:
        if item.key == key or item.setting == key:
            return item
    return None


def defaults():
    return {mount.key: mount.key in DEFAULT_ON for mount in MOUNTS}


def enabled(values):
    """The mounts that are on, in order.

    Never empty: a stored state with everything off should not be reachable,
    and if one is reached anyway the rootfs is the one to bring back, because
    without it there are no programs.
    """
    on = tuple(key for key in ORDER if values.get(key))
    return on or (ROOT,)


def is_on(values, key):
    return key in enabled(values)


def validate(values):
    """(ok, what to say). The rule is only ever "not the last one"."""
    if any(values.get(key) for key in ORDER):
        return True, ""
    return False, ("at least one path has to stay mounted — the console has to "
                   "open somewhere")


def would_empty(values, key, turning_off=True):
    """Would changing this one leave nothing mounted?"""
    after = dict(values)
    after[key] = not turning_off
    return not validate(after)[0]


def refusal(values, setting, new_value):
    """Why this change cannot be made, or "" if it can.

    One function so the settings page, `config set` and anything else added
    later cannot disagree about the rule. Turning one *on* is never refused.
    """
    item = mount(setting)
    if item is None or new_value:
        return ""
    after = dict(values)
    after[item.key] = False
    ok, message = validate(after)
    return "" if ok else message


def start(values):
    """Where the console opens.

    Alpine's home when the rootfs is mounted, since that is what every other
    shell does, and the first mount that is on when it is not.
    """
    on = enabled(values)
    if ROOT in on:
        return HOME
    return mount(on[0]).guest


def table(values, hosts):
    """[(guest path, host path)] for the loader.

    `/` is always in it, mounted or not: it is how a program finds its own
    libraries, and hiding it from the user is this shell's business rather
    than the loader's.
    """
    rows = []
    for item in MOUNTS:
        host = hosts.get(item.key)
        if not host:
            continue
        if item.key == ROOT or values.get(item.key):
            rows.append((item.guest, host))
    return rows


def encode(rows):
    """The form the loader reads out of EXTCLI_MOUNTS."""
    return "|".join("%s=%s" % (guest, host) for guest, host in rows)


def decode(text):
    rows = []
    for part in (text or "").split("|"):
        guest, sep, host = part.partition("=")
        if sep and guest and host:
            rows.append((guest, host))
    return rows


def normalise(guest_path):
    """A guest path with `.`, `..` and repeated slashes taken out.

    Textual on purpose: this decides what the user is allowed to name, and it
    must not depend on what happens to exist on the host.
    """
    parts = []
    for part in str(guest_path or "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def under(guest_path):
    """Which mount a guest path falls in, by longest name. Always answers:
    everything is under `/` if it is under nothing else."""
    path = normalise(guest_path)
    best = mount(ROOT)
    for item in MOUNTS:
        if item.key == ROOT:
            continue
        if path == item.guest or path.startswith(item.guest + "/"):
            if len(item.guest) > len(best.guest):
                best = item
    return best


def visible(values, guest_path):
    """May the user name this path?

    The only thing an unmounted path costs is reachability. `/etc` with the
    rootfs unmounted is not a file the user can open; busybox opening it for
    itself is another matter and never comes through here.
    """
    return bool(values.get(under(guest_path).key))


def host_path(rows, guest_path):
    """The host path for a guest one, by the same longest-name rule the loader
    uses. Pure, so it can be checked against the loader's own answer."""
    path = normalise(guest_path)
    best = None
    for guest, host in rows:
        if guest == "/":
            continue
        if path == guest or path.startswith(guest + "/"):
            if best is None or len(guest) > len(best[0]):
                best = (guest, host)
    if best is not None:
        rest = path[len(best[0]):].lstrip("/")
        return best[1].rstrip("/") + ("/" + rest if rest else "")
    for guest, host in rows:
        if guest == "/":
            rest = path.lstrip("/")
            return host.rstrip("/") + ("/" + rest if rest else "")
    return None


def guest_path(rows, host):
    """The other direction: where a host path is, in the guest's names.

    Needed wherever the shell learns a real path and has to say it back —
    globbing asks the filesystem, and the filesystem answers about the phone.
    """
    best = None
    for guest, host_dir in rows:
        trimmed = host_dir.rstrip("/")
        if host == trimmed or host.startswith(trimmed + "/"):
            if best is None or len(trimmed) > len(best[1]):
                best = (guest, trimmed)
    if best is None:
        return None
    rest = host[len(best[1]):].lstrip("/")
    if not rest:
        return best[0]
    return best[0].rstrip("/") + "/" + rest


class Paths(object):
    """Guest paths in, host paths out — the shell's half of the translation.

    The loader does this at the syscall for the programs it starts. This is for
    everything the shell does itself: `cd`, redirections, the file builtins,
    completion. Same table, same longest-name rule, so the two cannot disagree
    about where a path really is.

    `visible` is the half the loader has no business knowing. An unmounted path
    is not somewhere the user can go; a program that opens it for itself is
    another matter, and never comes through here.
    """

    def __init__(self, rows=None, values=None):
        self.rows = list(rows or [])
        self.values = dict(values or {})

    @property
    def active(self):
        return bool(self.rows)

    def home(self):
        return start(self.values) if self.active else None

    def host(self, guest_path):
        if not self.active:
            return guest_path
        return host_path(self.rows, guest_path) or guest_path

    def visible(self, guest_path):
        if not self.active:
            return True
        return visible(self.values, guest_path)

    def guest(self, host):
        if not self.active:
            return host
        return guest_path(self.rows, host) or host
