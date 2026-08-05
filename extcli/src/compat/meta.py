# SPDX-License-Identifier: Apache-2.0

"""Reading the plugin's own meta.yml.

Without `elyx` there is no metainfo API, but meta.yml ships inside the archive
next to src/, and ElyxBuilder appends build details to it (build number, date,
whether it was compiled). Parsed by hand: the file is flat `key: value`, and
pulling in a YAML parser for that would be silly.
"""

import os

_cache = None
_cache_path = None


def meta_path(root=None):
    if root is None:
        from . import paths

        root = paths.plugin_root()
    return os.path.join(root, "meta.yml")


def parse(text):
    """Flat key/value pairs; comments and blank lines ignored."""
    out = {}
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load(root=None, refresh=False):
    global _cache, _cache_path
    path = meta_path(root)
    if _cache is not None and not refresh and _cache_path == path:
        return _cache
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = parse(f.read())
    except Exception:
        data = {}
    _cache = data
    _cache_path = path
    return data


def get(key, default=None):
    value = load().get(key)
    return default if value in (None, "") else value


def version():
    return get("version")


def build_info():
    """(label, value) rows describing the build, for `host status`."""
    data = load()
    rows = []
    for key, label in (("version", "version"), ("buildNum", "build"),
                       ("buildDate", "built"), ("compiled", "compiled"),
                       ("elybVer", "elyb")):
        value = data.get(key)
        if value:
            rows.append((label, value))
    return rows
