# SPDX-License-Identifier: Apache-2.0

"""Reading Dalvik bytecode.

There is a real dex in this repository — `extcli/dex/terminal.dex`, the
renderer's own, built from Kotlin we wrote — so none of this has to be tested
against a fixture somebody made up. Better still, we know what is in it, which
means a listing can be checked against what the source actually said rather
than only against itself.

The test that matters most is `test_every_method_decodes_to_exactly_its_own
_length`. A disassembler with one wrong instruction width does not fail; it
desynchronises and produces plausible nonsense from that point on. Walking
every method in a real dex and insisting the stream ends exactly where the
method says it does is the only thing that catches it.
"""

from pathlib import Path

import pytest

from extcli_src.patch import dex as dexmod
from extcli_src.patch import smali

DEX = Path(__file__).resolve().parent.parent / "extcli" / "dex" / "terminal.dex"

NATIVE = "dev.vcvkk.extcli.terminal.TerminalNative"
VIEW = "dev.vcvkk.extcli.terminal.TerminalView"


@pytest.fixture(scope="module")
def dex():
    return dexmod.Dex.open(DEX)


# ------------------------------------------------------------------ numbers

def test_uleb128_reads_what_it_should():
    assert dexmod.uleb128(b"\x00", 0) == (0, 1)
    assert dexmod.uleb128(b"\x7f", 0) == (127, 1)
    assert dexmod.uleb128(b"\x80\x01", 0) == (128, 2)
    assert dexmod.uleb128(b"\xff\xff\x03", 0) == (65535, 3)


def test_sleb128_carries_the_sign():
    assert dexmod.sleb128(b"\x00", 0) == (0, 1)
    assert dexmod.sleb128(b"\x7f", 0) == (-1, 1)
    assert dexmod.sleb128(b"\x3f", 0) == (63, 1)
    assert dexmod.sleb128(b"\x40", 0) == (-64, 1)


def test_a_dex_string_is_not_utf8():
    """Two differences, and both appear in a real client's strings."""
    # a NUL is written as two bytes, so nothing in the file is ever zero
    assert dexmod.mutf8(b"\xc0\x80", 0, 1) == "\x00"
    # and anything outside the basic plane is the two halves of a surrogate
    # pair encoded separately, which an ordinary decoder rejects
    assert dexmod.mutf8(b"\xed\xa0\xbd\xed\xb8\x80", 0, 2) == "\U0001f600"
    assert dexmod.mutf8(b"hello", 0, 5) == "hello"


def test_descriptors_and_names_are_the_same_thing_written_twice():
    assert dexmod.type_name("Lorg/telegram/ui/LaunchActivity;") == \
        "org.telegram.ui.LaunchActivity"
    assert dexmod.type_name("[I") == "int[]"
    assert dexmod.type_name("[[Ljava/lang/String;") == "java.lang.String[][]"
    assert dexmod.type_name("V") == "void"
    assert dexmod.descriptor_of("org.telegram.ui.LaunchActivity") == \
        "Lorg/telegram/ui/LaunchActivity;"
    assert dexmod.descriptor_of("Lalready/There;") == "Lalready/There;"


# -------------------------------------------------------------- the tables

def test_the_header_reads(dex):
    assert dex.version() == "037"
    counts = dict(dex.counts())
    assert counts["class_defs"] == 54
    assert counts["string_ids"] == 359
    assert counts["method_ids"] == 204


def test_a_dex_is_recognised_by_its_magic(tmp_path):
    bad = tmp_path / "no.dex"
    bad.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
    with pytest.raises(dexmod.DexError):
        dexmod.Dex.open(bad)


def test_the_classes_are_the_ones_we_compiled(dex):
    names = dex.class_names()
    assert len(names) == 54
    assert NATIVE in names and VIEW in names
    # Kotlin's own runtime came along, which is what makes this a fair test
    assert any(name.startswith("kotlin.") for name in names)


def test_a_class_says_what_it_extends(dex):
    definition = dex.class_def(NATIVE)
    assert definition["superclass"] == "java.lang.Object"
    assert "public" in dexmod.flags_text(definition["access"])


