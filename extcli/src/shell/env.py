# SPDX-License-Identifier: Apache-2.0

"""Shell state: variables, functions, aliases, cwd, exit status."""

import os

DEFAULT_IFS = " \t\n"


class HostPaths(object):
    """The default: a shell whose paths are the machine's own.

    Every path this shell handles is a path in the world it believes it is in.
    With a rootfs that world is the guest's, and something has to turn its `/`
    into a real directory; without one the two are the same thing and this
    hands back what it was given. Duck-typed rather than imported so `shell/`
    keeps knowing nothing about rootfs.
    """

    active = False

    def host(self, path):
        return path

    def visible(self, path):
        return True

    def guest(self, path):
        return path

    def home(self):
        return None


def normalise(text):
    """`.`, `..` and repeated slashes taken out, textually.

    Textual because it has to answer for paths that do not exist yet and for a
    world this process is not standing in — and because `..` must stop at the
    root rather than walking out of it.
    """
    parts = []
    for part in str(text).split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


class Env(object):
    def __init__(self, cwd="/", variables=None, home=None, paths=None):
        self.paths = paths or HostPaths()
        self.cwd = cwd
        self.home = home or cwd
        self.variables = {
            "IFS": DEFAULT_IFS,
            "PS1": "$ ",
            "HOME": self.home,
            "PWD": cwd,
            "SHELL": "extcli",
        }
        if variables:
            self.variables.update(variables)
        self.exported = set(["HOME", "PWD", "SHELL"])
        self.functions = {}
        self.aliases = {}
        self.status = 0
        self.positional = []
        # set by `exit`; the executor stops and the console closes
        self.exit_requested = False
        self.exit_code = 0

    # ------------------------------------------------------------- variables

    def get(self, name, default=""):
        if name == "?":
            return str(self.status)
        if name == "#":
            return str(len(self.positional))
        if name == "0":
            return "extcli"
        if name.isdigit():
            index = int(name) - 1
            if 0 <= index < len(self.positional):
                return self.positional[index]
            return default
        if name == "@" or name == "*":
            return " ".join(self.positional)
        return self.variables.get(name, default)

    def has(self, name):
        return name in self.variables

    def set(self, name, value, export=False):
        self.variables[name] = "" if value is None else str(value)
        if export:
            self.exported.add(name)

    def unset(self, name):
        self.variables.pop(name, None)
        self.exported.discard(name)
        self.functions.pop(name, None)

    def export(self, name, value=None):
        if value is not None:
            self.set(name, value)
        self.exported.add(name)

    def environment(self):
        """What an external process should see."""
        out = {name: self.variables[name] for name in self.exported
               if name in self.variables}
        out.setdefault("PWD", self.cwd)
        out.setdefault("HOME", self.home)
        return out

    @property
    def ifs(self):
        return self.variables.get("IFS", DEFAULT_IFS)

    # ------------------------------------------------------------------- cwd

    def chdir(self, path):
        """Changes directory. Returns (ok, detail)."""
        target = self.resolve(path)
        if not self.paths.visible(target):
            return False, ("%s is not mounted — turn it on in extCLI's "
                           "settings" % target)
        if not os.path.isdir(self.host(target)):
            return False, "no such directory: %s" % path
        self.cwd = target
        self.set("PWD", self.cwd)
        return True, self.cwd

    def resolve(self, path):
        """Absolute path for a user-typed one, honouring ~ and the cwd.

        In the shell's own terms, which are the guest's when there is a rootfs.
        Whatever opens a file wants `host` instead.
        """
        text = str(path)
        if text.startswith("~"):
            rest = text[1:].lstrip("/")
            text = self.home.rstrip("/") + "/" + rest if rest else self.home
        if not text.startswith("/"):
            text = self.cwd.rstrip("/") + "/" + text
        return normalise(text)

    def host(self, path):
        """Where a path really is, for whatever is about to open it."""
        return self.paths.host(self.resolve(path))

    def guest(self, path):
        """The other direction, for whatever has just been told a real path."""
        return self.paths.guest(path)

    def display_cwd(self):
        """cwd with the home directory shortened to ~, as a prompt shows it."""
        if self.cwd == self.home:
            return "~"
        if self.cwd.startswith(self.home.rstrip("/") + "/"):
            return "~/" + self.cwd[len(self.home.rstrip("/")) + 1:]
        return self.cwd

    # ------------------------------------------------------------- functions

    def define_function(self, name, body):
        self.functions[name] = body

    def function(self, name):
        return self.functions.get(name)

    # --------------------------------------------------------------- aliases

    def set_alias(self, name, value):
        self.aliases[name] = value

    def alias(self, name):
        return self.aliases.get(name)

    def unset_alias(self, name):
        return self.aliases.pop(name, None) is not None
