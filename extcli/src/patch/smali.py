# SPDX-License-Identifier: Apache-2.0

"""Dalvik bytecode, made readable.

`patch.dex` finds a method's instructions; this turns them into the listing
MT Manager and every other Android tool shows — one instruction per line, with
registers, and with every class, method, field and string resolved back into
its name instead of the table index it is stored as.

That last part is what makes it worth reading at all. Raw, an instruction says
`invoke-virtual {v0, v1}, method@0x1a4`; resolved, it says which method of
which class, and a listing of resolved calls is something a person can follow.

Two things make a disassembler either right or quietly wrong, and both are
here rather than anywhere else:

* **Sizes.** Every instruction format has a fixed length in 16-bit code units,
  and one wrong length desynchronises everything after it — the listing does
  not fail, it turns into plausible nonsense. So the whole table is written
  out, and the tests walk every method in a real dex and insist the stream
  ends exactly where the method says it does.

* **Registers.** The arguments of a method live in the registers at the *end*
  of its frame, and every tool names those `p0`, `p1` … rather than by their
  real numbers. Getting that wrong makes a listing that reads fine and points
  at the wrong values.
"""

import struct

from . import dex as dex_module

# What a constant pool index refers to, which is decided by the opcode and not
# by the instruction format: `21c` is a string for const-string and a field
# for sget.
STRING, TYPE, FIELD, METHOD, PROTO, CALLSITE, HANDLE = (
    "string", "type", "field", "method", "proto", "call_site", "method_handle")

