# SPDX-License-Identifier: Apache-2.0

"""Reading the client's own code out of its `.dex` files.

exteraGram is Java, compiled to Dalvik bytecode and shipped inside the APK.
To patch it you first have to be able to read it: what classes there are, what
methods they have, and what those methods do. Nothing on a phone will tell you
that — there is no `javap`, no decompiler that works, and asking the runtime
by reflection means loading classes, which runs their static initialisers and
is not something to do twenty thousand times to build a list.

The dex format itself will tell you, and cheaply. It is index tables followed
by data: the names of every class, method and field are in fixed-size arrays
near the front, and reading those is a few hundred kilobytes and no code at
all. The bodies are further in and are read one class at a time, only when
something asks.

That laziness is the whole design. A client's dex is tens of megabytes and
there may be several of them; parsing all of it eagerly on a phone would take
minutes and hold it all in memory for a question about one method.

Nothing here is Android-specific — it reads a file. The disassembler that
turns a method's instructions into something readable is `patch.smali`.
"""

import struct

MAGIC = b"dex\n"
ENDIAN = 0x12345678

# Where each field sits in the 112-byte header. Fixed since dex 035 and not
# going to move; naming them beats counting offsets at every use.
_HEADER = (
    ("file_size", 32), ("header_size", 36), ("endian_tag", 40),
    ("link_size", 44), ("link_off", 48), ("map_off", 52),
    ("string_ids_size", 56), ("string_ids_off", 60),
    ("type_ids_size", 64), ("type_ids_off", 68),
    ("proto_ids_size", 72), ("proto_ids_off", 76),
    ("field_ids_size", 80), ("field_ids_off", 84),
    ("method_ids_size", 88), ("method_ids_off", 92),
    ("class_defs_size", 96), ("class_defs_off", 100),
    ("data_size", 104), ("data_off", 108),
)

NO_INDEX = 0xFFFFFFFF

# The one-letter type descriptors, and what a person calls them.
PRIMITIVES = {
    "V": "void", "Z": "boolean", "B": "byte", "S": "short", "C": "char",
    "I": "int", "J": "long", "F": "float", "D": "double",
}

# access_flags, as the ones worth printing in front of a method
ACCESS = (
    (0x0001, "public"), (0x0002, "private"), (0x0004, "protected"),
    (0x0008, "static"), (0x0010, "final"), (0x0020, "synchronized"),
    (0x0040, "volatile"), (0x0080, "transient"), (0x0100, "native"),
    (0x0200, "interface"), (0x0400, "abstract"), (0x0800, "strictfp"),
    (0x1000, "synthetic"), (0x4000, "enum"), (0x10000, "constructor"),
)


class DexError(Exception):
    pass


def flags_text(value):
    return " ".join(name for bit, name in ACCESS if value & bit)


def type_name(descriptor):
    """`Lorg/telegram/ui/X;` as `org.telegram.ui.X`, `[I` as `int[]`."""
    text = str(descriptor)
    arrays = 0
    while text.startswith("["):
        arrays += 1
        text = text[1:]
    if text.startswith("L") and text.endswith(";"):
        name = text[1:-1].replace("/", ".")
    else:
        name = PRIMITIVES.get(text, text)
    return name + "[]" * arrays


def descriptor_of(name):
    """The other way, for looking a class up by the name a person typed."""
    text = str(name)
    if text.startswith("L") and text.endswith(";"):
        return text
    return "L%s;" % text.replace(".", "/")


# ------------------------------------------------------------------ numbers


def uleb128(data, offset):
    """(value, offset after it). The dex format's variable-length integer."""
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 35:
            raise DexError("uleb128 that never ends")


def sleb128(data, offset):
    result = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if byte & 0x40 and shift < 64:
                result -= 1 << shift
            return result, offset
        if shift > 70:
            raise DexError("sleb128 that never ends")


