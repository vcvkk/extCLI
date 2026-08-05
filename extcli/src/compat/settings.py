# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""The client's own settings.

exteraGram keeps its preferences in a SharedPreferences file named
`exteraconfig` (confirmed in the client's dex), and Telegram keeps its own in
`mainconfig` and friends. Because SharedPreferences can enumerate itself, the
`config` command does not need a hardcoded list of keys: it reads what is
actually there, with the types the client stored.

Writing is typed on purpose. A key the client reads with getBoolean must be
written with putBoolean, or the client throws ClassCastException the next time
it starts — which is a bricked settings screen, not a wrong value.
"""

from ..utils import log

# name -> what it holds; `exteraconfig` first because it is the interesting one
STORES = (
    ("exteraconfig", "exteraGram settings"),
    ("mainconfig", "Telegram main settings"),
    ("userconfig", "account settings"),
    ("themeconfig", "theme settings"),
)

DEFAULT_STORE = "exteraconfig"


def _context():
    from org.telegram.messenger import ApplicationLoader

    return ApplicationLoader.applicationContext


def preferences(store=DEFAULT_STORE):
    return _context().getSharedPreferences(str(store), 0)


def available():
    try:
        return preferences() is not None
    except Exception:
        return False


def store_names():
    return [name for name, _description in STORES]


def _to_python(value):
    """Java value -> Python, keeping booleans distinct from ints."""
    if value is None:
        return None
    text = str(value)
    class_name = ""
    try:
        class_name = str(value.getClass().getSimpleName())
    except Exception:
        pass
    if class_name == "Boolean" or text in ("true", "false"):
        return text == "true"
    if class_name in ("Integer", "Long"):
        try:
            return int(text)
        except ValueError:
            return text
    if class_name == "Float" or class_name == "Double":
        try:
            return float(text)
        except ValueError:
            return text
    if class_name == "HashSet" or class_name == "ArraySet":
        try:
            return [str(item) for item in value.toArray()]
        except Exception:
            return text
    return text


def all_values(store=DEFAULT_STORE):
    """Every stored key, as Python values."""
    out = {}
    try:
        entries = preferences(store).getAll()
    except Exception as e:
        log.error("settings: cannot read %s" % store, e)
        return out
    try:
        iterator = entries.entrySet().iterator()
        while iterator.hasNext():
            entry = iterator.next()
            out[str(entry.getKey())] = _to_python(entry.getValue())
    except Exception as e:
        log.error("settings: cannot iterate %s" % store, e)
    return out


def get(key, default=None, store=DEFAULT_STORE):
    values = all_values(store)
    return values.get(str(key), default)


def has(key, store=DEFAULT_STORE):
    try:
        return bool(preferences(store).contains(str(key)))
    except Exception:
        return False


def type_name(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (list, tuple)):
        return "set"
    return "string"


def set_value(key, value, store=DEFAULT_STORE, existing=None):
    """Writes a value with the type the client expects.

    `existing` is the current value when there is one; its type wins, because
    the client reads the key with a typed getter and a changed type is a crash
    rather than a wrong setting.
    """
    key = str(key)
    try:
        editor = preferences(store).edit()
    except Exception as e:
        return False, "cannot open %s: %s" % (store, e)

    target = existing if existing is not None else value
    try:
        if isinstance(target, bool):
            editor.putBoolean(key, bool(_coerce_bool(value)))
        elif isinstance(target, int):
            editor.putInt(key, int(value))
        elif isinstance(target, float):
            editor.putFloat(key, float(value))
        else:
            editor.putString(key, str(value))
        editor.apply()
    except Exception as e:
        return False, "cannot write %s: %s" % (key, e)
    return True, "%s = %s" % (key, value)


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ValueError("%r is not a boolean" % value)


def remove(key, store=DEFAULT_STORE):
    try:
        preferences(store).edit().remove(str(key)).apply()
    except Exception as e:
        return False, "cannot remove %s: %s" % (key, e)
    return True, "%s removed" % key


def search(query, store=DEFAULT_STORE):
    """Keys whose name or value contains `query`."""
    needle = str(query).lower()
    out = {}
    for key, value in all_values(store).items():
        if needle in key.lower() or needle in str(value).lower():
            out[key] = value
    return out


def describe():
    """(store, key count) rows, for `config stores`."""
    rows = []
    for name, description in STORES:
        try:
            count = len(all_values(name))
        except Exception:
            count = 0
        rows.append((name, "%d keys - %s" % (count, description)))
    return rows
