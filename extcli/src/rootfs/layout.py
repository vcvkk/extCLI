# SPDX-License-Identifier: Apache-2.0

"""What an installed rootfs looks like, and whether one is there.

Paths arrive as arguments so this can be tested against a directory built in a
temporary folder rather than a device.
"""

import os

# what has to be present before a rootfs is worth calling installed
REQUIRED = ("bin", "etc", "lib")

# what a shell would be, in the order worth trying
SHELLS = ("/bin/sh", "/bin/ash", "/bin/bash", "/bin/busybox")

MARKER = ".extcli-rootfs"

# where guest programs live, as the guest sees them
BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin",
            "/bin", "/usr/bin", "/sbin", "/usr/sbin")

# and where a program installed by the user lands, under whatever its home is
LOCAL_BIN = ".local/bin"


def bin_dirs(home=None):
    """Everywhere a command might be, in the guest's terms.

    `uv tool install elyxbuilder` succeeded and ended with "warning:
    /root/.local/bin is not on your PATH". It was right: nothing looked there,
    so the tool it had just installed could not be run — not by the shell's
    PATH and not by `which`, which is the same list. It goes first, the way it
    does everywhere else, so a tool the user installed wins over an older one
    from a package.
    """
    if not home:
        return BIN_DIRS
    return ("%s/%s" % (home.rstrip("/"), LOCAL_BIN),) + BIN_DIRS


# where apk writes down what it has installed
INSTALLED_DB = "lib/apk/db/installed"


def installed_package(root, name):
    """Is this package in the container already?

    Read out of apk's own database rather than by running apk: this is asked
    once per package before anything is started, and starting a guest program
    to answer it would cost more than reading the file.

    The database is stanzas of `key:value` lines, one per package, and `P:` is
    the name.
    """
    if not root or not name:
        return False
    try:
        with open(os.path.join(root, INSTALLED_DB), "r", encoding="utf-8",
                  errors="replace") as handle:
            wanted = "P:%s" % name
            for line in handle:
                if line.rstrip("\n") == wanted:
                    return True
    except Exception:
        return False
    return False


def installed_packages(root):
    """Every package name in apk's database."""
    names = []
    try:
        with open(os.path.join(root, INSTALLED_DB), "r", encoding="utf-8",
                  errors="replace") as handle:
            for line in handle:
                if line.startswith("P:"):
                    names.append(line[2:].strip())
    except Exception:
        return names
    return names


def save_strategy(root, name):
    """Remembers how guest programs start on this device.

    It lives in the rootfs rather than in the plugin's settings so it travels
    with the thing it describes: a rootfs that is deleted and unpacked again
    has to be measured again, and one that is left alone does not.
    """
    try:
        with open(os.path.join(root, MARKER), "w", encoding="utf-8") as handle:
            handle.write("%s\n" % name)
        return True
    except Exception:
        return False


def saved_strategy(root):
    try:
        with open(os.path.join(root, MARKER), "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except Exception:
        return None


MAX_LINKS = 40


def resolve(root, guest_path, max_links=MAX_LINKS):
    """Turns a guest path into a host path, following links as the guest would.

    A rootfs is full of absolute symlinks — Alpine's `/bin/sh` points at
    `/bin/busybox` — and inside the rootfs that is correct. Resolved by the
    host, the same link leads out of the rootfs to a file on the phone that
    does not exist, which is exactly how `rootfs probe launch` came back with
    "unable to open file .../rootfs/bin/sh". So absolute targets are followed
    from the rootfs, not from /.

    Returns the host path, or None if it dangles or loops.
    """
    if not guest_path:
        return None
    parts = [part for part in guest_path.replace("\\", "/").split("/") if
             part not in ("", ".")]
    resolved = []       # components already followed, guest-side
    links = 0

    while parts:
        part = parts.pop(0)
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = resolved + [part]
        host = os.path.join(root, *candidate)
        if not os.path.islink(host):
            resolved = candidate
            continue
        links += 1
        if links > max_links:
            return None
        target = os.readlink(host)
        if target.startswith("/"):
            # absolute inside the rootfs, which is where it means to point
            resolved = []
            parts = [p for p in target.split("/") if p not in ("", ".")] + parts
        else:
            parts = [p for p in target.split("/") if p not in ("", ".")] + parts

    host = os.path.join(root, *resolved) if resolved else root
    return host if os.path.exists(host) else None


def translate(root, guest_path):
    """A guest path written the way the host has to write it.

    `resolve` answers only for things that are already there, and a program
    about to create a file needs an answer too. So the part of the path that
    does exist is resolved — following the rootfs's own symlinks — and the rest
    is appended to it.

    Absolute paths only. Inside a rootfs `/` is the rootfs, the same way it
    would be under chroot: `ls /` should list Alpine's own root and not the
    phone's, which an app is not allowed to read anyway.
    """
    if not guest_path or not guest_path.startswith("/"):
        return None
    host = resolve(root, guest_path)
    if host is not None:
        return host
    parts = [part for part in guest_path.split("/") if part not in ("", ".")]
    tail = []
    while parts:
        tail.insert(0, parts.pop())
        host = resolve(root, "/" + "/".join(parts)) if parts else root
        if host is not None:
            return os.path.join(host, *tail)
    return os.path.join(root, *tail) if tail else root


def installed(root):
    """True when the directory holds something that could be a rootfs."""
    if not root or not os.path.isdir(root):
        return False
    return all(os.path.isdir(os.path.join(root, name)) for name in REQUIRED)


def shell_in(root):
    """The guest shell, as a host path, or None."""
    for candidate in SHELLS:
        path = os.path.join(root, candidate.lstrip("/"))
        if os.path.isfile(path) or os.path.islink(path):
            return candidate
    return None


def release(root):
    """The distribution's own name for itself, if it left one."""
    for name in ("etc/alpine-release", "etc/os-release", "etc/issue"):
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(400).strip()
        except Exception:
            continue
        if not text:
            continue
        if name.endswith("os-release"):
            for line in text.splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
            continue
        return text.splitlines()[0].strip()
    return None


def size(root):
    """(files, bytes) under the rootfs. Symlinks are counted, not followed."""
    files = 0
    total = 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            files += 1
            try:
                total += os.lstat(path).st_size
            except OSError:
                pass
    return files, total


def human_size(count):
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.0f %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024
    return "%.1f GB" % value


def status_rows(root):
    """(label, value) rows for `rootfs status`."""
    rows = [("path", root or "unknown")]
    if not installed(root):
        rows.append(("state", "not installed"))
        return rows
    rows.append(("state", "installed"))
    name = release(root)
    if name:
        rows.append(("release", name))
    shell = shell_in(root)
    rows.append(("shell", shell or "none found"))
    files, total = size(root)
    rows.append(("contents", "%d files, %s" % (files, human_size(total))))
    return rows
