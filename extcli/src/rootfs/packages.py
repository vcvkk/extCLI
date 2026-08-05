# SPDX-License-Identifier: Apache-2.0

"""What goes into the container besides Alpine, and what it costs.

A minirootfs is four megabytes and can do almost nothing: busybox, musl and
apk. Everything anybody actually opens a shell for is a package away, and
having to know which packages is exactly the fiddling this removes.

None of it is bundled. The archive that ships with the plugin is 4 MB and the
smallest useful group is 13 MB compressed; bundling would make the plugin
several times larger and send the same bytes down again on every plugin
update, through Telegram rather than a package mirror. So they are offered
once, when the container is ready, and fetched if the answer is yes.

Sizes were measured against the aarch64 index of Alpine v3.24. A package's own
size is what the expanded list shows; a group's is what it and its
dependencies come to together, which is larger and is the number that matters
before pressing the button.
"""


# Where a package comes from. Alpine's own repositories for almost everything;
# Python's for the handful of things that are only published there, and that
# therefore need an interpreter in the container before they can be asked for
# at all.
APK = "apk"
PIP = "pip"


class Package(object):
    def __init__(self, name, size, summary, command=None):
        self.name = name
        self.size = size        # MB installed, this package alone
        self.summary = summary
        # what it puts on the PATH, when that is not its own name. It is how
        # "is this already here" is answered for anything pip installed, which
        # keeps no database this side can read.
        self.command = command or name


class Group(object):
    def __init__(self, name, title, summary, packages, download, installed,
                 default=False, kind=APK, needs=()):
        self.name = name
        self.title = title
        self.summary = summary
        self.packages = tuple(packages)
        self.download = download    # MB, compressed, dependencies included
        self.installed = installed  # MB, on disk, dependencies included
        self.default = default
        self.kind = kind
        # group names, any one of which makes this one possible. A Python
        # package cannot be installed into a container with no Python in it,
        # and offering it anyway would be offering an error message.
        self.needs = tuple(needs)

    @property
    def names(self):
        return tuple(item.name for item in self.packages)

    def package(self, name):
        for item in self.packages:
            if item.name == name:
                return item
        return None


GROUPS = (
    Group(
        "utils", "Utilities",
        "git, an editor, and the archivers a shell is unusable without",
        (
            Package("git", 7.3, "version control"),
            Package("curl", 0.3, "fetching things over http"),
            Package("nano", 0.3, "an editor that needs no learning"),
            Package("less", 0.3, "reading long output a page at a time"),
            Package("tar", 0.4, "archives, the GNU one"),
            Package("xz", 0.3, "the compressor most sources come in"),
            Package("unzip", 0.3, "opening zips"),
            Package("zip", 0.5, "making them"),
            Package("file", 11.6, "what a file actually is, by its contents"),
            Package("coreutils", 1.2, "ls, cp, sort — the full versions"),
            Package("grep", 0.1, "the full grep, with -P"),
            Package("sed", 0.1, "the full sed, with -i"),
        ),
        download=13, installed=38, default=True,
    ),
    Group(
        "python", "Python",
        "python3 with pip, and uv to install anything else",
        (
            Package("python3", 26.0, "the interpreter"),
            Package("py3-pip", 5.6, "installing packages the usual way"),
            Package("uv", 24.8, "installing them quickly, and tools with them"),
        ),
        download=26, installed=70,
    ),
    Group(
        "java", "Java runtime",
        "what jadx and every Android tool needs",
        (
            Package("openjdk17-jre-headless", 179.6, "Java 17, no desktop"),
        ),
        download=64, installed=189,
    ),
    Group(
        "javac", "Java compiler",
        "javac, and the modules that come with it",
        (
            Package("openjdk17-jdk", 10.0, "the compiler and its tools"),
        ),
        download=148, installed=291,
    ),
    Group(
        "build", "C toolchain",
        "gcc, g++ and make, for building from source",
        (
            Package("build-base", 251.0, "gcc, g++, binutils, musl-dev"),
            Package("make", 0.2, "make itself"),
            Package("pkgconf", 0.2, "finding libraries to build against"),
        ),
        download=86, installed=251,
    ),
    Group(
        "look", "For the look of it",
        "a fetch, a monitor, and things that are simply nice to watch",
        (
            Package("fastfetch", 2.2, "what this phone is, in one screen"),
            Package("btop", 1.5, "processes and memory, drawn"),
            Package("cmatrix", 0.1, "the green rain"),
            Package("cbonsai", 0.1, "a bonsai that grows while you watch"),
            Package("figlet", 0.7, "letters made out of letters"),
            Package("sl", 0.1, "the train that comes when you cannot type ls"),
        ),
        download=3, installed=8,
    ),
    Group(
        "pytools", "Python tools",
        "published on PyPI rather than in Alpine, so they need Python first",
        (
            Package("elyxbuilder", 12.0, "builds exteraGram plugins", "elyb"),
            Package("yt-dlp", 18.0, "downloads video from most places"),
            Package("httpie", 9.0, "an http client that reads like a sentence",
                    "http"),
        ),
        download=14, installed=39,
        kind=PIP, needs=("python",),
    ),
)

