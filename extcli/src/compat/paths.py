# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Filesystem locations extCLI works with.

Nothing here is hardcoded to a plugin-loader layout: the client builds those
paths in Kotlin and they are not stable across versions. The plugin root is
derived from this file's own location, and the settings directory is asked
from the SDK.
"""

import os

_PLUGIN_ID = "extcli"


def _app_context():
    from org.telegram.messenger import ApplicationLoader

    return ApplicationLoader.applicationContext


_files_dir = None


def real(path):
    """The one name a directory answers to.

    /data/user/0/<package> and /data/data/<package> are the same place, and
    Android hands out the first while the kernel answers with the second: a
    guest's own /proc/<pid>/cwd reads `/data/data/...`. Two names for one
    directory means a prefix that never matches, which is how a trigger came
    back as "cannot open" against a path that was plainly there — the mount
    table said one name and the process said the other.

    So everything is asked for by the name the kernel uses.
    """
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def files_dir():
    """Canonical, and remembered: it is asked for on the way to every other
    path here, and following the links costs a syscall each time."""
    global _files_dir

    if _files_dir is None:
        _files_dir = real(str(_app_context().getFilesDir().getAbsolutePath()))
    return _files_dir


def cache_dir():
    return str(_app_context().getCacheDir().getAbsolutePath())


def external_cache_dir():
    d = _app_context().getExternalCacheDir()
    return str(d.getAbsolutePath()) if d else cache_dir()


def package_name():
    return str(_app_context().getPackageName())


def code_cache_dir():
    """Android's own dir for generated code. Not executable to us either, but
    it is the one with the most promising name, so it gets measured."""
    try:
        return str(_app_context().getCodeCacheDir().getAbsolutePath())
    except Exception:
        return os.path.join(cache_dir(), "code_cache")


def no_backup_dir():
    try:
        return str(_app_context().getNoBackupFilesDir().getAbsolutePath())
    except Exception:
        return os.path.join(files_dir(), "no_backup")


def external_files_dir():
    try:
        d = _app_context().getExternalFilesDir(None)
        return str(d.getAbsolutePath()) if d else external_cache_dir()
    except Exception:
        return external_cache_dir()


def storage_dir():
    """The phone's own storage, as a guest is offered it under /sdcard.

    Asked of Android rather than hardcoded, because /sdcard is a symlink whose
    target has moved across releases — but /sdcard itself has outlived every
    one of those moves, so it is the fallback.
    """
    try:
        from java import jclass

        directory = jclass("android.os.Environment").getExternalStorageDirectory()
        path = str(directory.getAbsolutePath()) if directory else ""
        if path and os.path.isdir(path):
            # /sdcard is a symlink to where the storage really is, and the
            # kernel answers about a process with the real name
            return real(path)
    except Exception:
        pass
    return real("/sdcard")


def exec_candidates():
    """Every directory extCLI might be able to write to, for the exec scan.

    Ordered by how plausible each one is, most first, so a reader who stops
    after two lines has still read the interesting ones.
    """
    rows = []
    for label, fn in (
        ("code_cache", code_cache_dir),
        ("cache", cache_dir),
        ("files", files_dir),
        ("no_backup", no_backup_dir),
        ("extcli data", data_dir),
        ("external cache", external_cache_dir),
        ("external files", external_files_dir),
        ("downloads", downloads_dir),
    ):
        try:
            rows.append((label, fn()))
        except Exception:
            continue
    # not ours to write to, but if it ever became so it would change everything
    rows.append(("/data/local/tmp", "/data/local/tmp"))
    return rows


def native_lib_dir():
    """Directory the APK's own .so files were unpacked to — the one place in
    the app that is executable. Useful when probing exec restrictions."""
    return str(_app_context().getApplicationInfo().nativeLibraryDir)


_MARKER = "meta.yml"
_plugin_root = None


def _this_file():
    """Absolute path of this source file.

    `__file__` is not set for plugin modules: the client's loader execs the code
    without it, so reading it raises NameError on device even though it works
    everywhere else. The frame's code filename carries the same path and is
    always present — it is what tracebacks print.
    """
    candidates = []
    for name in ("__file__", "__spec__"):
        value = globals().get(name)
        if name == "__spec__":
            value = getattr(value, "origin", None)
        if value:
            candidates.append(str(value))
    try:
        import sys

        candidates.append(sys._getframe().f_code.co_filename)
    except Exception:
        pass
    for candidate in candidates:
        if candidate and os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
    return None


def _root_candidates():
    """Places the plugin root could be, best first."""
    out = []

    source = _this_file()
    if source:
        # <root>/src/compat/paths.py -> up three
        out.append(os.path.normpath(
            os.path.join(os.path.dirname(source), "..", "..")
        ))

    # newer SDKs can answer this directly
    try:
        from elyxcore import _importer

        root = _importer.importer.get_caller_root()
        if root:
            out.append(str(root))
    except Exception:
        pass

    # installed layouts seen in the wild: files/plugins[/ElyxPlugins]/<id>/<dir>
    try:
        base = os.path.join(files_dir(), "plugins")
        for middle in ("ElyxPlugins", ""):
            parent = os.path.join(base, middle) if middle else base
            for name in (_PLUGIN_ID, ""):
                holder = os.path.join(parent, name) if name else parent
                out.append(os.path.join(holder, _PLUGIN_ID))
                out.append(holder)
    except Exception:
        pass

    seen = []
    for path in out:
        normalized = os.path.normpath(path)
        if normalized not in seen:
            seen.append(normalized)
    return seen


def _looks_like_root(path):
    """Is this the directory the plugin was installed into?

    meta.yml is the nicest marker but not a reliable one: this client extracts
    only the Python it needs to import and serves everything else through the
    SDK's asset API, so on device there is no meta.yml, no res/ and no dex/
    next to src/ — only src/ itself. The one thing always on disk is the code
    we are running from, so that is what the root is verified by. meta.yml,
    where a build or another client does leave it, is still preferred.
    """
    try:
        if os.path.isfile(os.path.join(path, _MARKER)):
            return True
        # the very file this function lives in, found under the candidate
        return os.path.isfile(os.path.join(path, "src", "compat", "paths.py"))
    except Exception:
        return False


def plugin_root():
    """Directory the plugin was installed into (contains at least src/).

    Verified rather than trusted: an unverified guess would send the dex loader
    and the locale reader to a directory that does not exist, and they would
    report "missing" instead of "wrong path". A candidate carrying meta.yml
    wins over one recognised only by its src/ tree, so a layout that does have
    the metadata still resolves to it.
    """
    global _plugin_root
    if _plugin_root is not None:
        return _plugin_root

    candidates = _root_candidates()
    # meta.yml first, then the structural match, so nothing regresses on a
    # client that does lay the whole archive out
    for wants_meta in (True, False):
        for path in candidates:
            try:
                has_meta = os.path.isfile(os.path.join(path, _MARKER))
                if wants_meta and not has_meta:
                    continue
                if wants_meta or _looks_like_root(path):
                    _plugin_root = path
                    return path
            except Exception:
                continue

    # nothing verified: keep the best guess so callers get a usable path, but
    # do not cache it — the client may still be unpacking the archive
    from ..utils import log

    tried = ", ".join(candidates[:4]) if candidates else "no candidates"
    log.error("paths: cannot locate the plugin root; tried %s" % tried,
              trace=False)
    return candidates[0] if candidates else files_dir()


def reset_plugin_root():
    """Forgets the resolved root; used by tests and after a reinstall.

    The assets directory is found relative to the root, so it is forgotten too
    — otherwise a reinstall that moved the plugin would keep reading the old
    place.
    """
    global _plugin_root
    _plugin_root = None
    reset_res_dir()


_res_dir = None

# A file that is in res/ and nowhere else, used to find where the client put
# the assets. Two deep, so the directory two levels above it is res/ itself.
_RES_ANCHOR = "config/fastfetch.jsonc"


def _has_assets(directory):
    """Does this directory actually hold what res/ is supposed to?"""
    try:
        return (os.path.isdir(os.path.join(directory, "native"))
                or os.path.isfile(os.path.join(directory, _RES_ANCHOR))
                or os.path.isdir(os.path.join(directory, "rootfs")))
    except Exception:
        return False


def res_dir():
    """Where the bundled assets are — native binaries, the rootfs tarball, the
    dex renderer's data.

    Usually `plugin_root()/res`. But this client does not extract the archive
    whole: it lays out only the Python it imports and serves everything else
    through the SDK's asset API, so res/ is not next to src/ and the syscall
    map, the loader and Alpine all read as "not built". When the obvious place
    is empty the SDK is asked where the assets really are, by resolving one
    known file to its real path on disk and climbing back to the assets root.
    """
    global _res_dir
    if _res_dir is not None:
        return _res_dir

    guess = os.path.join(plugin_root(), "res")
    if _has_assets(guess):
        _res_dir = guess
        return guess

    found = _res_via_sdk()
    if found and _has_assets(found):
        _res_dir = found
        from ..utils import log

        log.log("paths: assets resolved through the SDK to %s" % found,
                debug=True)
        return found

    # not found and not cached: the client may still be materialising assets,
    # so a later call gets to try again rather than being stuck with the guess
    return guess


def reset_res_dir():
    global _res_dir
    _res_dir = None


def _res_via_sdk():
    """The assets directory as the SDK knows it, or None.

    The asset facade is rooted at what refmap calls `assets` (our res/), so a
    file resolved through it sits at `<res>/<relative path>`. Stripping the
    relative path off the real path it returns leaves `<res>`.
    """
    path = _asset_file(_RES_ANCHOR)
    if not path:
        return None
    path = real(path)
    suffix = _RES_ANCHOR.replace("/", os.sep)
    if path.endswith(suffix):
        return path[: -len(suffix)].rstrip(os.sep)
    # the SDK handed back something shaped differently; its own parent chain
    # is the next best guess at the assets root
    return os.path.dirname(os.path.dirname(path))


def _asset_file(rel):
    """The real on-disk path of a bundled asset, however the SDK exposes it."""
    for resolve in (_asset_via_facade, _asset_via_class):
        try:
            path = resolve(rel)
        except Exception:
            path = None
        if path and os.path.exists(path):
            return path
    return None


def _asset_via_facade(rel):
    from elyx import assets

    return _asset_path(assets.get(rel))


def _asset_via_class(rel):
    from elyxcore.assets import Asset

    return _asset_path(Asset.from_path(rel))


def _asset_path(asset):
    """A str path out of an Asset, whether java_file is a property or a call."""
    if asset is None:
        return None
    java_file = getattr(asset, "java_file", None)
    if callable(java_file):
        java_file = java_file()
    if java_file is None:
        return None
    try:
        return str(java_file.getAbsolutePath())
    except Exception:
        return str(java_file)


def dex_dir():
    return os.path.join(plugin_root(), "dex")


def plugins_dir():
    """Where the client keeps installed plugins and plugin_settings.json."""
    try:
        import plugin_settings

        path = getattr(plugin_settings, "plugins_dir_path", None)
        if callable(path):
            path = path()
        if path:
            return str(path)
    except Exception:
        pass
    return os.path.join(files_dir(), "plugins")


# ---------------------------------------------------------------- extCLI data

def data_dir():
    """Root of everything extCLI writes. Survives plugin updates, unlike
    plugin_root(), which is replaced on reinstall."""
    return os.path.join(files_dir(), _PLUGIN_ID)


def home_dir():
    """Working directory a fresh shell session starts in."""
    return os.path.join(data_dir(), "home")


def tmp_dir():
    return os.path.join(data_dir(), "tmp")


def state_dir():
    """Probe cache, shell history, session state."""
    return os.path.join(data_dir(), "state")


def themes_dir():
    """User-supplied themes; bundled ones live in res/themes."""
    return os.path.join(data_dir(), "themes")


def rootfs_dir():
    return os.path.join(data_dir(), "rootfs")


def patch_dir():
    """Where the client is taken apart and put back together.

    Its own directory rather than a corner of the rootfs: what is in it is the
    client's code, it is laid out on demand and thrown away by the command that
    made it, and none of it belongs to Alpine.
    """
    return os.path.join(data_dir(), "patch")


def native_dir():
    """Our own ELF binaries, per ABI.

    They cannot be executed from here directly — SELinux refuses execve inside
    the app's data directory — but /system/bin/linker64 will run them, which is
    what backends/linker.py does. A plugin cannot put anything in the client's
    APK, so this is the only place native code can live.
    """
    from . import host

    return os.path.join(data_dir(), "native", host.abi() or "unknown")


def downloads_dir():
    """Public Downloads folder, where `dump` writes files the user can share."""
    try:
        from android.os import Environment

        d = Environment.getExternalStoragePublicDirectory(
            Environment.DIRECTORY_DOWNLOADS
        )
        if d is not None:
            return str(d.getAbsolutePath())
    except Exception:
        pass
    return external_cache_dir()


def ensure_dirs():
    """Creates the extCLI directory tree; returns the paths that were made."""
    created = []
    for path in (data_dir(), home_dir(), tmp_dir(), state_dir(), themes_dir(),
                 native_dir(), patch_dir()):
        if not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
                created.append(path)
            except Exception:
                pass
    return created


def describe():
    """(label, path, exists) rows for `host paths` and diagnostics."""
    rows = []
    for label, fn in (
        ("files", files_dir),
        ("cache", cache_dir),
        ("plugin", plugin_root),
        ("plugins", plugins_dir),
        ("data", data_dir),
        ("home", home_dir),
        ("nativeLib", native_lib_dir),
        ("downloads", downloads_dir),
    ):
        try:
            path = fn()
            rows.append((label, path, os.path.exists(path)))
        except Exception as e:
            rows.append((label, "unavailable (%s)" % e, False))
    return rows