def mutf8(data, offset, length):
    """A dex string: modified UTF-8, which is not UTF-8.

    Two differences, and both of them appear in a real client's strings. A NUL
    is written as two bytes so that nothing in the file is ever zero, and
    anything outside the basic plane — every emoji in the app — is written as
    the two halves of a surrogate pair encoded separately, which ordinary
    UTF-8 decoders reject.
    """
    out = []
    while len(out) < length:
        byte = data[offset]
        offset += 1
        if byte < 0x80:
            out.append(byte)
        elif byte & 0xE0 == 0xC0:
            out.append(((byte & 0x1F) << 6) | (data[offset] & 0x3F))
            offset += 1
        elif byte & 0xF0 == 0xE0:
            out.append(((byte & 0x0F) << 12)
                       | ((data[offset] & 0x3F) << 6)
                       | (data[offset + 1] & 0x3F))
            offset += 2
        else:
            raise DexError("byte %02x is not modified UTF-8" % byte)
    return "".join(_characters(out))


def _characters(points):
    """Surrogate pairs joined back into the character they stand for."""
    index = 0
    while index < len(points):
        point = points[index]
        if 0xD800 <= point <= 0xDBFF and index + 1 < len(points):
            low = points[index + 1]
            if 0xDC00 <= low <= 0xDFFF:
                yield chr(0x10000 + ((point - 0xD800) << 10) + (low - 0xDC00))
                index += 2
                continue
        yield chr(point)
        index += 1


# --------------------------------------------------------------- the file


class Method(object):
    """One method, as the index tables describe it."""

    __slots__ = ("index", "class_name", "name", "shorty", "return_type",
                 "parameters", "access", "code_off")

    def __init__(self, index, class_name, name, shorty, return_type,
                 parameters, access=0, code_off=0):
        self.index = index
        self.class_name = class_name
        self.name = name
        self.shorty = shorty
        self.return_type = return_type
        self.parameters = tuple(parameters)
        self.access = access
        self.code_off = code_off

    def signature(self):
        """For a person reading an index: `onClick(android.view.View)`."""
        return "%s(%s)" % (self.name, ", ".join(
            type_name(one) for one in self.parameters))

    def descriptor_signature(self):
        """For a listing: `onClick(Landroid/view/View;)V`, as smali writes."""
        return "%s(%s)%s" % (self.name, "".join(self.parameters),
                             self.return_type)

    def full(self):
        return "%s.%s: %s" % (self.class_name, self.signature(),
                              type_name(self.return_type))

    def reference(self):
        """How the disassembler names it: `Lcls;->name(args)ret`."""
        return "%s->%s(%s)%s" % (descriptor_of(self.class_name), self.name,
                                 "".join(self.parameters), self.return_type)

    def __repr__(self):
        return "<Method %s>" % self.full()


class Field(object):
    __slots__ = ("index", "class_name", "name", "type", "access")

    def __init__(self, index, class_name, name, type_descriptor, access=0):
        self.index = index
        self.class_name = class_name
        self.name = name
        self.type = type_descriptor
        self.access = access

    def full(self):
        return "%s.%s: %s" % (self.class_name, self.name, type_name(self.type))

    def reference(self):
        return "%s->%s:%s" % (descriptor_of(self.class_name), self.name,
                              self.type)


class Code(object):
    """A method's body: its registers, and the instructions themselves."""

    __slots__ = ("registers", "ins", "outs", "tries", "debug_off", "insns")

    def __init__(self, registers, ins, outs, tries, debug_off, insns):
        self.registers = registers
        self.ins = ins
        self.outs = outs
        self.tries = tries
        self.debug_off = debug_off
        self.insns = insns