# opcode, mnemonic, format, and what its index means. Written out in full: a
# generated table would be shorter and there would be nothing to check it
# against, and one wrong entry here is a listing that lies.
_TABLE = """
00 nop 10x
01 move 12x
02 move/from16 22x
03 move/16 32x
04 move-wide 12x
05 move-wide/from16 22x
06 move-wide/16 32x
07 move-object 12x
08 move-object/from16 22x
09 move-object/16 32x
0a move-result 11x
0b move-result-wide 11x
0c move-result-object 11x
0d move-exception 11x
0e return-void 10x
0f return 11x
10 return-wide 11x
11 return-object 11x
12 const/4 11n
13 const/16 21s
14 const 31i
15 const/high16 21h
16 const-wide/16 21s
17 const-wide/32 31i
18 const-wide 51l
19 const-wide/high16 21h
1a const-string 21c string
1b const-string/jumbo 31c string
1c const-class 21c type
1d monitor-enter 11x
1e monitor-exit 11x
1f check-cast 21c type
20 instance-of 22c type
21 array-length 12x
22 new-instance 21c type
23 new-array 22c type
24 filled-new-array 35c type
25 filled-new-array/range 3rc type
26 fill-array-data 31t
27 throw 11x
28 goto 10t
29 goto/16 20t
2a goto/32 30t
2b packed-switch 31t
2c sparse-switch 31t
2d cmpl-float 23x
2e cmpg-float 23x
2f cmpl-double 23x
30 cmpg-double 23x
31 cmp-long 23x
32 if-eq 22t
33 if-ne 22t
34 if-lt 22t
35 if-ge 22t
36 if-gt 22t
37 if-le 22t
38 if-eqz 21t
39 if-nez 21t
3a if-ltz 21t
3b if-gez 21t
3c if-gtz 21t
3d if-lez 21t
44 aget 23x
45 aget-wide 23x
46 aget-object 23x
47 aget-boolean 23x
48 aget-byte 23x
49 aget-char 23x
4a aget-short 23x
4b aput 23x
4c aput-wide 23x
4d aput-object 23x
4e aput-boolean 23x
4f aput-byte 23x
50 aput-char 23x
51 aput-short 23x
52 iget 22c field
53 iget-wide 22c field
54 iget-object 22c field
55 iget-boolean 22c field
56 iget-byte 22c field
57 iget-char 22c field
58 iget-short 22c field
59 iput 22c field
5a iput-wide 22c field
5b iput-object 22c field
5c iput-boolean 22c field
5d iput-byte 22c field
5e iput-char 22c field
5f iput-short 22c field
60 sget 21c field
61 sget-wide 21c field
62 sget-object 21c field
63 sget-boolean 21c field
64 sget-byte 21c field
65 sget-char 21c field
66 sget-short 21c field
67 sput 21c field
68 sput-wide 21c field
69 sput-object 21c field
6a sput-boolean 21c field
6b sput-byte 21c field
6c sput-char 21c field
6d sput-short 21c field
6e invoke-virtual 35c method
6f invoke-super 35c method
70 invoke-direct 35c method
71 invoke-static 35c method
72 invoke-interface 35c method
74 invoke-virtual/range 3rc method
75 invoke-super/range 3rc method
76 invoke-direct/range 3rc method
77 invoke-static/range 3rc method
78 invoke-interface/range 3rc method
7b neg-int 12x
7c not-int 12x
7d neg-long 12x
7e not-long 12x
7f neg-float 12x
80 neg-double 12x
81 int-to-long 12x
82 int-to-float 12x
83 int-to-double 12x
84 long-to-int 12x
85 long-to-float 12x
86 long-to-double 12x
87 float-to-int 12x
88 float-to-long 12x
89 float-to-double 12x
8a double-to-int 12x
8b double-to-long 12x
8c double-to-float 12x
8d int-to-byte 12x
8e int-to-char 12x
8f int-to-short 12x
90 add-int 23x
91 sub-int 23x
92 mul-int 23x
93 div-int 23x
94 rem-int 23x
95 and-int 23x
96 or-int 23x
97 xor-int 23x
98 shl-int 23x
99 shr-int 23x
9a ushr-int 23x
9b add-long 23x
9c sub-long 23x
9d mul-long 23x
9e div-long 23x
9f rem-long 23x
a0 and-long 23x
a1 or-long 23x
a2 xor-long 23x
a3 shl-long 23x
a4 shr-long 23x
a5 ushr-long 23x
a6 add-float 23x
a7 sub-float 23x
a8 mul-float 23x
a9 div-float 23x
aa rem-float 23x
ab add-double 23x
ac sub-double 23x
ad mul-double 23x
ae div-double 23x
af rem-double 23x
b0 add-int/2addr 12x
b1 sub-int/2addr 12x
b2 mul-int/2addr 12x
b3 div-int/2addr 12x
b4 rem-int/2addr 12x
b5 and-int/2addr 12x
b6 or-int/2addr 12x
b7 xor-int/2addr 12x
b8 shl-int/2addr 12x
b9 shr-int/2addr 12x
ba ushr-int/2addr 12x
bb add-long/2addr 12x
bc sub-long/2addr 12x
bd mul-long/2addr 12x
be div-long/2addr 12x
bf rem-long/2addr 12x
c0 and-long/2addr 12x
c1 or-long/2addr 12x
c2 xor-long/2addr 12x
c3 shl-long/2addr 12x
c4 shr-long/2addr 12x
c5 ushr-long/2addr 12x
c6 add-float/2addr 12x
c7 sub-float/2addr 12x
c8 mul-float/2addr 12x
c9 div-float/2addr 12x
ca rem-float/2addr 12x
cb add-double/2addr 12x
cc sub-double/2addr 12x
cd mul-double/2addr 12x
ce div-double/2addr 12x
cf rem-double/2addr 12x
d0 add-int/lit16 22s
d1 rsub-int 22s
d2 mul-int/lit16 22s
d3 div-int/lit16 22s
d4 rem-int/lit16 22s
d5 and-int/lit16 22s
d6 or-int/lit16 22s
d7 xor-int/lit16 22s
d8 add-int/lit8 22b
d9 rsub-int/lit8 22b
da mul-int/lit8 22b
db div-int/lit8 22b
dc rem-int/lit8 22b
dd and-int/lit8 22b
de or-int/lit8 22b
df xor-int/lit8 22b
e0 shl-int/lit8 22b
e1 shr-int/lit8 22b
e2 ushr-int/lit8 22b
fa invoke-polymorphic 45cc method
fb invoke-polymorphic/range 4rcc method
fc invoke-custom 35c call_site
fd invoke-custom/range 3rc call_site
fe const-method-handle 21c method_handle
ff const-method-type 21c proto
"""

