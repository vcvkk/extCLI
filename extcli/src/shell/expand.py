# SPDX-License-Identifier: Apache-2.0

"""Word expansion: parameters, command substitution, arithmetic, split, glob.

Order matters and follows the shell's: parameters and substitutions first, then
field splitting of *unquoted* results only, then pathname expansion. That is why
the lexer keeps quoting per word part — `$x` and "$x" differ only here.
"""

import glob as glob_module
import os
import re

from .lexer import DOUBLE, LITERAL, SINGLE

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPECIAL = "?#@*!$"
# arithmetic is evaluated only if it contains nothing but this
_ARITHMETIC_SAFE = re.compile(r"^[0-9+\-*/%() \t]*$")


class _FieldBreak(object):
    """Marks where a field must end even though nothing was quoted away.

    Only "$@" produces one. It is an object rather than a string so no amount
    of user data can be mistaken for it.
    """

    def __repr__(self):
        return "<field break>"


FIELD_BREAK = _FieldBreak()


def expand_word(word, env, substitute=None, do_glob=True, do_split=True):
    """Expands one Word into a list of fields."""
    pieces = []   # [(text, quoted)]
    for index, (quote, text) in enumerate(word.parts):
        if quote == SINGLE:
            pieces.append((text, True))
            continue
        if index == 0 and quote == LITERAL:
            text = _expand_tilde(text, env)
        expanded = _expand_text(text, env, substitute)
        for chunk in expanded:
            if chunk is FIELD_BREAK:
                pieces.append((FIELD_BREAK, False))
            else:
                pieces.append((chunk, quote == DOUBLE))

    fields = _split_fields(pieces, env) if do_split else [
        "".join(text for text, _ in pieces if text is not FIELD_BREAK)
    ]

    # an empty unquoted expansion disappears; a quoted one stays
    if not fields and word.is_quoted():
        fields = [""]

    if not do_glob:
        return fields

    out = []
    for field, globbable in zip(fields, _globbable_flags(pieces, fields, do_split)):
        if globbable and _has_glob(field):
            matches = _expand_glob(field, env)
            out.extend(matches if matches else [field])
        else:
            out.append(field)
    return out


def expand_to_string(word, env, substitute=None):
    """Expansion where splitting makes no sense: a redirection target, a
    variable's value, a case subject."""
    fields = expand_word(word, env, substitute, do_glob=False, do_split=False)
    return fields[0] if fields else ""


# --------------------------------------------------------------- parameters

def _expand_tilde(text, env):
    """`~` and `~/path` at the start of an unquoted word become the home path.

    Only the leading form is handled; `~user` needs a passwd database that an
    app sandbox does not have.
    """
    if not text.startswith("~"):
        return text
    if text == "~":
        return env.home
    if text.startswith("~/"):
        return env.home.rstrip("/") + text[1:]
    return text


def _expand_text(text, env, substitute):
    """Expands $... inside one part; returns a list of chunks.

    Chunks exist so that an unquoted `$var` containing spaces can still be split
    while the literal text around it cannot.
    """
    out = []
    buffer = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "$":
            buffer.append(char)
            i += 1
            continue
        if i + 1 >= len(text):
            buffer.append("$")
            break
        nxt = text[i + 1]

        if nxt == "(":
            if text.startswith("$((", i):
                value, i = _read_group(text, i + 2, "(", ")")
                # the outer ) of $(( )) still has to be consumed
                if i < len(text) and text[i] == ")":
                    i += 1
                buffer.append(_arithmetic(value, env))
                continue
            command, i = _read_group(text, i + 1, "(", ")")
            result = substitute(command) if substitute else ""
            buffer.append(result)
            continue

        if nxt == "{":
            body, i = _read_group(text, i + 1, "{", "}")
            buffer.append(_expand_braced(body, env, substitute))
            continue

        if nxt == "@":
            # "$@" is the one expansion that splits even inside quotes: each
            # positional parameter becomes its own field. Without this a
            # wrapper like `run() { "$@"; }` passes its whole argument list as
            # a single word and the command is never found.
            out.append("".join(buffer))
            buffer = []
            values = list(env.positional)
            for index, value in enumerate(values):
                if index:
                    out.append(FIELD_BREAK)
                out.append(value)
            i += 2
            continue

        if nxt in _SPECIAL or nxt.isdigit():
            buffer.append(env.get(nxt))
            i += 2
            continue

        match = _NAME.match(text, i + 1)
        if match:
            buffer.append(env.get(match.group(0)))
            i = match.end()
            continue

        buffer.append("$")
        i += 1

    if buffer:
        out.append("".join(buffer))
    return out or [""]