class Dex(object):
    """One `.dex`, read lazily.

    The index tables are parsed on first use and kept; the bodies are not kept
    at all. A client's dex is tens of megabytes and the question is almost
    always about one class.
    """

    def __init__(self, data):
        self.data = bytes(data)
        if self.data[:4] != MAGIC:
            raise DexError("not a dex file")
        self.header = {name: self._u4(offset) for name, offset in _HEADER}
        if self.header["endian_tag"] != ENDIAN:
            raise DexError("a big-endian dex, which nothing produces")
        self._strings = {}
        self._types = {}
        self._class_index = None

    # -------------------------------------------------------------- reading

    @classmethod
    def open(cls, path):
        with open(str(path), "rb") as handle:
            return cls(handle.read())

    def _u1(self, offset):
        return self.data[offset]

    def _u2(self, offset):
        return struct.unpack_from("<H", self.data, offset)[0]

    def _u4(self, offset):
        return struct.unpack_from("<I", self.data, offset)[0]

    def version(self):
        """`035`, `038`, `039` — which revision of the format this is."""
        return self.data[4:7].decode("ascii", "replace")

    def counts(self):
        return [(name.replace("_size", ""), self.header[name])
                for name in ("string_ids_size", "type_ids_size",
                             "proto_ids_size", "field_ids_size",
                             "method_ids_size", "class_defs_size")]

    # --------------------------------------------------------------- tables

    def string(self, index):
        found = self._strings.get(index)
        if found is not None:
            return found
        if not 0 <= index < self.header["string_ids_size"]:
            raise DexError("no string %d" % index)
        offset = self._u4(self.header["string_ids_off"] + index * 4)
        length, offset = uleb128(self.data, offset)
        text = mutf8(self.data, offset, length)
        self._strings[index] = text
        return text

    def type_descriptor(self, index):
        found = self._types.get(index)
        if found is not None:
            return found
        if index == NO_INDEX:
            return ""
        if not 0 <= index < self.header["type_ids_size"]:
            raise DexError("no type %d" % index)
        text = self.string(self._u4(self.header["type_ids_off"] + index * 4))
        self._types[index] = text
        return text

    def proto(self, index):
        """(shorty, return descriptor, parameter descriptors)."""
        base = self.header["proto_ids_off"] + index * 12
        shorty = self.string(self._u4(base))
        returns = self.type_descriptor(self._u4(base + 4))
        parameters_off = self._u4(base + 8)
        return shorty, returns, self._type_list(parameters_off)

    def _type_list(self, offset):
        if not offset:
            return ()
        size = self._u4(offset)
        return tuple(self.type_descriptor(self._u2(offset + 4 + n * 2))
                     for n in range(size))

    def method(self, index):
        base = self.header["method_ids_off"] + index * 8
        class_name = type_name(self.type_descriptor(self._u2(base)))
        shorty, returns, parameters = self.proto(self._u2(base + 2))
        name = self.string(self._u4(base + 4))
        return Method(index, class_name, name, shorty, returns, parameters)

    def field(self, index):
        base = self.header["field_ids_off"] + index * 8
        class_name = type_name(self.type_descriptor(self._u2(base)))
        type_descriptor = self.type_descriptor(self._u2(base + 2))
        name = self.string(self._u4(base + 4))
        return Field(index, class_name, name, type_descriptor)

    # -------------------------------------------------------------- classes

    def class_count(self):
        return self.header["class_defs_size"]

    def class_names(self):
        """Every class in this dex, in the order it appears.

        Only the type table is touched, so this is fast even on a dex with
        twenty thousand classes in it — which is what makes an index of a
        whole client something that can be built while somebody waits.
        """
        base = self.header["class_defs_off"]
        return [type_name(self.type_descriptor(self._u4(base + n * 32)))
                for n in range(self.header["class_defs_size"])]

    def _index_classes(self):
        if self._class_index is None:
            self._class_index = {}
            for number, name in enumerate(self.class_names()):
                self._class_index.setdefault(name, number)
        return self._class_index

    def has_class(self, name):
        return type_name(descriptor_of(name)) in self._index_classes()

    def class_def(self, name):
        """The header of one class: what it extends, and where its data is."""
        wanted = type_name(descriptor_of(name))
        number = self._index_classes().get(wanted)
        if number is None:
            raise DexError("no class %s in this dex" % name)
        base = self.header["class_defs_off"] + number * 32
        return {
            "name": wanted,
            "access": self._u4(base + 4),
            "superclass": type_name(self.type_descriptor(self._u4(base + 8)))
            if self._u4(base + 8) != NO_INDEX else "",
            "interfaces": [type_name(one) for one
                           in self._type_list(self._u4(base + 12))],
            "source": self._source(self._u4(base + 16)),
            "data_off": self._u4(base + 24),
        }

    def _source(self, index):
        return self.string(index) if index != NO_INDEX else ""

    def members(self, name):
        """(fields, methods) of one class, with their code offsets.

        The encoded lists are deltas — each entry's index is the previous plus
        this one's — which is why they cannot be read out of order and why
        this returns the whole class at once.
        """
        definition = self.class_def(name)
        offset = definition["data_off"]
        if not offset:
            return [], []
        data = self.data
        counts = []
        for _ in range(4):
            value, offset = uleb128(data, offset)
            counts.append(value)
        static_fields, instance_fields, direct_methods, virtual_methods = counts

        fields = []
        for count in (static_fields, instance_fields):
            index = 0
            for _ in range(count):
                delta, offset = uleb128(data, offset)
                access, offset = uleb128(data, offset)
                index += delta
                one = self.field(index)
                one.access = access
                fields.append(one)

        methods = []
        for count in (direct_methods, virtual_methods):
            index = 0
            for _ in range(count):
                delta, offset = uleb128(data, offset)
                access, offset = uleb128(data, offset)
                code_off, offset = uleb128(data, offset)
                index += delta
                one = self.method(index)
                one.access = access
                one.code_off = code_off
                methods.append(one)
        return fields, methods

    def code(self, offset):
        """A method's body, or None for an abstract or native one."""
        if not offset:
            return None
        registers = self._u2(offset)
        ins = self._u2(offset + 2)
        outs = self._u2(offset + 4)
        tries = self._u2(offset + 6)
        debug_off = self._u4(offset + 8)
        size = self._u4(offset + 12)
        start = offset + 16
        return Code(registers, ins, outs, tries, debug_off,
                    self.data[start:start + size * 2])

    # -------------------------------------------------------------- looking

    def find_classes(self, needle, limit=None):
        """Class names containing `needle`, case-insensitively."""
        wanted = str(needle).lower()
        out = [name for name in self.class_names() if wanted in name.lower()]
        return out if limit is None else out[:limit]

    def find_methods(self, needle, limit=None):
        """Methods whose name contains `needle`, from the index table.

        The whole method table rather than a walk of the classes: a method
        referenced by this dex but defined in another is still worth finding,
        because that is exactly the case where somebody is looking for where
        something lives.
        """
        wanted = str(needle).lower()
        out = []
        for index in range(self.header["method_ids_size"]):
            base = self.header["method_ids_off"] + index * 8
            name = self.string(self._u4(base + 4))
            if wanted in name.lower():
                out.append(self.method(index))
                if limit is not None and len(out) >= limit:
                    break
        return out

    def find_strings(self, needle, limit=None):
        """Every string constant containing `needle`.

        Often the fastest way into a client: a label somebody can see on
        screen leads to the code that puts it there.
        """
        wanted = str(needle).lower()
        out = []
        for index in range(self.header["string_ids_size"]):
            text = self.string(index)
            if wanted in text.lower():
                out.append(text)
                if limit is not None and len(out) >= limit:
                    break
        return out


def open_all(paths):
    """Several dex files as one list, skipping any that will not parse."""
    out = []
    for path in paths:
        try:
            out.append((str(path), Dex.open(path)))
        except Exception:
            continue
    return out
