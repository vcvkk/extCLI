# SPDX-License-Identifier: Apache-2.0

"""Unpacking a root filesystem tarball.

Written by hand rather than with `tarfile.extractall` for three reasons, all of
which have bitten someone before:

  * A tar entry may name `../../etc/passwd`, or an absolute path. Extracting
    that writes outside the rootfs. Every member is checked against the
    destination before anything is created.
  * A rootfs tarball contains device nodes, fifos and sockets that an
    unprivileged app cannot create; `extractall` raises on the first one and
    leaves a half-unpacked tree. They are skipped and counted instead.
  * setuid and setgid bits mean nothing here and are worth nothing but risk,
    so they are dropped.

Absolute symlink targets are kept as written: inside a rootfs `/lib/ld-musl` is
correct, and rewriting it would break the guest to please the host.
"""

import os
import posixpath
import tarfile

# what a member turned into
WRITTEN = "written"
SKIPPED = "skipped"
REFUSED = "refused"
# not worth telling anyone about: the entry for the archive's own root, which
# every real tarball has and which is not an attempt to escape anything
IGNORED = "ignored"

ROOT_NAMES = ("", ".", "./", "/")

SETID_BITS = 0o6000


def safe_name(name):
    """The path a member may be extracted to, or None if it escapes.

    Pure, and the most important function in the file.
    """
    name = (name or "").replace("\\", "/")
    if not name or name in (".", "./"):
        return None
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    normalised = posixpath.normpath(name)
    if normalised.startswith("../") or normalised == "..":
        return None
    if normalised.startswith("/"):
        return None
    return normalised


def classify(member):
    """(kind, reason) for one tar member, without touching the filesystem."""
    if (member.name or "").strip().rstrip("/") in ("", "."):
        return IGNORED, "the archive's own root"
    if safe_name(member.name) is None:
        return REFUSED, "path escapes the rootfs"
    if member.ischr() or member.isblk() or member.isfifo():
        return SKIPPED, "device or fifo, which an app cannot create"
    if not (member.isfile() or member.isdir() or member.issym()
            or member.islnk()):
        return SKIPPED, "unsupported entry type"
    return WRITTEN, ""


def permissions(mode):
    """The mode to actually use: never setuid, never setgid."""
    return int(mode) & ~SETID_BITS & 0o7777


class Report(object):
    """What an install did, in numbers a person can act on."""

    def __init__(self):
        self.written = 0
        self.directories = 0
        self.symlinks = 0
        self.skipped = []
        self.refused = []

    @property
    def total(self):
        return self.written + self.directories + self.symlinks

    def lines(self):
        out = ["%d files, %d directories, %d symlinks"
               % (self.written, self.directories, self.symlinks)]
        if self.skipped:
            out.append("%d entries skipped (%s)"
                       % (len(self.skipped), _sample(self.skipped)))
        if self.refused:
            out.append("%d entries refused (%s)"
                       % (len(self.refused), _sample(self.refused)))
        return out


def _sample(entries, limit=3):
    names = [name for name, _reason in entries[:limit]]
    if len(entries) > limit:
        names.append("...")
    return ", ".join(names)


def extract(archive, destination, on_progress=None):
    """Unpacks an open tarfile into `destination`. Returns a Report."""
    report = Report()
    os.makedirs(destination, exist_ok=True)
    root = os.path.realpath(destination)

    for member in archive:
        kind, reason = classify(member)
        if kind == IGNORED:
            continue
        if kind == REFUSED:
            report.refused.append((member.name, reason))
            continue
        if kind == SKIPPED:
            report.skipped.append((member.name, reason))
            continue

        relative = safe_name(member.name)
        target = os.path.join(root, relative)
        # belt and braces: normpath above is about the name, this is about the
        # path that actually results, symlinked parents included
        if not _inside(root, target):
            report.refused.append((member.name, "resolves outside the rootfs"))
            continue

        try:
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                report.directories += 1
            elif member.issym():
                _replace_symlink(target, member.linkname)
                report.symlinks += 1
            elif member.islnk():
                _write_hardlink(archive, member, root, target, report)
            else:
                _write_file(archive, member, target)
                report.written += 1
        except Exception as e:
            report.skipped.append((member.name, "%s: %s" % (type(e).__name__, e)))
            continue

        # often enough for a bar to move: an Alpine minirootfs is about five
        # hundred entries, so every two hundred was two calls in all
        if on_progress is not None and report.total % 25 == 0:
            on_progress(report.total)

    _apply_modes(archive, root, report)
    return report


def _inside(root, path):
    normalised = os.path.normpath(path)
    return normalised == root or normalised.startswith(root + os.sep)


def _write_file(archive, member, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        return
    with open(target, "wb") as out:
        while True:
            chunk = source.read(262144)
            if not chunk:
                break
            out.write(chunk)
    os.chmod(target, permissions(member.mode))


def _replace_symlink(target, linkname):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.islink(target) or os.path.exists(target):
        os.remove(target)
    os.symlink(linkname, target)


def _write_hardlink(archive, member, root, target, report):
    """Hard links become copies.

    A tarball's link target may not have been extracted yet, and an app has no
    reason to care about inode sharing — a copy is correct and cannot fail.
    """
    source_name = safe_name(member.linkname)
    source = os.path.join(root, source_name) if source_name else None
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if source and os.path.isfile(source):
        with open(source, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
        report.written += 1
    else:
        report.skipped.append((member.name, "hard link with no target"))


def _apply_modes(archive, root, report):
    """Directory modes last: writing into a read-only directory fails, so they
    are only tightened once everything inside them exists."""
    try:
        members = archive.getmembers()
    except Exception:
        return
    for member in members:
        if not member.isdir():
            continue
        relative = safe_name(member.name)
        if not relative:
            continue
        path = os.path.join(root, relative)
        try:
            os.chmod(path, permissions(member.mode) | 0o700)
        except Exception:
            pass


def install(tarball, destination, on_progress=None):
    """Opens a tarball and unpacks it. Any compression tarfile understands."""
    if not os.path.isfile(tarball):
        raise IOError("no such file: %s" % tarball)
    with tarfile.open(tarball, "r:*") as archive:
        return extract(archive, destination, on_progress)
