# SPDX-License-Identifier: Apache-2.0

"""Packing the container into one file, to be sent somewhere.

Setting a container up is an evening: unpacking Alpine, measuring what the
device allows, then tens or hundreds of megabytes of toolsets over somebody's
mobile data. Losing the phone should not mean doing all of it again, and the
place to keep a backup is already open on the screen — a chat.

Nothing here knows about chats. It writes a file; who it is handed to is the
caller's business.
"""

import os
import posixpath
import tarfile
import time

from ..utils import log, purge

# Compression is on, but at the cheapest setting. The archive is going over a
# network, so sending it uncompressed would cost more than the phone saves —
# and a rootfs is mostly ELF, which gives most of its ratio up at level 1
# anyway. Level 9 on three hundred megabytes is minutes of a warm phone.
COMPRESSION = 1

# How often the caller hears about progress. A file per callback would be one
# call per hundred microseconds and a bar redrawn far more often than a screen
# can show it.
REPORT_EVERY = 200


def name_for(root, when=None):
    """What the archive is called.

    Dated, because the first thing anybody does with a backup is make another
    one, and two files called extcli-rootfs.tar.gz in the same chat are a
    puzzle rather than a backup.
    """
    del root
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(when or time.time()))
    return "extcli-rootfs-%s.tar.gz" % stamp


def measure(root):
    """(files, bytes) of what would go in. Symlinks count as themselves."""
    return purge.measure(root)


def archive(root, target, on_progress=None):
    """Writes the container to `target`. Returns (ok, detail).

    Symlinks are stored as symlinks, not followed.  Absolute links that point
    at the rootfs's own `/` are made relative: they still resolve to the same
    member after restore, but archives also unpack with Python's safe default
    extraction filter.  Following links would both inflate the archive and
    bake this phone's paths into it.
    """
    if not os.path.isdir(root):
        return False, "there is no container to export"
    total = measure(root)[0] or 1
    done = [0]

    def entries():
        for base, directories, files in os.walk(root):
            for name in directories + files:
                yield os.path.join(base, name)

    try:
        with tarfile.open(target, "w:gz", compresslevel=COMPRESSION) as archive_file:
            for path in entries():
                try:
                    archive_file.add(path, arcname=os.path.relpath(path, root),
                                     recursive=False, filter=_portable_link)
                except (OSError, ValueError) as e:
                    # a socket, or something that went away mid-walk; the rest
                    # of the container is still worth having
                    log.log("export: skipped %s: %s" % (path, e), debug=True)
                done[0] += 1
                if on_progress is not None and done[0] % REPORT_EVERY == 0:
                    try:
                        on_progress(min(done[0] / float(total), 1.0))
                    except Exception:
                        pass
    except Exception as e:
        log.error("export: could not write %s" % target, e)
        try:
            os.remove(target)
        except Exception:
            pass
        return False, "%s: %s" % (type(e).__name__, e)
    if on_progress is not None:
        try:
            on_progress(1.0)
        except Exception:
            pass
    return True, target


def _portable_link(info):
    """Return a tar member whose rootfs-local absolute link is relative.

    Alpine uses links such as ``/bin/busybox`` to mean a file below its own
    root, not a file in the Android host filesystem.  A relative target has
    that same meaning once the archive is restored and, unlike an absolute
    target, is accepted by :mod:`tarfile`'s data filter.
    """
    if info.issym() and info.linkname.startswith("/"):
        info.linkname = posixpath.relpath(
            info.linkname.lstrip("/"), posixpath.dirname(info.name) or "."
        )
    return info
