# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Reading and controlling installed plugins.

Wraps com.exteragram.messenger.plugins.PluginsController. Method names come
from the client's own dex (getPlugins, setPluginEnabled, togglePlugin,
loadPlugin/unloadPlugin, deletePlugin, pinPlugin, getPluginPath, ...), but each
call goes through reflect.try_call with alternatives so a rename in a future
client version degrades to a clear error instead of a crash.
"""

from ..utils import log
from . import reflect

CONTROLLER = "com.exteragram.messenger.plugins.PluginsController"


def controller():
    return reflect.instance(CONTROLLER)


def available():
    return controller() is not None


class PluginInfo(object):
    """Plain snapshot of one plugin, so the rest of the code never holds a
    Java object it might outlive."""

    def __init__(self, plugin_id, name=None, version=None, author=None,
                 enabled=None, path=None, description=None, pinned=None):
        self.id = plugin_id
        self.name = name or plugin_id
        self.version = version
        self.author = author
        self.enabled = enabled
        self.path = path
        self.description = description
        self.pinned = pinned

    @property
    def state(self):
        if self.enabled is None:
            return None
        return "on" if self.enabled else "off"

    def as_fields(self):
        rows = [("id", self.id), ("name", self.name)]
        if self.version:
            rows.append(("version", self.version))
        if self.author:
            rows.append(("author", self.author))
        if self.enabled is not None:
            rows.append(("state", "enabled" if self.enabled else "disabled"))
        if self.pinned is not None:
            rows.append(("pinned", "yes" if self.pinned else "no"))
        if self.path:
            rows.append(("path", self.path))
        if self.description:
            rows.append(("about", self.description))
        return rows


def _string(value):
    if value is None:
        return None
    text = str(value)
    return text if text and text != "null" else None


def _read_plugin(plugin_id, obj):
    """Pulls what we can off a client Plugin object, tolerating absences."""
    metadata = reflect.try_call(obj, ["getMetadata", "getPluginMetadata"],
                                key="plugin.metadata")
    source = metadata if metadata is not None else obj

    def read(names, key, target=None):
        return _string(reflect.try_call(target or source, names, key=key))

    name = read(["getName", "name"], "plugin.name") or plugin_id
    version = read(["getVersion", "version"], "plugin.version")
    author = read(["getAuthor", "author"], "plugin.author")
    description = read(["getDescription", "description"], "plugin.description")

    enabled = reflect.try_call(
        obj, ["isEnabled", "getEnabled", "isPluginActive", "isActive"],
        key="plugin.enabled",
    )
    if enabled is not None:
        enabled = bool(enabled)

    path = _string(reflect.try_call(obj, ["getPath", "getPluginPath", "getFilePath"],
                                    key="plugin.path"))
    if path is None:
        ctrl = controller()
        if ctrl is not None:
            path = _string(reflect.try_call(ctrl, ["getPluginPath"], str(plugin_id),
                                            key="controller.pluginPath"))

    pinned = None
    ctrl = controller()
    if ctrl is not None:
        pinned = reflect.try_call(ctrl, ["isPluginPinned"], str(plugin_id),
                                  key="controller.isPinned")
        if pinned is not None:
            pinned = bool(pinned)

    return PluginInfo(str(plugin_id), name, version, author, enabled, path,
                      description, pinned)


def _plugins_map():
    ctrl = controller()
    if ctrl is None:
        return []
    java_map = reflect.try_call(ctrl, ["getPlugins"], key="controller.getPlugins")
    if java_map is None:
        java_map = reflect.get_field(ctrl, "plugins")
    return reflect.java_map_items(java_map)


def list_plugins(include_self=True, self_id="extcli"):
    out = []
    for key, value in _plugins_map():
        plugin_id = str(key)
        if not include_self and plugin_id == self_id:
            continue
        try:
            out.append(_read_plugin(plugin_id, value))
        except Exception as e:
            log.error("plugins: cannot read %s" % plugin_id, e)
            out.append(PluginInfo(plugin_id))
    out.sort(key=lambda p: p.name.lower())
    return out


def get(plugin_id):
    ctrl = controller()
    if ctrl is None:
        return None
    obj = reflect.try_call(ctrl, ["getPlugin"], str(plugin_id), key="controller.getPlugin")
    if obj is None:
        for key, value in _plugins_map():
            if str(key) == str(plugin_id):
                obj = value
                break
    if obj is None:
        return None
    return _read_plugin(str(plugin_id), obj)


def find(query):
    """Plugins whose id or name contains `query`, case-insensitively."""
    needle = str(query).lower()
    return [p for p in list_plugins()
            if needle in p.id.lower() or needle in p.name.lower()]


def set_enabled(plugin_id, enabled):
    """Enables or disables a plugin. Returns (ok, detail)."""
    ctrl = controller()
    if ctrl is None:
        return False, "plugins controller unavailable"
    result = reflect.try_call(
        ctrl, ["setPluginEnabled"], str(plugin_id), bool(enabled),
        key="controller.setEnabled", default="__missing__",
    )
    if result != "__missing__":
        return True, "%s %s" % (plugin_id, "enabled" if enabled else "disabled")

    # older clients only expose a toggle
    current = get(plugin_id)
    if current is None:
        return False, "no such plugin: %s" % plugin_id
    if current.enabled is not None and bool(current.enabled) == bool(enabled):
        return True, "%s already %s" % (plugin_id, "enabled" if enabled else "disabled")
    result = reflect.try_call(ctrl, ["togglePlugin"], str(plugin_id),
                              key="controller.toggle", default="__missing__")
    if result == "__missing__":
        return False, "client exposes no way to change plugin state"
    return True, "%s %s" % (plugin_id, "enabled" if enabled else "disabled")


def reload(plugin_id):
    """Unload + load, which is how the client applies edited plugin code."""
    ctrl = controller()
    if ctrl is None:
        return False, "plugins controller unavailable"
    if get(plugin_id) is None:
        return False, "no such plugin: %s" % plugin_id
    unloaded = reflect.try_call(ctrl, ["unloadPlugin"], str(plugin_id),
                                key="controller.unload", default="__missing__")
    loaded = reflect.try_call(ctrl, ["loadPlugin"], str(plugin_id),
                              key="controller.load", default="__missing__")
    if unloaded == "__missing__" and loaded == "__missing__":
        return False, "client exposes no reload path"
    return True, "%s reloaded" % plugin_id


def uninstall(plugin_id):
    ctrl = controller()
    if ctrl is None:
        return False, "plugins controller unavailable"
    if get(plugin_id) is None:
        return False, "no such plugin: %s" % plugin_id
    result = reflect.try_call(ctrl, ["deletePlugin", "forceDeletePlugin"],
                              str(plugin_id), key="controller.delete",
                              default="__missing__")
    if result == "__missing__":
        return False, "client exposes no uninstall path"
    return True, "%s uninstalled" % plugin_id


def set_pinned(plugin_id, pinned):
    ctrl = controller()
    if ctrl is None:
        return False, "plugins controller unavailable"
    result = reflect.try_call(ctrl, ["setPluginPinned", "pinPlugin"], str(plugin_id),
                              bool(pinned), key="controller.setPinned",
                              default="__missing__")
    if result == "__missing__":
        result = reflect.try_call(ctrl, ["pinPlugin"], str(plugin_id),
                                  key="controller.pin", default="__missing__")
    if result == "__missing__":
        return False, "client exposes no pin path"
    return True, "%s %s" % (plugin_id, "pinned" if pinned else "unpinned")


# ------------------------------------------------------------ plugin settings

def get_settings(plugin_id):
    """All stored settings of a plugin, as a dict."""
    try:
        import plugin_settings

        values = plugin_settings.get_all_settings(str(plugin_id))
        return dict(values) if values else {}
    except Exception as e:
        log.log("plugins: get_all_settings(%s) failed: %s" % (plugin_id, e), debug=True)
    ctrl = controller()
    if ctrl is None:
        return {}
    values = reflect.try_call(ctrl, ["getAllPluginSettings"], str(plugin_id),
                              key="controller.allSettings")
    return {str(k): v for k, v in reflect.java_map_items(values)}


def get_setting(plugin_id, key, default=None):
    try:
        import plugin_settings

        return plugin_settings.get_setting(str(plugin_id), str(key), default)
    except Exception:
        return get_settings(plugin_id).get(str(key), default)


def set_setting(plugin_id, key, value):
    try:
        import plugin_settings

        plugin_settings.set_setting(str(plugin_id), str(key), value)
        return True, "%s.%s = %s" % (plugin_id, key, value)
    except Exception as e:
        return False, "cannot write setting: %s" % e


def clear_settings(plugin_id):
    try:
        import plugin_settings

        plugin_settings.clear_settings(str(plugin_id))
        return True, "settings of %s cleared" % plugin_id
    except Exception as e:
        return False, "cannot clear settings: %s" % e