def test_a_class_says_what_it_implements(dex):
    definition = dex.class_def("dev.vcvkk.extcli.terminal.TerminalNative$0")
    assert definition["interfaces"] == ["java.lang.Runnable"]


def test_a_class_can_be_asked_for_by_either_spelling(dex):
    assert dex.has_class(NATIVE)
    assert dex.has_class("Ldev/vcvkk/extcli/terminal/TerminalNative;")
    assert not dex.has_class("org.telegram.NotHere")
    with pytest.raises(dexmod.DexError):
        dex.class_def("org.telegram.NotHere")


def test_the_members_are_the_ones_the_kotlin_declared(dex):
    fields, methods = dex.members(NATIVE)
    names = {one.name for one in fields}
    # the object, the session map and the palette constants
    assert {"INSTANCE", "sessions", "DEFAULT_ANSI", "ROLE_FG"} <= names
    signatures = {one.signature() for one in methods}
    assert "append(android.view.View, java.lang.String)" in signatures


def test_a_method_knows_its_frame(dex):
    _fields, methods = dex.members(NATIVE)
    append = [one for one in methods if one.name == "append"][0]
    code = dex.code(append.code_off)
    # a View and a String went in, and Kotlin gave it one more to work with
    assert code.ins == 2
    assert code.registers == 3
    assert code.insns


def test_a_method_written_for_a_listing_and_a_method_written_for_a_person(dex):
    _fields, methods = dex.members(NATIVE)
    append = [one for one in methods if one.name == "append"][0]
    assert append.descriptor_signature() == \
        "append(Landroid/view/View;Ljava/lang/String;)V"
    assert append.signature() == "append(android.view.View, java.lang.String)"
    assert append.reference() == (
        "Ldev/vcvkk/extcli/terminal/TerminalNative;->"
        "append(Landroid/view/View;Ljava/lang/String;)V")


# -------------------------------------------------------------- looking up

def test_classes_can_be_searched_for(dex):
    assert VIEW in dex.find_classes("terminalview")
    assert dex.find_classes("nothing at all") == []


def test_methods_can_be_searched_for(dex):
    found = dex.find_methods("append")
    assert any(one.class_name == NATIVE for one in found)


def test_strings_can_be_searched_for(dex):
    """Often the fastest way in: a label on screen leads to the code."""
    assert dex.find_strings("TerminalNative")
    assert dex.find_strings("this is not in there") == []


def test_a_search_can_be_cut_short(dex):
    assert len(dex.find_strings("a", limit=3)) == 3


# ------------------------------------------------------------ disassembling

def test_every_method_decodes_to_exactly_its_own_length(dex):
    """The test that matters.

    A wrong instruction width does not fail — it desynchronises, and every
    line after it is plausible nonsense. Requiring each stream to end exactly
    where the method says it does is what catches that.
    """
    methods = bodies = instructions = 0
    for name in dex.class_names():
        _fields, found = dex.members(name)
        for one in found:
            methods += 1
            code = dex.code(one.code_off)
            if code is None:
                continue
            bodies += 1
            worker = smali.Disassembler(dex, code)
            decoded = worker.instructions()
            instructions += len(decoded)
            assert sum(item.width for item in decoded) == len(worker.units), \
                "%s.%s desynchronised" % (name, one.signature())
            for item in decoded:
                assert item.name != "unused", \
                    "%s.%s: opcode %02x" % (name, one.signature(), item.opcode)
                assert "could not read" not in item.text, item.text
    assert methods == 110 and bodies == 110
    assert instructions > 1500


def test_the_whole_opcode_table_is_shaped_the_way_it_should_be():
    for opcode, (name, form, kind) in smali.OPCODES.items():
        assert 0 <= opcode <= 0xFF
        assert form in smali.WIDTHS, (name, form)
        assert kind in (None, smali.STRING, smali.TYPE, smali.FIELD,
                        smali.METHOD, smali.PROTO, smali.CALLSITE,
                        smali.HANDLE), (name, kind)
    # every invoke resolves a method, or the listing would say method@123
    for opcode in (0x6E, 0x6F, 0x70, 0x71, 0x72, 0x74, 0x77):
        assert smali.OPCODES[opcode][2] == smali.METHOD
    # and const-string a string
    assert smali.OPCODES[0x1A][2] == smali.STRING