# How many 16-bit code units each format occupies. One wrong number here does
# not fail, it turns the rest of the method into plausible nonsense — which is
# why the tests walk a real dex and insist every stream ends exactly on its
# stated length.
WIDTHS = {
    "10x": 1, "12x": 1, "11n": 1, "11x": 1, "10t": 1,
    "20t": 2, "22x": 2, "21t": 2, "21s": 2, "21h": 2, "21c": 2, "23x": 2,
    "22b": 2, "22t": 2, "22s": 2, "22c": 2,
    "30t": 3, "32x": 3, "31i": 3, "31t": 3, "31c": 3, "35c": 3, "3rc": 3,
    "45cc": 4, "4rcc": 4,
    "51l": 5,
}


def _load():
    table = {}
    for line in _TABLE.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        table[int(parts[0], 16)] = (parts[1], parts[2],
                                    parts[3] if len(parts) > 3 else None)
    return table


OPCODES = _load()

# The three data blobs that sit inside an instruction stream. Their length is
# in the data itself, so they are the one thing that cannot be a table entry.
PAYLOADS = {0x0100: "packed-switch-payload",
            0x0200: "sparse-switch-payload",
            0x0300: "fill-array-data-payload"}


class Instruction(object):
    __slots__ = ("offset", "opcode", "name", "format", "width", "text")

    def __init__(self, offset, opcode, name, format_name, width, text):
        self.offset = offset
        self.opcode = opcode
        self.name = name
        self.format = format_name
        self.width = width
        self.text = text

    def line(self):
        return "%04x: %s" % (self.offset, self.text)

    def __repr__(self):
        return "<%s %s>" % (self.offset, self.text)


