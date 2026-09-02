# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""The client's own code, as something a workspace can be opened on.

exteraGram is one APK with seven `.dex` files in it and fifty-odd thousand
classes. Three decisions shape everything here, and all three are about not
doing the obvious thing:

* **The dex files are not copied into the workspace.** They are sixty
  megabytes; the APK is already on the phone and can be read out of where it
  sits. What goes into `/patch` is a list of class names and a place to write
  hooks — small enough to sit under a mount somebody browses.

* **The index is built once and written down.** Fifty thousand class names is
  two megabytes of text and half a minute of work, and it is the thing
  everything else starts from: you cannot hook what you cannot find.

* **A listing is produced when it is asked for.** `patch dis` reads the one
  dex holding the one class. Laying out fifty thousand smali files would be
  hours and gigabytes to answer a question about one method.

What can actually be changed is the last piece and the one that decides the
shape of all of it: nothing here rewrites the APK. The client's code is read,
and a patch is a *plugin* that hooks it at runtime — which is why the
workspace holds hooks rather than edited smali.
"""

import os
import zipfile

from . import dex as dex_module
from ..utils import log

# What the workspace holds. `hooks` is the only one that is ever built into
# anything; the rest is what you read while deciding what to put in it.
INDEX = "classes.txt"
HOOKS = "hooks"
LISTINGS = "smali"
NOTES = "README.md"

# How often the caller hears about an index being built. Seven dex files with
# tens of thousands of classes each is half a minute on a phone, and half a
# minute with nothing on screen is a phone that looks broken.
REPORT_EVERY = 2000


def apk_paths():
    """Every APK making up the client, base first.

    Split APKs are ordinary now — the language and density pieces are separate
    files — and while the code is almost always in the base one, asking for
    all of them costs nothing and means a split build does not come up empty.
    """
    info = _application_info()
    if info is None:
        return []
    out = []
    try:
        base = str(info.sourceDir)
        if base:
            out.append(base)
    except Exception:
        pass
    try:
        for one in (info.splitSourceDirs or []):
            text = str(one)
            if text:
                out.append(text)
    except Exception:
        pass
    return out


def _application_info():
    """The client's own ApplicationInfo, however this build lets us reach it."""
    for get in (_from_loader, _from_fragment):
        try:
            context = get()
            if context is not None:
                return context.getApplicationInfo()
        except Exception:
            continue
    return None


def _from_loader():
    from org.telegram.messenger import ApplicationLoader

    return ApplicationLoader.applicationContext


def _from_fragment():
    from client_utils import get_last_fragment

    fragment = get_last_fragment()
    return fragment.getParentActivity() if fragment else None


class Client(object):
    """One APK, read a dex at a time.

    Nothing is held: a dex is opened, asked its question and dropped. Holding
    all seven would be sixty megabytes of a phone's memory for a tool that is
    open while somebody reads one method.
    """

    def __init__(self, path):
        self.path = str(path)

    def exists(self):
        return os.path.isfile(self.path)

    def _zip(self):
        return zipfile.ZipFile(self.path)

    def dex_names(self):
        """`classes.dex`, `classes2.dex`, … in the order the runtime reads."""
        try:
            with self._zip() as archive:
                found = [name for name in archive.namelist()
                         if name.endswith(".dex") and "/" not in name]
        except Exception as e:
            log.error("patch: cannot read %s" % self.path, e)
            return []
        return sorted(found, key=_dex_order)

    def dex(self, name):
        """One dex, parsed. The caller is expected to let it go."""
        with self._zip() as archive:
            return dex_module.Dex(archive.read(name))

    def size(self):
        try:
            return os.path.getsize(self.path)
        except Exception:
            return 0

    # ---------------------------------------------------------------- index

    def index(self, on_progress=None):
        """[(class name, which dex)] for the whole client.

        Only the type table of each dex is touched, so this is fifty thousand
        names without decoding a single instruction.
        """
        out = []
        for name in self.dex_names():
            try:
                found = self.dex(name).class_names()
            except Exception as e:
                log.error("patch: %s will not parse" % name, e)
                continue
            for position, class_name in enumerate(found):
                out.append((class_name, name))
                if on_progress is not None and position % REPORT_EVERY == 0:
                    _say(on_progress, "%s: %d classes" % (name, position))
            _say(on_progress, "%s: %d classes" % (name, len(found)))
        out.sort()
        return out

    def where(self, class_name):
        """Which dex holds a class, or None.

        Asked of each dex in turn rather than of the written index, because
        the index is a file somebody may have edited and this is used to go
        and read real code.
        """
        wanted = dex_module.type_name(dex_module.descriptor_of(class_name))
        for name in self.dex_names():
            try:
                if self.dex(name).has_class(wanted):
                    return name
            except Exception:
                continue
        return None

    # -------------------------------------------------------------- looking

    def search(self, needle, kind="classes", limit=200, on_progress=None):
        """Classes, methods or strings containing `needle`, across every dex.

        Strings are worth saying something about: a label somebody can see on
        screen is very often the fastest way into a client, because the code
        that puts it there is the code they want.
        """
        out = []
        for name in self.dex_names():
            if len(out) >= limit:
                break
            _say(on_progress, "searching %s" % name)
            try:
                one = self.dex(name)
                room = limit - len(out)
                if kind == "methods":
                    out.extend((name, found.full())
                               for found in one.find_methods(needle, room))
                elif kind == "strings":
                    out.extend((name, found)
                               for found in one.find_strings(needle, room))
                else:
                    out.extend((name, found)
                               for found in one.find_classes(needle, room))
            except Exception as e:
                log.error("patch: cannot search %s" % name, e)
        return out[:limit]