def test_a_listing_says_what_the_kotlin_said(dex):
    """`append` looks the session up, casts it, checks it and calls through —
    which is exactly what the source it was compiled from does."""
    _fields, methods = dex.members(NATIVE)
    append = [one for one in methods if one.name == "append"][0]
    text = "\n".join(smali.method_lines(dex, append))

    assert ".method public static final " \
           "append(Landroid/view/View;Ljava/lang/String;)V" in text
    assert "Ldev/vcvkk/extcli/terminal/TerminalNative;->" \
           "sessions:Ljava/util/HashMap;" in text
    assert "Ljava/util/HashMap;->get(Ljava/lang/Object;)Ljava/lang/Object;" \
        in text
    assert "check-cast p0, Ldev/vcvkk/extcli/terminal/TerminalView;" in text
    assert "return-void" in text


def test_arguments_are_named_the_way_every_tool_names_them(dex):
    """They live at the end of the frame and are called p0, p1 … everywhere.
    Getting it wrong makes a listing that reads fine and points at the wrong
    values."""
    _fields, methods = dex.members(NATIVE)
    append = [one for one in methods if one.name == "append"][0]
    worker = smali.Disassembler(dex, dex.code(append.code_off))
    # three registers, two of them arguments: v0, then p0 and p1
    assert worker.register(0) == "v0"
    assert worker.register(1) == "p0"
    assert worker.register(2) == "p1"


def test_a_static_method_with_no_arguments_has_no_p_registers(dex):
    _fields, methods = dex.members(NATIVE)
    setup = [one for one in methods if one.name == "<clinit>"][0]
    worker = smali.Disassembler(dex, dex.code(setup.code_off))
    assert worker.register(0) == "v0"


def test_a_class_listing_is_shaped_like_smali(dex):
    lines = smali.class_lines(dex, NATIVE)
    assert lines[0] == \
        ".class public final Ldev/vcvkk/extcli/terminal/TerminalNative;"
    assert lines[1] == ".super Ljava/lang/Object;"
    assert any(line == ".field public static final ROLE_FG:I"
               for line in lines)
    assert lines.count(".end method") == \
        len(dex.members(NATIVE)[1])


def test_one_notation_throughout_a_listing(dex):
    """A listing that says `[I` in one operand and `int[]` in the next is two
    notations for one thing."""
    lines = smali.class_lines(dex, NATIVE)
    body = [line for line in lines if line.strip().startswith("0")]
    for line in body:
        assert "int[]" not in line, line
        assert "java.lang." not in line, line


def test_a_listing_can_be_cut_short(dex):
    _fields, methods = dex.members(VIEW)
    longest = max(methods, key=lambda one: (dex.code(one.code_off).insns
                                            if one.code_off else b""))
    lines = smali.method_lines(dex, longest, limit=5)
    assert any("and" in line and "more instructions" in line for line in lines)


def test_a_method_with_no_body_says_so(dex):
    """Abstract and native methods have no code, and a listing that showed
    nothing would look like a method that does nothing."""
    for name in dex.class_names():
        _fields, methods = dex.members(name)
        for one in methods:
            if one.code_off:
                continue
            assert "no body" in "\n".join(smali.method_lines(dex, one))
            return


def test_branch_targets_are_offsets_within_the_method(dex):
    _fields, methods = dex.members(NATIVE)
    append = [one for one in methods if one.name == "append"][0]
    text = "\n".join(smali.method_lines(dex, append))
    # `if-nez p0, 0011` jumps past the early return, which is 0010
    assert "if-nez p0, 0011" in text


def test_the_payloads_carry_their_own_length(dex):
    """A switch table's size is in the data, not in the opcode table — the one
    place a disassembler cannot be driven by the opcode alone."""
    _fields, methods = dex.members(NATIVE)
    setup = [one for one in methods if one.name == "<clinit>"][0]
    decoded = smali.Disassembler(dex, dex.code(setup.code_off)).instructions()
    payloads = [one for one in decoded if one.format == "payload"]
    assert payloads and payloads[0].name == "fill-array-data-payload"
    # sixteen ints: 32 bytes of data plus the four-unit header
    assert payloads[0].width == 36