class Disassembler(object):
    """One method's instructions, resolved against the dex they came from."""

    def __init__(self, dex, code):
        self.dex = dex
        self.code = code
        self.units = list(struct.unpack("<%dH" % (len(code.insns) // 2),
                                        code.insns))
        # the arguments live at the end of the frame, and every tool names
        # them p0, p1 … rather than by their real numbers
        self.first_parameter = max(code.registers - code.ins, 0)

    # ------------------------------------------------------------- helpers

    def register(self, number):
        if number >= self.first_parameter and self.code.ins:
            return "p%d" % (number - self.first_parameter)
        return "v%d" % number

    def registers(self, numbers):
        return "{%s}" % ", ".join(self.register(one) for one in numbers)

    def _pool(self, kind, index):
        """A table index as the thing it points at."""
        try:
            if kind == STRING:
                return '"%s"' % _escape(self.dex.string(index))
            if kind == TYPE:
                # the descriptor, not the dotted name: a listing that says
                # `[I` in one operand and `int[]` in the next is two notations
                # for one thing, and real smali uses the first everywhere
                return self.dex.type_descriptor(index)
            if kind == FIELD:
                return self.dex.field(index).reference()
            if kind == METHOD:
                return self.dex.method(index).reference()
            if kind == PROTO:
                shorty, returns, parameters = self.dex.proto(index)
                del shorty
                return "(%s)%s" % ("".join(parameters), returns)
        except Exception:
            pass
        return "%s@%d" % (kind or "index", index)

    # -------------------------------------------------------------- walking

    def instructions(self):
        """Every instruction in order. Stops rather than guessing on damage."""
        out = []
        at = 0
        total = len(self.units)
        while at < total:
            one = self.one(at)
            if one is None:
                break
            out.append(one)
            at += one.width
        return out

    def lines(self, limit=None):
        found = self.instructions()
        out = [one.line() for one in
               (found if limit is None else found[:limit])]
        if limit is not None and len(found) > limit:
            out.append("      … and %d more instructions"
                       % (len(found) - limit))
        return out

    def one(self, at):
        """The instruction at code-unit `at`, or None if it will not decode."""
        units = self.units
        unit = units[at]
        opcode = unit & 0xFF

        if opcode == 0x00 and unit != 0x0000:
            return self._payload(at, unit)

        entry = OPCODES.get(opcode)
        if entry is None:
            return Instruction(at, opcode, "unused", "10x", 1,
                               "unused-%02x" % opcode)
        name, form, kind = entry
        width = WIDTHS[form]
        if at + width > len(units):
            return None
        try:
            text = self._render(at, name, form, kind)
        except Exception as e:
            text = "%s ; could not read: %s" % (name, e)
        return Instruction(at, opcode, name, form, width, text)

    def _payload(self, at, unit):
        """A switch table or an array's contents, sitting in the stream.

        Its length is in the data rather than in a table, which is the one
        place a disassembler cannot be driven by the opcode alone.
        """
        units = self.units
        name = PAYLOADS.get(unit, "payload-%04x" % unit)
        if unit == 0x0100:                       # packed-switch
            width = units[at + 1] * 2 + 4
        elif unit == 0x0200:                     # sparse-switch
            width = units[at + 1] * 4 + 2
        elif unit == 0x0300:                     # fill-array-data
            element = units[at + 1]
            count = units[at + 2] | (units[at + 3] << 16)
            width = (count * element + 1) // 2 + 4
        else:
            return None
        return Instruction(at, 0x00, name, "payload", max(width, 1),
                           "%s ; %d code units" % (name, width))

    # ------------------------------------------------------------ rendering

    def _render(self, at, name, form, kind):
        units = self.units
        unit = units[at]
        high = unit >> 8
        low_nibble = (unit >> 8) & 0xF
        high_nibble = unit >> 12

        if form == "10x":
            return name
        if form == "12x":
            return "%s %s, %s" % (name, self.register(low_nibble),
                                  self.register(high_nibble))
        if form == "11n":
            return "%s %s, #%d" % (name, self.register(low_nibble),
                                   _signed(high_nibble, 4))
        if form == "11x":
            return "%s %s" % (name, self.register(high))
        if form == "10t":
            return "%s %s" % (name, _target(at, _signed(high, 8)))
        if form == "20t":
            return "%s %s" % (name, _target(at, _signed(units[at + 1], 16)))
        if form == "22x":
            return "%s %s, %s" % (name, self.register(high),
                                  self.register(units[at + 1]))
        if form == "21t":
            return "%s %s, %s" % (name, self.register(high),
                                  _target(at, _signed(units[at + 1], 16)))
        if form == "21s":
            return "%s %s, #%d" % (name, self.register(high),
                                   _signed(units[at + 1], 16))
        if form == "21h":
            value = units[at + 1]
            # the literal is the operand shifted up: into the top of a 32-bit
            # value for const/high16, the top of a 64-bit one for the wide form
            shifted = (value << 48) if name.startswith("const-wide") \
                else (value << 16)
            return "%s %s, #0x%x" % (name, self.register(high), shifted)
        if form == "21c":
            return "%s %s, %s" % (name, self.register(high),
                                  self._pool(kind, units[at + 1]))
        if form == "23x":
            second = units[at + 1]
            return "%s %s, %s, %s" % (name, self.register(high),
                                      self.register(second & 0xFF),
                                      self.register(second >> 8))
        if form == "22b":
            second = units[at + 1]
            return "%s %s, %s, #%d" % (name, self.register(high),
                                       self.register(second & 0xFF),
                                       _signed(second >> 8, 8))
        if form == "22t":
            return "%s %s, %s, %s" % (name, self.register(low_nibble),
                                      self.register(high_nibble),
                                      _target(at, _signed(units[at + 1], 16)))
        if form == "22s":
            return "%s %s, %s, #%d" % (name, self.register(low_nibble),
                                       self.register(high_nibble),
                                       _signed(units[at + 1], 16))
        if form == "22c":
            return "%s %s, %s, %s" % (name, self.register(low_nibble),
                                      self.register(high_nibble),
                                      self._pool(kind, units[at + 1]))
        if form == "30t":
            return "%s %s" % (name, _target(at, _signed(
                units[at + 1] | (units[at + 2] << 16), 32)))
        if form == "32x":
            return "%s %s, %s" % (name, self.register(units[at + 1]),
                                  self.register(units[at + 2]))
        if form == "31i":
            return "%s %s, #%d" % (name, self.register(high), _signed(
                units[at + 1] | (units[at + 2] << 16), 32))
        if form == "31t":
            return "%s %s, %s" % (name, self.register(high), _target(
                at, _signed(units[at + 1] | (units[at + 2] << 16), 32)))
        if form == "31c":
            return "%s %s, %s" % (name, self.register(high), self._pool(
                kind, units[at + 1] | (units[at + 2] << 16)))
        if form in ("35c", "45cc"):
            return self._invoke(at, name, kind, extra=form == "45cc")
        if form in ("3rc", "4rcc"):
            return self._invoke_range(at, name, kind, extra=form == "4rcc")
        if form == "51l":
            value = 0
            for step in range(4):
                value |= units[at + 1 + step] << (16 * step)
            return "%s %s, #%d" % (name, self.register(high),
                                   _signed(value, 64))
        return name

    def _invoke(self, at, name, kind, extra=False):
        """`{v0, v1}, kind@BBBB` — up to five registers in packed nibbles."""
        units = self.units
        unit = units[at]
        count = unit >> 12
        fifth = (unit >> 8) & 0xF
        packed = units[at + 2]
        chosen = [(packed >> (4 * n)) & 0xF for n in range(4)] + [fifth]
        text = "%s %s, %s" % (name, self.registers(chosen[:count]),
                              self._pool(kind, units[at + 1]))
        if extra:
            text += ", %s" % self._pool(PROTO, units[at + 3])
        return text

    def _invoke_range(self, at, name, kind, extra=False):
        units = self.units
        count = units[at] >> 8
        first = units[at + 2]
        span = ("{}" if not count else
                "{%s}" % (self.register(first) if count == 1 else
                          "%s .. %s" % (self.register(first),
                                        self.register(first + count - 1))))
        text = "%s %s, %s" % (name, span, self._pool(kind, units[at + 1]))
        if extra:
            text += ", %s" % self._pool(PROTO, units[at + 3])
        return text


def _signed(value, bits):
    limit = 1 << (bits - 1)
    return value - (1 << bits) if value & limit else value


def _target(at, delta):
    """A branch's destination as an offset, the way every tool prints it."""
    return "%04x" % (at + delta)


def _escape(text):
    out = []
    for char in str(text):
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20:
            out.append("\\u%04x" % ord(char))
        else:
            out.append(char)
    return "".join(out)


# ------------------------------------------------------------ whole classes


def method_lines(dex, method, limit=None):
    """One method: its signature, then what it does."""
    head = ".method %s%s" % (
        (dex_module.flags_text(method.access) + " ")
        if method.access else "", method.descriptor_signature())
    # the dotted form as a comment: the descriptor is what smali uses and what
    # a hook has to be written against, and the readable one is what tells you
    # at a glance which method you are looking at
    out = [head, "    # %s: %s" % (method.signature(),
                                   dex_module.type_name(method.return_type))]
    code = dex.code(method.code_off)
    if code is None:
        out.append("    ; no body — abstract or native")
        out.append(".end method")
        return out
    out.append("    .registers %d  ; %d of them are arguments"
               % (code.registers, code.ins))
    for line in Disassembler(dex, code).lines(limit=limit):
        out.append("    " + line)
    out.append(".end method")
    return out


def class_lines(dex, name, limit=None):
    """A whole class, in the shape a smali file has."""
    definition = dex.class_def(name)
    fields, methods = dex.members(name)
    out = [".class %s %s" % (dex_module.flags_text(definition["access"]),
                             dex_module.descriptor_of(definition["name"]))]
    if definition["superclass"]:
        out.append(".super %s"
                   % dex_module.descriptor_of(definition["superclass"]))
    for one in definition["interfaces"]:
        out.append(".implements %s" % dex_module.descriptor_of(one))
    if definition["source"]:
        out.append(".source \"%s\"" % definition["source"])
    if fields:
        out.append("")
        out.append("# %d field%s" % (len(fields),
                                     "" if len(fields) == 1 else "s"))
        for one in fields:
            out.append(".field %s%s:%s" % (
                (dex_module.flags_text(one.access) + " ")
                if one.access else "", one.name, one.type))
    for one in methods:
        out.append("")
        out.extend(method_lines(dex, one, limit=limit))
    return out
