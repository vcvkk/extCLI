# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Reading and writing extCLI's own settings.

The SDK shipped with exteraGram 12.9.0 (1.4.5.0) has no `elyx` module, so
settings cannot be read through it. What does exist is BasePlugin.get_setting /
set_setting and the `plugin_settings` module, which is the same store the
settings page writes to. Those are tried in that order, with `elyx` last in case
a newer SDK provides it, and an in-memory dict so a missing store degrades to
"nothing persists" rather than an exception on every read.
"""

from ..utils import log

PLUGIN_ID = "extcli"

_plugin = None
_memory = {}
_backend = None  # remembered after the first successful read


def bind(plugin):
    """Called on load: BasePlugin's own accessors are the preferred path."""
    global _plugin, _backend
    _plugin = plugin
    _backend = None


def plugin():
    """The plugin instance the client gave us, if we have been bound yet.

    Anything that opens a console needs it, and the settings page is not the
    only thing that opens one.
    """
    return _plugin


def _from_plugin(key, default):
    if _plugin is None:
        raise LookupError("no plugin bound")
    return _plugin.get_setting(key, default)


def _from_module(key, default):
    import plugin_settings

    return plugin_settings.get_setting(PLUGIN_ID, key, default)


def _from_elyx(key, default):
    from elyx import settings

    return settings.get(key, default)


_READERS = (
    ("plugin", _from_plugin),
    ("module", _from_module),
    ("elyx", _from_elyx),
)


def get(key, default=None):
    """Setting value, or `default` when no store answers."""
    order = _READERS
    if _backend is not None:
        order = tuple(r for r in _READERS if r[0] == _backend) + \
            tuple(r for r in _READERS if r[0] != _backend)
    for name, reader in order:
        try:
            value = reader(key, default)
        except Exception:
            continue
        _remember(name)
        return default if value is None else value
    return _memory.get(key, default)


def _remember(name):
    global _backend
    if _backend != name:
        _backend = name
        log.log("store: using the %s settings backend" % name, debug=True)


def set(key, value):
    """Writes a setting. Returns True when it was stored somewhere durable."""
    if _plugin is not None:
        try:
            _plugin.set_setting(key, value)
            return True
        except Exception as e:
            log.log("store: plugin.set_setting failed: %s" % e, debug=True)
    try:
        import plugin_settings

        plugin_settings.set_setting(PLUGIN_ID, key, value)
        return True
    except Exception as e:
        log.log("store: plugin_settings.set_setting failed: %s" % e, debug=True)
    try:
        from elyx import settings

        settings.set(key, value)
        return True
    except Exception:
        pass
    _memory[key] = value
    log.error("store: nothing persistent available, %r kept in memory only" % key)
    return False


def all_settings():
    if _plugin is not None:
        try:
            values = _plugin.get_all_settings()
            if values:
                return dict(values)
        except Exception:
            pass
    try:
        import plugin_settings

        values = plugin_settings.get_all_settings(PLUGIN_ID)
        if values:
            return dict(values)
    except Exception:
        pass
    return dict(_memory)


def backend_name():
    """Which store answered last; shown by `host status`."""
    return _backend or ("memory" if _memory else "none")
