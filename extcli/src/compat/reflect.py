# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Reflection helpers for talking to client classes.

Method names in exteraGram are stable enough to use, but signatures are not
guaranteed across versions, and Chaquopy cannot import arbitrary Java packages.
So calls go through java.lang.reflect, matched by name and argument count, with
a list of candidate names where the client has renamed things before.

`describe` backs the `host class` command: on a device it prints the real
signatures, which is how the next version of this file gets pinned down.
"""

from ..utils import log

_MODIFIER_STATIC = 0x8

_class_cache = {}
_resolved = {}


def find_class(name):
    """Loads a Java class by fully-qualified name, or None."""
    if name in _class_cache:
        return _class_cache[name]
    cls = None
    try:
        from hook_utils import find_class as _find

        cls = _find(name)
    except Exception as e:
        log.error("reflect: find_class(%s) failed" % name, e)
    _class_cache[name] = cls
    if cls is None:
        log.log("reflect: class not found: %s" % name)
    return cls


def _methods(target, name, count, static=False):
    try:
        cls = target if hasattr(target, "getMethods") else target.getClass()
    except Exception:
        return []
    out = []
    try:
        for m in cls.getMethods():
            if m.getName() != name:
                continue
            if len(m.getParameterTypes()) != count:
                continue
            is_static = (m.getModifiers() & _MODIFIER_STATIC) != 0
            if static and not is_static:
                continue
            out.append(m)
    except Exception as e:
        log.error("reflect: listing %s failed" % name, e)
    return out


def call_static(cls, name, *args):
    for m in _methods(cls, name, len(args), static=True):
        return m.invoke(None, *args)
    raise AttributeError("no static %s(%d args)" % (name, len(args)))


def call(obj, name, *args):
    """Calls an instance method by name and argument count."""
    for m in _methods(obj, name, len(args)):
        return m.invoke(obj, *args)
    raise AttributeError("no method %s(%d args) on %s" % (name, len(args), obj))


def try_call(obj, names, *args, **kwargs):
    """First of `names` that exists and does not raise.

    `key` names the lookup for caching and logging, so a rename is resolved
    once per process rather than on every call.
    """
    key = kwargs.get("key")
    default = kwargs.get("default")
    if key and key in _resolved:
        resolved = _resolved[key]
        if resolved is None:
            return default
        try:
            return call(obj, resolved, *args)
        except Exception:
            pass  # fall through and re-resolve; the client may have changed
    for name in names:
        try:
            value = call(obj, name, *args)
        except AttributeError:
            continue
        except Exception as e:
            log.log("reflect: %s raised %s" % (name, e), debug=True)
            continue
        if key:
            _resolved[key] = name
            log.log("reflect: %s -> %s" % (key, name), debug=True)
        return value
    if key:
        _resolved[key] = None
    log.log("reflect: none of %s available" % ", ".join(names), debug=True)
    return default


def get_field(obj, name, default=None):
    try:
        from hook_utils import get_private_field

        return get_private_field(obj, name)
    except Exception:
        pass
    try:
        field = obj.getClass().getDeclaredField(name)
        field.setAccessible(True)
        return field.get(obj)
    except Exception:
        return default


def static_field(cls, name, default=None):
    try:
        field = cls.getDeclaredField(name)
        field.setAccessible(True)
        return field.get(None)
    except Exception:
        return default


def instance(class_name, getter_names=("getInstance",)):
    """Singleton accessor: PluginsController.getInstance() and friends."""
    cls = find_class(class_name)
    if cls is None:
        return None
    for name in getter_names:
        try:
            return call_static(cls, name)
        except Exception:
            continue
    return None


def java_map_items(java_map):
    """(key, value) pairs out of a java.util.Map."""
    out = []
    if java_map is None:
        return out
    try:
        iterator = java_map.entrySet().iterator()
        while iterator.hasNext():
            entry = iterator.next()
            out.append((entry.getKey(), entry.getValue()))
    except Exception as e:
        log.error("reflect: reading map failed", e)
    return out


def java_list_items(java_list):
    out = []
    if java_list is None:
        return out
    try:
        for i in range(java_list.size()):
            out.append(java_list.get(i))
    except Exception as e:
        log.error("reflect: reading list failed", e)
    return out


def describe(class_name, filter_text=None, include_fields=True):
    """Signatures of a class, for `host class <fqn>`.

    This is a development tool: it is how signatures get confirmed on a real
    device instead of guessed from a decompiled dex.
    """
    cls = find_class(class_name)
    if cls is None:
        return None
    needle = (filter_text or "").lower()
    methods = []
    try:
        for m in cls.getMethods():
            name = str(m.getName())
            if needle and needle not in name.lower():
                continue
            params = ", ".join(_simple_name(t) for t in m.getParameterTypes())
            static = "static " if (m.getModifiers() & _MODIFIER_STATIC) else ""
            methods.append("%s%s(%s) -> %s" % (
                static, name, params, _simple_name(m.getReturnType())
            ))
    except Exception as e:
        log.error("reflect: describe methods failed", e)
    fields = []
    if include_fields:
        try:
            for f in cls.getDeclaredFields():
                name = str(f.getName())
                if needle and needle not in name.lower():
                    continue
                fields.append("%s: %s" % (name, _simple_name(f.getType())))
        except Exception as e:
            log.error("reflect: describe fields failed", e)
    return {"methods": sorted(set(methods)), "fields": sorted(set(fields))}


def _simple_name(java_type):
    try:
        name = str(java_type.getName())
    except Exception:
        return "?"
    return name.rsplit(".", 1)[-1]