def _dex_order(name):
    """`classes.dex` first, then 2, 3 … rather than 10 before 2."""
    stem = name[len("classes"):-len(".dex")]
    return int(stem) if stem.isdigit() else 1


def _say(on_progress, text):
    if on_progress is None:
        return
    try:
        on_progress(text)
    except Exception:
        pass


# --------------------------------------------------------- laying it out


def lay_out(root, client, on_progress=None):
    """Writes a client workspace into `root`. Returns (ok, detail).

    What lands there is a list of every class, an empty place to write hooks,
    and a note saying what building it does — deliberately not the code
    itself, which stays in the APK and is read out of it when asked.
    """
    from . import hooks as hooks_module

    root = str(root)
    try:
        os.makedirs(os.path.join(root, HOOKS), exist_ok=True)
        os.makedirs(os.path.join(root, LISTINGS), exist_ok=True)
        # the helper every hook imports, and one that works, so the first
        # thing anybody does is read a hook rather than invent one
        _put(root, HOOKS, "_api.py", hooks_module.api())
        _put(root, HOOKS, "example.py", hooks_module.example())
        entries = client.index(on_progress=on_progress)
        if not entries:
            return False, "no classes found in %s" % client.path
        with open(os.path.join(root, INDEX), "w", encoding="utf-8") as handle:
            handle.write("# %d classes in %s\n"
                         % (len(entries), os.path.basename(client.path)))
            for class_name, dex_name in entries:
                handle.write("%s\t%s\n" % (class_name, dex_name))
        with open(os.path.join(root, NOTES), "w", encoding="utf-8") as handle:
            handle.write("\n".join(notes(client, len(entries))) + "\n")
    except Exception as e:
        log.error("patch: cannot lay out the client workspace", e)
        return False, "%s: %s" % (type(e).__name__, e)
    return True, root


def _put(root, directory, name, text):
    path = os.path.join(root, directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read_index(root, limit=None):
    """The written index back as [(class, dex)]."""
    out = []
    try:
        with open(os.path.join(str(root), INDEX), "r",
                  encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("#") or "\t" not in line:
                    continue
                class_name, _, dex_name = line.rstrip("\n").partition("\t")
                out.append((class_name, dex_name))
                if limit is not None and len(out) >= limit:
                    break
    except Exception:
        return []
    return out


def notes(client, classes):
    """The README that sits in the workspace."""
    return [
        "# The client, taken apart",
        "",
        "%d classes, read out of" % classes,
        "`%s`." % client.path,
        "",
        "The code is **not** copied in here — it is sixty megabytes and it is",
        "already on the phone. What is here is the index and a place to work.",
        "",
        "    %s      every class in the client, and which dex holds it" % INDEX,
        "    %s/         listings, written when you ask for one" % LISTINGS,
        "    %s/         what you write; the only thing that gets built" % HOOKS,
        "",
        "## Finding something",
        "",
        "    patch find <text>              classes whose name contains it",
        "    patch find <text> --methods    methods",
        "    patch find <text> --strings    string constants",
        "    patch dis <class>              what a class actually does",
        "",
        "Searching the strings is often the fastest way in, but search for",
        "the *key* rather than the words on screen. The client's visible text",
        "lives in its resources and is translated; what is in the code is the",
        "name it looks the text up by. `Delete for everyone` finds nothing;",
        "`DeleteForAll` finds the code that shows it.",
        "",
        "## Changing something",
        "",
        "    patch hook <class> <method>    a skeleton in %s/" % HOOKS,
        "    patch build --install          the hooks, as a plugin",
        "",
        "The APK is never rewritten. A patch is a plugin that hooks the",
        "client's methods as it loads, so turning the plugin off puts the",
        "client back exactly as it was — which is the whole reason to do it",
        "this way rather than repacking the app.",
        "",
        "Editing the files under `%s/` changes nothing by itself. They are" % LISTINGS,
        "there to be read: Dalvik bytecode cannot be assembled back into a",
        "running app without rebuilding it, and rebuilding it is what this",
        "deliberately does not do.",
    ]
