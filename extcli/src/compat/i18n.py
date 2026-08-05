# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Localized strings.

SDK 1.4.5.3 exposes `elyx.strings` (an elyxcore.localization.Strings with
`get(key, default)`), and that is the preferred path: it follows the client's
language and handles plural forms. Older SDKs — including the 1.4.5.0 that
exteraGram 12.9.0 ships — have no `elyx` at all, so the same JSON files that
ship in the archive are read directly as a fallback.

Command output stays English on purpose; this is only for UI chrome.
"""

import json
import os

DEFAULT_LANGUAGE = "en"

_strings = None      # elyx.strings, once resolved
_strings_tried = False
_files = {}          # language -> {key: value}
_language = None


def _elyx_strings():
    global _strings, _strings_tried
    if _strings_tried:
        return _strings
    _strings_tried = True
    try:
        from elyx import strings

        _strings = strings
    except Exception:
        _strings = None
    return _strings


def locales_dir(root=None):
    if root is None:
        from . import paths

        root = paths.plugin_root()
    return os.path.join(root, "locales")


def language():
    """Two-letter code of the client's language, best effort."""
    global _language
    if _language is not None:
        return _language
    strings = _elyx_strings()
    if strings is not None:
        try:
            _language = str(strings.get_current_language())[:2].lower()
            return _language
        except Exception:
            pass
    try:
        from java.util import Locale

        _language = str(Locale.getDefault().getLanguage())[:2].lower()
    except Exception:
        _language = DEFAULT_LANGUAGE
    return _language


def _load_file(lang, root=None):
    # keyed by directory as well as language: a cached miss for one root must
    # not answer for another
    directory = locales_dir(root)
    key = (lang, directory)
    if key in _files:
        return _files[key]
    path = os.path.join(directory, "strings_%s.json" % lang)
    try:
        with open(path, "r", encoding="utf-8") as f:
            _files[key] = json.load(f)
    except Exception:
        _files[key] = {}
    return _files[key]


def get(key, default=None, root=None):
    """Localized string for `key`, or `default`."""
    strings = _elyx_strings()
    if strings is not None:
        try:
            value = strings.get(key, default)
            if value is not None:
                text = str(value)
                # a missing entry can come back as the key itself
                if text and text != key:
                    return text
        except Exception:
            pass
    entries = _load_file(language(), root)
    if key in entries:
        return entries[key]
    if language() != DEFAULT_LANGUAGE:
        entries = _load_file(DEFAULT_LANGUAGE, root)
        if key in entries:
            return entries[key]
    return default if default is not None else key


PLURAL_FORMS = ("one", "few", "many")

# Where a number picks a third form as well, by the rule the Slavic languages
# share: 1 and 21 take one word, 2-4 and 22-24 another, the rest a third — so
# "1 пакет", "22 пакета", "25 пакетов" are three different words for the same
# thing. Everything else here gets the two forms English has.
_THREE_FORMS = ("ru", "uk", "be")


def plural_form(count, lang=None):
    """Which of `PLURAL_FORMS` a number takes in a language."""
    if lang is None:
        lang = language()
    try:
        count = abs(int(count))
    except (TypeError, ValueError):
        count = 0
    if lang not in _THREE_FORMS:
        return "one" if count == 1 else "many"
    if count % 100 in (11, 12, 13, 14):
        return "many"
    last = count % 10
    if last == 1:
        return "one"
    if last in (2, 3, 4):
        return "few"
    return "many"


def plural(key, count, default=None, root=None):
    """get() for a phrase whose words change with a number.

    `key` names a family — `key_one`, `key_few`, `key_many` — and the number
    picks one of them. The caller does the formatting: where the number goes in
    the sentence is part of the translation, not something to decide here.
    """
    missing = "\x00"
    text = get("%s_%s" % (key, plural_form(count)), missing, root)
    if text == missing:
        text = get("%s_many" % key, default, root)
    return text


def format(key, default=None, root=None, **kwargs):
    """get() plus str.format, for strings with placeholders."""
    text = get(key, default, root)
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except Exception:
        return text


def reset():
    """Drops caches; used by tests and after a language change."""
    global _strings, _strings_tried, _language
    _strings = None
    _strings_tried = False
    _language = None
    _files.clear()


def backend_name():
    return "elyx" if _elyx_strings() is not None else "bundled json"
