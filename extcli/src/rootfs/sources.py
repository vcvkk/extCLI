# SPDX-License-Identifier: Apache-2.0

"""Root filesystems that ship with the plugin.

`rootfs install alpine` is meant to be the whole story: no downloading, no
finding a tarball, no getting the architecture right by hand. The archive is in
the plugin, its checksum is recorded here, and it is verified before a single
file is unpacked — a truncated asset is a broken rootfs that fails much later
and much more confusingly.

Recorded rather than computed, deliberately: this is the checksum Alpine
published for the file, checked when it was fetched. Computing it from the file
we are about to trust would prove nothing.
"""

import hashlib
import os

# where bundled archives live inside res/
DIRECTORY = "rootfs"

READ_SIZE = 1 << 20


class Source(object):
    """One bundled root filesystem."""

    def __init__(self, name, filename, abis, sha256, release, description="",
                 entries=0):
        self.name = name
        self.filename = filename
        self.abis = tuple(abis)
        self.sha256 = sha256
        self.release = release
        self.description = description
        # how many things are in the archive. Known because we bundled it, and
        # worth knowing because a progress bar cannot be drawn from a count
        # with nothing to compare it against — and counting them first means
        # decompressing the whole archive twice.
        self.entries = int(entries or 0)

    def path(self, res_dir):
        return os.path.join(res_dir, DIRECTORY, self.filename)

    def present(self, res_dir):
        return os.path.isfile(self.path(res_dir))

    def size(self, res_dir):
        try:
            return os.path.getsize(self.path(res_dir))
        except OSError:
            return 0

    def supports(self, abi):
        return not abi or abi in self.abis

    def as_row(self, res_dir=None, abi=None):
        notes = []
        if res_dir and not self.present(res_dir):
            notes.append("not bundled")
        if abi and not self.supports(abi):
            notes.append("not for %s" % abi)
        detail = self.release
        if notes:
            detail = "%s (%s)" % (detail, ", ".join(notes))
        return (self.name, detail)


BUNDLED = (
    Source(
        name="alpine",
        filename="alpine-minirootfs-3.24.1-aarch64.tar.gz",
        abis=("arm64-v8a", "aarch64"),
        sha256="f55a90f69052c5bd6f92cb09a8f47065970830b194c917a006fb94028e721259",
        release="Alpine Linux 3.24.1 (aarch64)",
        description="busybox, musl and apk; about 8 MB unpacked",
        entries=515,
    ),
)


def names():
    return [source.name for source in BUNDLED]


def find(name):
    """A bundled source by name, case-insensitively. `Alpine` is `alpine`."""
    wanted = (name or "").strip().lower()
    for source in BUNDLED:
        if source.name == wanted:
            return source
    return None


def digest(path, read_size=READ_SIZE):
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(read_size)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def verify(source, res_dir):
    """(ok, detail). Checked before unpacking, never after."""
    path = source.path(res_dir)
    if not os.path.isfile(path):
        return False, "%s is not bundled in this build" % source.filename
    try:
        found = digest(path)
    except Exception as e:
        return False, "cannot read %s: %s" % (source.filename, e)
    if found != source.sha256:
        return False, ("%s does not match its recorded checksum "
                       "(%s..., expected %s...)"
                       % (source.filename, found[:12], source.sha256[:12]))
    return True, "checksum matches"