def _expand_braced(body, env, substitute):
    """${name}, ${name:-default}, ${name:=default}, ${name:+alt}, ${#name}."""
    if body.startswith("#") and len(body) > 1:
        return str(len(env.get(body[1:])))
    for operator in (":-", ":=", ":+", "-", "=", "+"):
        if operator in body:
            name, _, argument = body.partition(operator)
            value = env.get(name)
            argument_text = "".join(_expand_text(argument, env, substitute))
            if operator in (":-", "-"):
                return value if value else argument_text
            if operator in (":=", "="):
                if not value:
                    env.set(name, argument_text)
                    return argument_text
                return value
            return argument_text if value else ""
    return env.get(body)


def _arithmetic(expression, env):
    """$(( ... )) over integers.

    Variables are substituted first, then the result is checked against a strict
    whitelist before evaluation — nothing but digits, operators and parentheses
    can reach the evaluator.
    """
    text = expression
    for name in sorted(set(_NAME.findall(text)), key=len, reverse=True):
        value = env.get(name, "0") or "0"
        text = re.sub(r"\b%s\b" % re.escape(name), value, text)
    text = text.replace("$", "")
    if not _ARITHMETIC_SAFE.match(text):
        return "0"
    try:
        return str(int(eval(text, {"__builtins__": {}}, {})))  # noqa: S307
    except Exception:
        return "0"


def _read_group(text, index, opener, closer):
    """Reads a balanced group; `index` points at the opener."""
    depth = 0
    start = index + 1
    i = index
    while i < len(text):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return text[start:], len(text)


# ----------------------------------------------------------- field splitting

def _split_fields(pieces, env):
    ifs = env.ifs
    if not ifs:
        return _split_on_breaks_only(pieces)
    fields = []
    current = []
    for text, quoted in pieces:
        if text is FIELD_BREAK:
            fields.append("".join(current))
            current = []
            continue
        if quoted:
            current.append(text)
            continue
        token = []
        for char in text:
            if char in ifs:
                if token:
                    current.append("".join(token))
                    token = []
                if current:
                    fields.append("".join(current))
                    current = []
            else:
                token.append(char)
        if token:
            current.append("".join(token))
    if current:
        fields.append("".join(current))
    return [field for field in fields if field != ""] or ([] if not pieces else
                                                          _keep_empty(pieces))


def _split_on_breaks_only(pieces):
    """IFS is empty, so nothing splits except "$@"."""
    fields = []
    current = []
    for text, _quoted in pieces:
        if text is FIELD_BREAK:
            fields.append("".join(current))
            current = []
            continue
        current.append(text)
    fields.append("".join(current))
    return fields


def _keep_empty(pieces):
    joined = "".join(text for text, _ in pieces if text is not FIELD_BREAK)
    return [joined] if joined else []


def _globbable_flags(pieces, fields, did_split):
    """Whether each field may be glob-expanded.

    A field is globbable when no quoted piece contributed to it. Tracking that
    exactly through splitting is more machinery than it earns here, so a word
    with any quoting at all is treated as non-globbable.
    """
    any_quoted = any(quoted for text, quoted in pieces
                     if text is not FIELD_BREAK)
    return [not any_quoted] * len(fields)


# -------------------------------------------------------- pathname expansion

def _has_glob(text):
    return any(char in text for char in "*?[")


def _expand_glob(pattern, env):
    """Matches a pattern against the filesystem, relative to the shell's cwd.

    Two translations, because only the filesystem knows what matches and only
    the shell knows what to call it: the pattern goes out in the machine's
    terms and every match comes back in the shell's.
    """
    absolute = pattern.startswith("/")
    prefix = env.cwd.rstrip("/") + "/"
    wanted = pattern if absolute else prefix + pattern
    try:
        matches = sorted(glob_module.glob(env.host(wanted)))
    except Exception:
        return []
    found = [env.guest(match) for match in matches]
    if absolute:
        return found
    return [match[len(prefix):] if match.startswith(prefix) else match
            for match in found]


def matches_pattern(text, pattern):
    """Shell pattern match for `case` branches."""
    import fnmatch

    return fnmatch.fnmatchcase(text, pattern)