NAMES = tuple(group.name for group in GROUPS)
DEFAULT = tuple(group.name for group in GROUPS if group.default)


def group(name):
    for item in GROUPS:
        if item.name == name:
            return item
    return None


def default_selection():
    """{group: [package names]} as it is first offered.

    Everything inside a group is on; which *groups* are on is the question the
    first line of each row answers.
    """
    return {item.name: list(item.names) for item in GROUPS if item.default}


def selection_for(installed=(), satisfied=()):
    """What to open the question with, given what the container already has.

    The defaults, minus anything already installed: a tick against something
    that is there would be a promise to fetch it again, and the total under the
    button would be counting megabytes nobody is going to download.
    """
    installed = set(installed)
    chosen = {}
    for item in GROUPS:
        if not item.default:
            continue
        left = [name for name in item.names if name not in installed]
        if left:
            chosen[item.name] = left
    return Selection(chosen, satisfied=satisfied)


class Selection(object):
    """Which groups are on, and what is left ticked inside them.

    Kept apart from the dialog that shows it so the arithmetic — what will be
    installed, what it comes to — is settled here and tested without a screen.

    `satisfied` is what the container already has, by group: a Python tool can
    be installed into a container that already has Python without Python being
    ticked as well, and a group that is already there in full is not something
    to offer again.
    """

    def __init__(self, chosen=None, satisfied=()):
        self.satisfied = frozenset(satisfied)
        self.chosen = {}
        # an empty dict is an answer — nothing chosen — and only None means
        # "whatever the defaults are"
        chosen = default_selection() if chosen is None else chosen
        for name, names in chosen.items():
            if group(name) is not None:
                self.chosen[name] = list(names)
        self._drop_the_impossible()

    def is_on(self, name):
        return bool(self.chosen.get(name))

    def is_possible(self, name):
        """Is anything this group needs either ticked or already installed?"""
        item = group(name)
        if item is None:
            return False
        if not item.needs:
            return True
        return any(need in self.satisfied or self.is_on(need)
                   for need in item.needs)

    def needs_of(self, name):
        item = group(name)
        return () if item is None else item.needs

    def _drop_the_impossible(self):
        """Turns off whatever can no longer be installed.

        Untick Python and the Python tools go with it — they cannot be
        installed without it, and leaving them ticked would mean pressing
        Install and watching half of it fail.
        """
        changed = True
        while changed:
            changed = False
            for item in GROUPS:
                if self.is_on(item.name) and not self.is_possible(item.name):
                    self.chosen.pop(item.name, None)
                    changed = True

    def has(self, group_name, package):
        return package in self.chosen.get(group_name, ())

    def set_group(self, name, on):
        """Turning a group on ticks everything in it again."""
        item = group(name)
        if item is None:
            return
        if on:
            if not self.is_possible(name):
                return
            self.chosen[name] = list(item.names)
        else:
            self.chosen.pop(name, None)
            self._drop_the_impossible()

    def set_package(self, group_name, package, on):
        item = group(group_name)
        if item is None or item.package(package) is None:
            return
        if on and not self.is_possible(group_name):
            return
        current = self.chosen.get(group_name)
        if current is None:
            if not on:
                return
            current = self.chosen[group_name] = []
        if on:
            if package not in current:
                # kept in the order the group lists them, not the order they
                # were ticked back on
                current[:] = [name for name in item.names
                              if name in current or name == package]
        else:
            current[:] = [name for name in current if name != package]
            if not current:
                # a group with nothing left in it is a group that is off
                self.chosen.pop(group_name, None)
                self._drop_the_impossible()

    def packages(self):
        """Every package to install, in the order the groups are offered."""
        out = []
        for item in GROUPS:
            for name in item.names:
                if self.has(item.name, name) and name not in out:
                    out.append(name)
        return out

    def cost(self):
        """(MB to download, MB installed).

        A group that has had something removed is priced by what is left of
        it, in proportion — the dependencies are shared and cannot honestly be
        split, and a number that is roughly right beats none at all.
        """
        download = installed = 0.0
        for item in GROUPS:
            picked = self.chosen.get(item.name)
            if not picked:
                continue
            whole = sum(package.size for package in item.packages) or 1.0
            part = sum(item.package(name).size for name in picked
                       if item.package(name))
            share = min(1.0, max(part / whole, 0.15))
            download += item.download * share
            installed += item.installed * share
        return int(round(download)), int(round(installed))

    def command_words(self):
        """What this selection is, as arguments to `rootfs tools add`.

        A group that is wanted whole is named as a group — shorter, and it says
        what was meant — and one that has had something taken out of it is
        listed package by package. Either way the command in the console is a
        command anybody could have typed themselves.
        """
        words = []
        for item in GROUPS:
            picked = self.chosen.get(item.name)
            if not picked:
                continue
            if len(picked) == len(item.names):
                words.append(item.name)
            else:
                words.extend(picked)
        return words

    def sentence(self):
        packages = self.packages()
        if not packages:
            return "nothing"
        download, installed = self.cost()
        return "%d packages · about %d MB to download, %d MB on disk" % (
            len(packages), download, installed)


def missing(root, names, installed_check):
    """The packages in `names` that are not in the container yet."""
    return [name for name in names if not installed_check(root, name)]
