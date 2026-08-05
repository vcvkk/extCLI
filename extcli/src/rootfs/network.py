# SPDX-License-Identifier: Apache-2.0

"""Giving the guest a resolver.

`apk update` came back with "DNS: transient error (try again later)" against
every repository, which reads like a network fault and is not one. A minirootfs
ships no /etc/resolv.conf — on a real machine something writes it at boot — and
musl with no nameserver has nowhere to ask. The certificates and the repository
list are both already there, so this is the only thing between the guest and
the network.

Android will not lend its own: there is no /etc/resolv.conf on the phone either,
because resolution goes through netd rather than through a file. The servers
have to be asked for through the framework and written out here.

A file rather than a setting, and rewritten rather than kept, because which
resolver a phone is using changes with the network it is on — the one that was
right on wifi is unreachable on mobile data.
"""

import os

from . import layout

PATH = "/etc/resolv.conf"

# Where to point when the phone will not say. Not a preference: a guest with no
# resolver at all cannot fetch anything, and these two answer from everywhere.
FALLBACK = ("1.1.1.1", "8.8.8.8")

HEADER = "# written by extCLI"


def contents(servers):
    """The file musl reads. Pure, so the formatting is tested rather than
    hoped for."""
    lines = [HEADER]
    for server in servers or ():
        server = str(server).strip()
        if not server:
            continue
        # an IPv6 scope means "this interface", which is not this process's to
        # interpret and which musl will not parse
        server = server.split("%")[0]
        lines.append("nameserver %s" % server)
    if len(lines) == 1:
        lines.extend("nameserver %s" % server for server in FALLBACK)
    return "\n".join(lines) + "\n"


def read(root):
    try:
        with open(_path(root), "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


def servers_in(text):
    """The nameservers a resolv.conf names, in order."""
    found = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            found.append(parts[1])
    return found


def _path(root):
    return layout.translate(root, PATH) or os.path.join(root, "etc/resolv.conf")


def write(root, servers):
    """Writes the resolver, if it is not already what it should be.

    Returns (written, servers). Not writing an identical file keeps a rootfs on
    a read-only day from failing for no reason, and keeps the mtime meaningful.
    """
    wanted = contents(servers)
    if read(root) == wanted:
        return False, servers_in(wanted)
    path = _path(root)
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(wanted)
    except Exception:
        return False, servers_in(read(root))
    return True, servers_in(wanted)


def describe(root):
    """A row for `rootfs status`: what the guest would ask, if anything."""
    found = servers_in(read(root))
    if not found:
        return "none — the guest cannot resolve a name"
    return ", ".join(found)
