# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Device and client facts, collected for the probe and `host` commands."""

from ..utils import log


def _build():
    from android.os import Build

    return Build


def abi():
    try:
        return str(_build().SUPPORTED_ABIS[0])
    except Exception:
        try:
            return str(_build().CPU_ABI)
        except Exception:
            return None


def supported_abis():
    try:
        return [str(a) for a in _build().SUPPORTED_ABIS]
    except Exception:
        return []


def api_level():
    try:
        return int(_build().VERSION.SDK_INT)
    except Exception:
        return None


def android_release():
    try:
        return str(_build().VERSION.RELEASE)
    except Exception:
        return None


def device_model():
    try:
        b = _build()
        return "%s %s" % (str(b.MANUFACTURER), str(b.MODEL))
    except Exception:
        return None


def app_version():
    try:
        from org.telegram.messenger import BuildVars

        return str(BuildVars.BUILD_VERSION_STRING)
    except Exception:
        return None


def sdk_version():
    """Version of the plugin SDK the client ships.

    Only plain values are read, never called: this module also exposes
    setup_hooks(), __start__() and check_safemode(), and invoking an SDK
    internal because it happened to be callable is not worth a version string.
    """
    try:
        import _sdk_version
    except Exception:
        return None
    for attr in ("__version__", "version_str", "version"):
        try:
            value = getattr(_sdk_version, attr, None)
        except Exception:
            continue
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (tuple, list)) and value:
            return ".".join(str(part) for part in value)
    return None


def plugin_version():
    """Version from elyx metainfo when available, else our own meta.yml."""
    try:
        from elyx import metainfo

        value = metainfo["version"]
        if value:
            return str(value)
    except Exception:
        pass
    from . import meta

    return meta.version()


def probe_facts():
    """A backends.probe.HostFacts filled in from the client."""
    from ..backends.probe import HostFacts
    from .paths import native_lib_dir

    try:
        native = native_lib_dir()
    except Exception:
        native = None

    return HostFacts(
        abi=abi(),
        api_level=api_level(),
        android_release=android_release(),
        app_version=app_version(),
        sdk_version=sdk_version(),
        native_lib_dir=native,
    )


def describe():
    """(label, value) rows for `host status`."""
    rows = [
        ("plugin", plugin_version() or "?"),
        ("client", app_version() or "?"),
        ("sdk", sdk_version() or "?"),
        ("android", "%s (api %s)" % (android_release() or "?", api_level() or "?")),
        ("device", device_model() or "?"),
        ("abi", abi() or "?"),
        ("python", _python_version()),
    ]
    try:
        from . import i18n, store

        rows.append(("settings", store.backend_name()))
        rows.append(("strings", i18n.backend_name()))
    except Exception:
        pass
    return rows


def _python_version():
    try:
        import sys

        return "%d.%d.%d" % sys.version_info[:3]
    except Exception:
        return "?"


def log_environment():
    for label, value in describe():
        log.log("host: %s = %s" % (label, value), debug=True)
