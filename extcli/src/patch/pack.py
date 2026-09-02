# SPDX-License-Identifier: Apache-2.0

"""Turning a workspace back into a plugin.

A `.eaf` is a zip with `refmap.yml` at its root; that file names the metadata
file, and the metadata is the flat `key: value` `compat.meta` already reads.
So a workspace opened from a plugin is already a plugin — it is the archive,
unpacked — and building it again is a matter of changing four lines of the
metadata and zipping the tree back up.

Four lines, and not one more. The patch is a new plugin rather than an
overwrite of the one it came from: a different id, a name saying what it is
and which build of it this is, and a description carrying the summary. The
original stays installed and working, which is the whole point of being able
to build a patch at all — if it were an overwrite there would be nothing to
go back to.

No client, no device, no Android. It reads a directory and writes a file.
"""

import os
import zipfile

from ..compat import meta as meta_module
from . import workspace

REFMAP = "refmap.yml"

# What the built archive carries beyond what was in the workspace: the report
# `workspace.report` writes, so the plugin says what it is even to somebody
# who has only the file.
REPORT_NAME = "PATCH.md"

# The keys a patch build replaces, and nothing else. Everything the plugin
# said about itself — its author, the client versions it needs, the SDK it was
# written against — is still true of the patched copy.
OWNED = ("id", "name", "version", "description")


def metadata(path):
    """The workspace's own metadata: (where it lives, what it says).

    (None, {}) when this is not a plugin tree, which is the honest answer for
    a directory somebody made by hand and is the difference between a refusal
    with a reason and a zip nothing can install.
    """
    root = str(path)
    try:
        with open(os.path.join(root, REFMAP), "r", encoding="utf-8") as handle:
            refmap = meta_module.parse(handle.read())
    except Exception:
        return None, {}
    relative = refmap.get("metainfo")
    if not relative:
        return None, {}
    full = os.path.join(root, relative.replace("/", os.sep))
    try:
        with open(full, "r", encoding="utf-8") as handle:
            return relative, meta_module.parse(handle.read())
    except Exception:
        return relative, {}


def render(data, order=()):
    """Flat metadata back into the file it came from.

    Quoted only where it has to be: a value with a colon in it would be read
    back as another key, and one that starts with a quote would be unwrapped.
    Everything else is left exactly as it was typed, because this file is
    something people read.
    """
    keys = [key for key in order if key in data]
    keys += [key for key in data if key not in keys]
    lines = []
    for key in keys:
        value = "" if data[key] is None else str(data[key])
        if _needs_quotes(value):
            value = '"%s"' % value.replace('"', '\\"')
        lines.append("%s: %s" % (key, value))
    return "\n".join(lines) + "\n"


def _needs_quotes(value):
    if not value:
        return False
    if value != value.strip():
        return True
    if value[0] in "\"'#":
        return True
    return ":" in value or " #" in value


def version_of(data, mark):
    """The patch's version: the original's, with the build's mark on it.

    A patch of 1.2.0 is not 1.2.0, and it is not 1.2.1 either — it is not
    further along, it is off to one side. Saying so in the version is the one
    place somebody will look when two plugins claim the same lineage.
    """
    base = str(data.get("version") or "0").strip() or "0"
    return "%s+patch.%s" % (base, mark)


def fields(data, mark, changes, source=None):
    """What the built plugin says about itself."""
    source = source or data.get("name") or data.get("id") or "a plugin"
    return {
        "id": workspace.plugin_id(mark),
        "name": workspace.plugin_name(mark),
        "version": version_of(data, mark),
        "description": workspace.description(source, data.get("version"),
                                             changes),
    }


def build(work, target, mark, changes, source=None, when=None):
    """Writes the workspace to `target` as a plugin. Returns (ok, detail).

    The archive is deflated and its entries are sorted, so building the same
    workspace twice gives the same bytes — which matters the moment somebody
    wants to know whether the file in a chat is the one they built.
    """
    relative, data = metadata(work)
    if relative is None:
        return False, "%s is not a plugin tree (no %s in it)" % (work, REFMAP)
    if not data.get("id"):
        return False, "%s has no id in it" % relative

    named = dict(data)
    named.update(fields(data, mark, changes, source=source))
    text = render(named, order=OWNED + tuple(sorted(data)))
    source_name = source or data.get("name") or data.get("id")

    try:
        with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as out:
            for path in workspace.walk(work):
                if path == relative:
                    out.writestr(path, text)
                    continue
                out.write(os.path.join(str(work), path.replace("/", os.sep)),
                          path)
            out.writestr(REPORT_NAME, "\n".join(workspace.report(
                source_name, data.get("version"), changes,
                named["name"], when=when)) + "\n")
    except Exception as e:
        try:
            os.remove(str(target))
        except Exception:
            pass
        return False, "%s: %s" % (type(e).__name__, e)
    return True, named["name"]


def unpack(archive, target):
    """Lays a `.eaf` out as a directory. Returns (ok, detail).

    Entries are checked before anything is written: a zip may name a path
    outside the directory it is being unpacked into, and the one time that
    happens it will not be by accident.
    """
    root = os.path.abspath(str(target))
    try:
        with zipfile.ZipFile(str(archive)) as source:
            names = source.namelist()
            for name in names:
                full = os.path.abspath(os.path.join(root, name))
                if full != root and not full.startswith(root + os.sep):
                    return False, "%s escapes the workspace" % name
            source.extractall(root)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return True, root


def copy_tree(source, target):
    """A second copy of a tree, for the pristine one nothing may reach."""
    import shutil

    try:
        if os.path.isdir(str(target)):
            shutil.rmtree(str(target))
        shutil.copytree(str(source), str(target),
                        ignore=shutil.ignore_patterns(*workspace.SKIP_DIRS))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return True, str(target)
