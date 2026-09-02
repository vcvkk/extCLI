# SPDX-License-Identifier: Apache-2.0

"""What changed in a workspace, and what to call what comes out of it.

A patch workspace is two copies of the same tree: the one that was opened,
kept where nothing can reach it, and the one being edited, sitting under
`/patch` where the shell and every editor in the container can. Everything
this module answers comes from comparing the two, so nothing has to be
recorded as it is typed and no editor has to know it is being watched — vi,
sed, a Python script, a file dropped in from a chat, all of it counts the
same.

Nothing here touches a device or a client. It reads two directories and
returns what is different about them.
"""

import difflib
import os

# What is never part of a change: a repository somebody happened to clone into
# the workspace, and an editor's leavings. `__pycache__` covers the ordinary
# case of bytecode generated from source, which is the only case where a
# `.pyc` is not worth looking at.
SKIP_DIRS = ("__pycache__", ".git", ".hg", ".svn")
SKIP_SUFFIXES = (".orig", ".rej", ".swp", "~")

# A compiled file beside its own source is made from that source and says
# nothing the source does not. A compiled file *without* source is the plugin
# — most published ones ship exactly like that — and skipping those would make
# a compiled plugin's workspace look empty and drop every change made to it.
COMPILED = (".pyc", ".pyo")

# The token in a built patch's name. No 0/O or 1/l/I: these get read off a
# screen and typed back in, and a name nobody can transcribe is a name that
# gets copied wrong.
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TOKEN_LENGTH = 6

# How much of a file is read before it is treated as too big to diff. A plugin
# source file is kilobytes; anything past this is an asset that happens to be
# text, and counting its lines would say nothing useful and cost a lot.
DIFF_LIMIT = 1 << 20

ADDED, REMOVED, MODIFIED = "A", "D", "M"


# ------------------------------------------------------------------- naming


def token(seed=None):
    """The random part of a patch's name.

    `seed` is for the tests and for `--name`; without one it is genuinely
    random, because two patches built a minute apart have to be two plugins
    and not one overwriting the other.
    """
    import random

    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    return "".join(rng.choice(ALPHABET) for _ in range(TOKEN_LENGTH))


def plugin_name(mark):
    return "extCLI patch-%s" % mark


def plugin_id(mark):
    """An id the client can hold: lower case, no spaces, no punctuation."""
    return "extcli_patch_%s" % str(mark).lower()


def workspace_name(text):
    """A directory name safe to put in a path and to type again afterwards."""
    out = []
    for char in str(text or "").strip():
        out.append(char if (char.isalnum() or char in "-_.") else "-")
    name = "".join(out).strip("-.") or "patch"
    return name[:64]


# ------------------------------------------------------------------ reading


def _skip_dir(name):
    return name in SKIP_DIRS


def _skip_file(name, siblings=()):
    if name.endswith(SKIP_SUFFIXES):
        return True
    if name.endswith(COMPILED):
        return (name.rsplit(".", 1)[0] + ".py") in siblings
    return False


def walk(root):
    """Every file in a tree, by its path relative to the root.

    Sorted, so two walks of the same tree are the same list and a report does
    not change order between runs.
    """
    found = []
    root = str(root)
    for base, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories
                                if not _skip_dir(name))
        here = frozenset(files)
        for name in sorted(files):
            if _skip_file(name, here):
                continue
            full = os.path.join(base, name)
            found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def _read(root, path):
    try:
        with open(os.path.join(str(root), path), "rb") as handle:
            return handle.read(DIFF_LIMIT + 1)
    except Exception:
        return None


def _lines(data):
    """The file as lines of text, or None when it is not text.

    A NUL byte settles it without decoding anything, which is what `grep` and
    `git` both do and is right far more often than any heuristic worth the
    name.
    """
    if data is None or len(data) > DIFF_LIMIT or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


# ----------------------------------------------------------------- comparing


class FileChange(object):
    """One file that is not what it was."""

    __slots__ = ("path", "kind", "plus", "minus", "binary")

    def __init__(self, path, kind, plus=0, minus=0, binary=False):
        self.path = path
        self.kind = kind
        self.plus = int(plus)
        self.minus = int(minus)
        self.binary = bool(binary)

    def counts(self):
        """`+30 -4`, or nothing at all for a file that has no lines."""
        if self.binary:
            return "binary"
        parts = []
        if self.plus:
            parts.append("+%d" % self.plus)
        if self.minus:
            parts.append("-%d" % self.minus)
        return " ".join(parts)

    def line(self, width=0):
        return "%s  %-*s  %s" % (self.kind, width, self.path, self.counts())

    def __repr__(self):
        return "<%s %s %s>" % (self.kind, self.path, self.counts())


