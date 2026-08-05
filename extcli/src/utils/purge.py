# SPDX-License-Identifier: Apache-2.0

"""Deleting what extCLI has written.

Everything the plugin writes lives outside the plugin's own directory, in
`files/extcli`, so that a plugin update does not throw away an Alpine the user
has spent an evening setting up. The cost of that decision is the one this
module answers: removing the plugin leaves it all behind, and there is no
uninstall hook to notice — `on_plugin_unload` fires when a plugin is merely
switched off, and wiping a container because somebody toggled a switch would be
unforgivable.

So it is asked for, and the asking says exactly what will go and how much of it
there is. Paths arrive as arguments and nothing here imports the client, so the
counting and the refusals are tested against directories in a temporary folder.
"""

import os
import shutil


def measure(path):
    """(files, bytes) under a path. A missing path is (0, 0), not an error."""
    files = 0
    total = 0
    if not path or not os.path.isdir(path):
        return files, total
    for directory, _names, entries in os.walk(path):
        for entry in entries:
            full = os.path.join(directory, entry)
            files += 1
            try:
                # the link itself, never what it points at: a rootfs is full of
                # absolute links, and following one would count the phone
                total += os.lstat(full).st_size
            except OSError:
                continue
    return files, total


def human_size(count):
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%.0f %s" % (value, unit) if unit == "B"
                    else "%.1f %s" % (value, unit))
        value /= 1024
    return "%.1f GB" % value


def describe(paths):
    """What removing these would cost, as one short sentence."""
    files = 0
    total = 0
    for path in paths:
        found, size = measure(path)
        files += found
        total += size
    if not files:
        return "nothing to delete", 0, 0
    return "%d files, %s" % (files, human_size(total)), files, total


def protects(path, *keep):
    """Is this path one of the ones that must never be deleted?

    A guard rather than a comment. `data_dir` is built from the app's own files
    directory, and a bug that returned that directory instead — or `/`, or the
    phone's storage — would take the client's own data with it. Nothing here
    knows what those directories are, so the caller names them.
    """
    if not path:
        return True
    normalised = os.path.normpath(path)
    if normalised in ("/", "", "."):
        return True
    for other in keep:
        if other and os.path.normpath(other) == normalised:
            return True
    return False


class Result(object):
    def __init__(self):
        self.removed = []   # (path, files, bytes)
        self.refused = []   # (path, why)
        self.failed = []    # (path, why)

    @property
    def ok(self):
        return not self.refused and not self.failed

    @property
    def files(self):
        return sum(files for _path, files, _size in self.removed)

    @property
    def bytes(self):
        return sum(size for _path, _files, size in self.removed)

    def sentence(self):
        if self.failed:
            return "could not delete %s" % self.failed[0][0]
        if self.refused:
            return "refused to delete %s: %s" % self.refused[0]
        if not self.files:
            return "there was nothing to delete"
        return "deleted %d files (%s)" % (self.files, human_size(self.bytes))


def remove(paths, keep=()):
    """Deletes each path, whole. Never raises.

    Counted before it goes, because afterwards there is nothing left to ask.
    """
    result = Result()
    for path in paths:
        if protects(path, *keep):
            result.refused.append((path, "this is not ours to delete"))
            continue
        if not os.path.isdir(path):
            continue
        files, total = measure(path)
        shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):
            # ignore_errors leaves what it could not remove; say so rather
            # than reporting a success the user can see is not one
            left, _size = measure(path)
            result.failed.append((path, "%d files are still there" % left))
            continue
        result.removed.append((path, files, total))
    return result
