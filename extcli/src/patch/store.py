# SPDX-License-Identifier: Apache-2.0

"""Where a workspace lives, and where the copy of it that nobody edits lives.

Two directories per workspace:

* the tree being edited, under the patch directory, which is mounted into the
  container as `/patch` — so an editor, a shell script and a file dropped in
  from a chat all reach it the same way;

* the tree as it was opened, under the state directory, which is not mounted
  anywhere. It is what `patch diff` compares against, and keeping it out of
  reach is what makes the comparison mean something: a workspace cannot
  accidentally rewrite its own idea of what it started as.

Both take explicit roots rather than asking `compat.paths` for them, so all of
this runs in a test with two temporary directories and no device.
"""

import json
import os
import shutil
import time

from . import pack, workspace

# What the pristine copy and the note about it are called under the state
# directory.
ORIGIN = "origin"
NOTE = "patch.json"


def work_dir(work_root, name):
    return os.path.join(str(work_root), name)


def state_dir(state_root, name):
    return os.path.join(str(state_root), "patches", name)


def origin_dir(state_root, name):
    return os.path.join(state_dir(state_root, name), ORIGIN)


def names(work_root):
    """Every workspace there is, whatever made it."""
    try:
        return sorted(entry for entry in os.listdir(str(work_root))
                      if os.path.isdir(os.path.join(str(work_root), entry))
                      and not entry.startswith("."))
    except Exception:
        return []


def note(state_root, name):
    """Where the workspace came from, or {} if that was never written down."""
    try:
        with open(os.path.join(state_dir(state_root, name), NOTE),
                  "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_note(state_root, name, data):
    directory = state_dir(state_root, name)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, NOTE), "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)


PLUGIN, CLIENT = "plugin", "client"


def kind(state_root, name):
    """What sort of workspace this is; they are built in different ways."""
    return note(state_root, name).get("kind") or PLUGIN


def create_client(work_root, state_root, name, client, label=None,
                  replace=False, on_progress=None):
    """Lays the client out as a workspace. Returns (ok, detail).

    Same two copies as a plugin workspace, for the same reason — but what is
    copied is the index and the hooks, not the client's code, which stays in
    the APK where it already is.
    """
    from . import client as client_module

    work = work_dir(work_root, name)
    if os.path.isdir(work):
        if not replace:
            return False, "there is already a workspace called %s" % name
        drop(work_root, state_root, name)
    os.makedirs(work, exist_ok=True)

    ok, detail = client_module.lay_out(work, client, on_progress=on_progress)
    if not ok:
        _remove(work)
        return False, detail

    ok, copied = pack.copy_tree(work, origin_dir(state_root, name))
    if not ok:
        _remove(work)
        return False, "could not keep a copy to compare against: %s" % copied

    _write_note(state_root, name, {
        "kind": CLIENT,
        "source": client.path,
        "label": label or "the client",
        "version": "",
        "opened": int(time.time()),
    })
    return True, work


def create(work_root, state_root, name, source, label=None, version=None,
           replace=False):
    """Lays `source` out as a workspace. Returns (ok, detail).

    `source` is a `.eaf` or a directory; both happen, because a client may
    keep a plugin either way and because opening a workspace on a tree
    somebody already has is a perfectly reasonable thing to want.

    Refuses an existing workspace unless told otherwise: the tree under
    `/patch` is somebody's unfinished work, and replacing it without being
    asked would throw away the only copy of it.
    """
    work = work_dir(work_root, name)
    if os.path.isdir(work):
        if not replace:
            return False, "there is already a workspace called %s" % name
        drop(work_root, state_root, name)
    os.makedirs(str(work_root), exist_ok=True)

    source = str(source)
    if os.path.isdir(source):
        ok, detail = pack.copy_tree(source, work)
    elif os.path.isfile(source):
        ok, detail = pack.unpack(source, work)
    else:
        return False, "no such plugin file: %s" % source
    if not ok:
        _remove(work)
        return False, detail

    ok, detail = pack.copy_tree(work, origin_dir(state_root, name))
    if not ok:
        _remove(work)
        return False, "could not keep a copy to compare against: %s" % detail

    _write_note(state_root, name, {
        "kind": PLUGIN,
        "source": source,
        "label": label or name,
        "version": version or "",
        "opened": int(time.time()),
    })
    return True, work


def drop(work_root, state_root, name):
    """Throws a workspace and its pristine copy away. Returns (ok, detail)."""
    problems = []
    for path in (work_dir(work_root, name), state_dir(state_root, name)):
        if not _remove(path):
            problems.append(path)
    if problems:
        return False, "could not remove %s" % ", ".join(problems)
    return True, "%s is gone" % name


def _remove(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        return True
    except Exception:
        return False


def changes(work_root, state_root, name):
    """What has been done to a workspace since it was opened."""
    return workspace.compare(origin_dir(state_root, name),
                             work_dir(work_root, name))


def exists(work_root, name):
    return os.path.isdir(work_dir(work_root, name))


def openable(state_root, name):
    """Is there something to compare this workspace against?

    A workspace whose pristine copy has gone — deleted with the rest of the
    stored data, most likely — can still be built, but nothing can be said
    about what changed in it, and saying so is better than reporting that
    every file in it is new.
    """
    return os.path.isdir(origin_dir(state_root, name))
