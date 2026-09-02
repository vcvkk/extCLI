# SPDX-License-Identifier: Apache-2.0

"""Reading, and carefully changing, a plugin that shipped as bytecode.

ElyxBuilder can compile a plugin, and most published ones are compiled: the
archive holds `.pyc` files and no source at all. Opening one of those as a
workspace gives a tree nothing can be done with, which is where a patch tool
stops being useful exactly when it would be most useful.

Two things are honest to do about that, and this module does both:

* **Read it.** The disassembly is exact — it is what the interpreter will
  actually run, not a guess at the source that produced it. Every string, name
  and number in the file is there to be found, which is usually the question
  anyway: what does this thing talk to, what does it call itself, what is the
  limit it refuses to go past.

* **Change what can be changed exactly.** A constant is a value in a table,
  and swapping one for another of the same kind leaves every jump, every
  offset and every line number where it was. A URL, a label, a limit, a
  timeout — the things most patches are actually about — are all constants,
  and rewriting one round-trips perfectly.

What this deliberately does not do is pretend to decompile. Python 3.11's
bytecode has no working decompiler; what comes out of the ones that claim to
handle it is functions with empty bodies, and a patch built on that would be a
patch that quietly deletes code. Better to say plainly that source is not
recoverable and offer the two operations that are exact.
"""

import importlib.util
import io
import marshal
import os

# The 16-byte header a .pyc has carried since 3.7: magic, flags, and either a
# timestamp and size or a source hash.
HEADER = 16

MAGIC = importlib.util.MAGIC_NUMBER

SUFFIX = ".pyc"


def is_compiled(path):
    return str(path).endswith(SUFFIX)


def readable(path):
    """(ok, why not). A file this interpreter can actually take apart.

    A `.pyc` from another Python is not a lesser version of this one, it is a
    different format, and `marshal` reading it would not fail so much as
    produce nonsense. Refusing it by its magic number is the only safe answer
    this can give.
    """
    try:
        with open(str(path), "rb") as handle:
            magic = handle.read(4)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    if len(magic) < 4:
        return False, "not a compiled file"
    if magic != MAGIC:
        return False, ("compiled by another Python (%s, this one is %s)"
                       % (_magic_text(magic), _magic_text(MAGIC)))
    return True, "ok"


def _magic_text(magic):
    return "".join("%02x" % byte for byte in magic)


def load(path):
    """(header bytes, code object). Raises if it cannot be read."""
    with open(str(path), "rb") as handle:
        data = handle.read()
    if len(data) <= HEADER:
        raise ValueError("not a compiled file")
    if data[:4] != MAGIC:
        raise ValueError("compiled by another Python")
    return data[:HEADER], marshal.loads(data[HEADER:])


def dump(path, header, code):
    """Writes a code object back, keeping the header it came with.

    The header holds the source's timestamp and size, and leaving them alone
    matters: the interpreter compares them against the `.py` beside the file
    to decide whether to recompile. There is no `.py` in a compiled plugin, so
    nothing will — but writing a header that claims otherwise would be a lie
    told for no reason.
    """
    with open(str(path), "wb") as handle:
        handle.write(header)
        handle.write(marshal.dumps(code))


# ------------------------------------------------------------------ reading


def listing(code, limit=None):
    """The disassembly, as `dis` writes it, nested functions included."""
    import dis

    out = io.StringIO()
    dis.dis(code, file=out)
    lines = out.getvalue().splitlines()
    if limit is not None and len(lines) > limit:
        lines = lines[:limit] + ["… and %d more lines"
                                 % (len(lines) - limit)]
    return lines


def strings(code, minimum=1):
    """Every text constant in a code object and everything nested in it.

    Sorted and de-duplicated: the same string appears in a dozen places and
    what is being asked is what the file says, not how many times it says it.
    """
    found = set()
    for value in _constants(code):
        if isinstance(value, str) and len(value) >= minimum:
            found.add(value)
    return sorted(found)


def names(code):
    """Every name the code looks up — what it calls, and on what."""
    found = set()
    _walk(code, lambda one: found.update(one.co_names))
    return sorted(found)


def _constants(code):
    out = []
    _walk(code, lambda one: out.extend(
        value for value in one.co_consts
        if not isinstance(value, type(code))))
    return out


def _walk(code, visit):
    visit(code)
    for value in code.co_consts:
        if isinstance(value, type(code)):
            _walk(value, visit)


def summary(code):
    """(rows) describing a compiled file, for `patch code` to print."""
    strings_found = strings(code)
    return [
        ("module", code.co_filename or "?"),
        ("functions", str(len(_nested(code)))),
        ("names", str(len(names(code)))),
        ("strings", str(len(strings_found))),
    ]


def _nested(code):
    out = []
    for value in code.co_consts:
        if isinstance(value, type(code)):
            out.append(value)
            out.extend(_nested(value))
    return out


# ----------------------------------------------------------------- changing


def replace(code, old, new):
    """A copy of `code` with every constant equal to `old` replaced.

    Recursive, because a string inside a function is a constant of that
    function's own code object and not of the module's. Returns (code, count);
    a count of zero means nothing matched and the file is best left alone.

    Only the constant table moves. Every instruction, jump target, line number
    and name stays exactly where it was, which is what makes this safe in a
    way that anything resembling decompilation is not.
    """
    count = [0]

    def rebuild(one):
        consts = []
        for value in one.co_consts:
            if isinstance(value, type(one)):
                consts.append(rebuild(value))
            elif _same(value, old):
                consts.append(new)
                count[0] += 1
            else:
                consts.append(value)
        return one.replace(co_consts=tuple(consts))

    return rebuild(code), count[0]


def _same(value, wanted):
    """Equal *and* of the same kind.

    In Python `1 == True` and `0 == False`, so a plain `==` here would rewrite
    a boolean when asked to rewrite a number. That is the sort of change that
    would work in testing and go wrong months later.
    """
    if type(value) is not type(wanted):
        return False
    return value == wanted


def rewrite(path, old, new):
    """Replaces a constant in a compiled file on disk. Returns (count, detail).

    A count of zero writes nothing: a file rewritten to exactly what it
    already was would still show up as changed in `patch diff`, because a
    remarshalled code object is not byte-identical to the one that was read.
    """
    ok, why = readable(path)
    if not ok:
        return 0, why
    try:
        header, code = load(path)
        changed, count = replace(code, old, new)
        if not count:
            return 0, "%s is not a constant in %s" % (
                _short(old), os.path.basename(str(path)))
        dump(path, header, changed)
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)
    return count, "replaced %d occurrence%s" % (count,
                                                "" if count == 1 else "s")


def _short(value, limit=40):
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def compiled_files(root):
    """Every `.pyc` in a tree, by its path relative to the root."""
    from . import workspace

    return [path for path in workspace.walk(root) if is_compiled(path)]