class Changes(object):
    """Everything that is different, and how much of it there is."""

    def __init__(self, entries=()):
        self.entries = list(entries)

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def empty(self):
        return not self.entries

    def of_kind(self, kind):
        return [entry for entry in self.entries if entry.kind == kind]

    def plus(self):
        return sum(entry.plus for entry in self.entries)

    def minus(self):
        return sum(entry.minus for entry in self.entries)

    def sentence(self):
        """One line: what a build is going to be, in the fewest words."""
        if self.empty():
            return "nothing changed"
        counts = []
        for kind, word in ((ADDED, "new"), (MODIFIED, "changed"),
                           (REMOVED, "removed")):
            found = len(self.of_kind(kind))
            if found:
                counts.append("%d %s" % (found, word))
        text = "%d file%s (%s)" % (len(self.entries),
                                   "" if len(self.entries) == 1 else "s",
                                   ", ".join(counts))
        lines = []
        if self.plus():
            lines.append("+%d" % self.plus())
        if self.minus():
            lines.append("-%d" % self.minus())
        return "%s, %s" % (text, " ".join(lines)) if lines else text

    def lines(self, limit=None):
        """A line per file, columns lined up, longest first is not the point —
        the order files come in is the order they are on disk, which is where
        somebody will go looking for them."""
        shown = self.entries if limit is None else self.entries[:limit]
        width = max([len(entry.path) for entry in shown] or [0])
        out = [entry.line(width).rstrip() for entry in shown]
        if limit is not None and len(self.entries) > limit:
            out.append("… and %d more" % (len(self.entries) - limit))
        return out


def compare(origin, work):
    """What `work` has that `origin` did not, and the other way round."""
    before = set(walk(origin))
    after = set(walk(work))
    entries = []
    for path in sorted(before | after):
        if path not in before:
            entries.append(_appeared(work, path, ADDED))
        elif path not in after:
            entries.append(_appeared(origin, path, REMOVED))
        else:
            change = _differs(origin, work, path)
            if change is not None:
                entries.append(change)
    return Changes(entries)


def _appeared(root, path, kind):
    lines = _lines(_read(root, path))
    if lines is None:
        return FileChange(path, kind, binary=True)
    if kind == ADDED:
        return FileChange(path, kind, plus=len(lines))
    return FileChange(path, kind, minus=len(lines))


def _differs(origin, work, path):
    """None when the file is the same, a FileChange when it is not."""
    old, new = _read(origin, path), _read(work, path)
    if old == new:
        return None
    old_lines, new_lines = _lines(old), _lines(new)
    if old_lines is None or new_lines is None:
        return FileChange(path, MODIFIED, binary=True)
    plus = minus = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, first, second, third, fourth in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            minus += second - first
        if tag in ("replace", "insert"):
            plus += fourth - third
    if not plus and not minus:
        # the bytes differ but the lines do not: a line ending, or a missing
        # newline at the end. Real, and worth saying, but not worth a count.
        return FileChange(path, MODIFIED)
    return FileChange(path, MODIFIED, plus=plus, minus=minus)


def unified(origin, work, path, context=3):
    """The diff of one file, as `diff -u` would write it."""
    old, new = _lines(_read(origin, path)), _lines(_read(work, path))
    if old is None or new is None:
        return ["%s: binary" % path]
    return [line.rstrip("\n") for line in difflib.unified_diff(
        old, new, fromfile="a/%s" % path, tofile="b/%s" % path,
        lineterm="", n=int(context))]


# ------------------------------------------------------------- what it says


def description(source, version, changes, limit=180):
    """The one line the client shows under a plugin's name.

    A plugin's metadata is flat `key: value` with no way to hold a newline, so
    the full report goes into the archive as a file and this is the sentence:
    where it came from, how much moved, and as many of the file names as fit.
    """
    head = "Patch of %s" % source
    if version:
        head += " %s" % version
    head = "%s — %s" % (head, changes.sentence())
    names = ["%s %s" % (entry.kind, entry.path) for entry in changes]
    while names:
        text = "%s: %s" % (head, ", ".join(names))
        if len(text) <= limit:
            return text
        names.pop()
    return head


def report(source, version, changes, name, when=None):
    """The whole story, for the file that goes inside the archive."""
    import time

    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when or time.time()))
    out = ["# %s" % name, "",
           "Built by extCLI from a patch workspace on %s." % stamp, "",
           "Source: %s%s" % (source, " %s" % version if version else ""), ""]
    if changes.empty():
        out.append("Nothing was changed.")
        return out
    out.append(changes.sentence())
    out.append("")
    out.extend(changes.lines())
    return out
