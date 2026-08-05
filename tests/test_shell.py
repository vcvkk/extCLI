# SPDX-License-Identifier: Apache-2.0

"""The shell: lexing, parsing, expansion and execution.

None of this touches Android, which is the point — a shell is exactly the kind
of code that is miserable to debug on a phone, so it is written to be provable
on a desktop and shipped only once it behaves.
"""

import os

import pytest

from extcli_src.backends.chain import ChainBackend
from extcli_src.backends.inproc import InprocBackend
from extcli_src.render import plain
from extcli_src.shell import dispatch
from extcli_src.shell.builtins import build_registry
from extcli_src.shell.context import Context
from extcli_src.shell.env import Env
from extcli_src.shell.lexer import ShellSyntaxError, tokenize
from extcli_src.shell.parser import parse


@pytest.fixture
def shell(tmp_path):
    """A console-like session rooted in a temporary directory."""
    home = str(tmp_path)
    ctx = Context(
        registry=build_registry(),
        env=Env(cwd=home, home=home),
        width=60,
        backend=ChainBackend([InprocBackend()]),
    )

    def run(line):
        return dispatch.run_line(line, ctx)

    run.ctx = ctx
    run.env = ctx.env
    run.home = home
    run.out = lambda line: plain.text(run(line)).strip()
    return run


# ------------------------------------------------------------------- lexing

def test_words_and_operators():
    kinds = [(t.kind, t.text) for t in tokenize("ls -l | grep x")][:-1]
    assert kinds == [("word", "ls"), ("word", "-l"), ("operator", "|"),
                     ("word", "grep"), ("word", "x")]


def test_quotes_keep_spaces_together():
    tokens = [t for t in tokenize('echo "one two" \'three four\'') if t.kind == "word"]
    assert [t.text for t in tokens] == ["echo", "one two", "three four"]


def test_literal_runs_are_merged():
    # per-character parts would break $VAR and name=value detection
    word = [t for t in tokenize("$HOME") if t.kind == "word"][0]
    assert word.value.parts == [("literal", "$HOME")]


def test_escaped_characters_are_literal():
    word = [t for t in tokenize(r"a\ b") if t.kind == "word"][0]
    assert word.value.text == "a b"


def test_redirection_file_descriptor():
    tokens = [t for t in tokenize("cmd 2> err.txt") if t.kind == "operator"]
    assert tokens[0].value == ">"
    assert tokens[0].fd == 2


def test_unterminated_quote_is_reported():
    with pytest.raises(ShellSyntaxError):
        tokenize("echo 'oops")


def test_comments_are_dropped():
    assert [t.text for t in tokenize("echo hi # trailing")][:-1] == ["echo", "hi"]


# ------------------------------------------------------------------ parsing

def test_pipeline_structure():
    script = parse("a | b | c")
    pipeline = script.items[0][0]
    assert pipeline.kind == "pipeline"
    assert len(pipeline.commands) == 3


def test_and_or_is_left_associative():
    node = parse("a && b || c").items[0][0]
    assert node.kind == "andor" and node.operator == "||"
    assert node.left.kind == "andor" and node.left.operator == "&&"


def test_assignments_are_separated_from_words():
    command = parse("A=1 B=2 echo hi").items[0][0]
    assert [name for name, _ in command.assignments] == ["A", "B"]
    assert [w.text for w in command.words] == ["echo", "hi"]


def test_assignment_only_command():
    command = parse("A=1").items[0][0]
    assert command.assignments and not command.words


def test_if_with_elif_and_else():
    node = parse("if a; then b; elif c; then d; else e; fi").items[0][0]
    assert node.kind == "if"
    assert len(node.clauses) == 2
    assert node.otherwise is not None


def test_for_and_while():
    assert parse("for i in 1 2; do echo $i; done").items[0][0].kind == "for"
    node = parse("until false; do echo x; done").items[0][0]
    assert node.kind == "while" and node.until


def test_function_definition():
    node = parse("f() { echo hi; }").items[0][0]
    assert node.kind == "function" and node.name == "f"


def test_missing_fi_is_a_syntax_error():
    with pytest.raises(ShellSyntaxError):
        parse("if true; then echo hi")


# ---------------------------------------------------------------- execution

def test_echo(shell):
    assert shell.out("echo hello world") == "hello world"


def test_exit_status_of_a_failing_builtin(shell):
    assert shell("false").code == 1
    assert shell("true").code == 0


def test_status_variable(shell):
    shell("false")
    assert shell.out("echo $?") == "1"


def test_and_or_short_circuit(shell):
    assert shell.out("true && echo yes") == "yes"
    assert shell.out("false && echo yes") == ""
    assert shell.out("false || echo fallback") == "fallback"


def test_negation(shell):
    assert shell("! false").code == 0
    assert shell("! true").code == 1


def test_sequence_runs_every_command(shell):
    assert shell.out("echo one; echo two") == "one\ntwo"


# --------------------------------------------------------------- expansion

def test_variables(shell):
    shell("name=world")
    assert shell.out("echo hello $name") == "hello world"
    assert shell.out('echo "hello $name"') == "hello world"
    assert shell.out("echo 'hello $name'") == "hello $name"


def test_braced_forms(shell):
    shell("x=set")
    assert shell.out("echo ${x}") == "set"
    assert shell.out("echo ${missing:-default}") == "default"
    assert shell.out("echo ${x:+replaced}") == "replaced"
    assert shell.out("echo ${#x}") == "3"


def test_assignment_default_sticks(shell):
    shell("echo ${fresh:=made}")
    assert shell.out("echo $fresh") == "made"


def test_temporary_assignment_does_not_leak(shell):
    shell("TEMP=outer")
    assert shell.out("TEMP=inner env") .count("TEMP=inner") == 1
    assert shell.out("echo $TEMP") == "outer"


def test_arithmetic(shell):
    assert shell.out("echo $((2 + 3 * 4))") == "14"
    shell("n=10")
    assert shell.out("echo $((n / 2))") == "5"


def test_arithmetic_rejects_anything_but_numbers(shell):
    # the evaluator must never see names it did not substitute
    assert shell.out('echo $((__import__("os")))') == "0"


def test_command_substitution(shell):
    assert shell.out("echo [$(echo inner)]") == "[inner]"
    assert shell.out("echo [`echo old-style`]") == "[old-style]"


def test_field_splitting_of_unquoted_expansions(shell):
    # quoted on assignment, because `items=a b c` would run the command `b`
    shell('items="a b c"')
    assert shell.out("echo $items") == "a b c"
    assert shell.out("echo $items | wc -w").strip() == "3"


def test_quoted_expansion_is_one_field(shell):
    shell('spaced="one  two"')
    assert shell.out('echo "$spaced" | wc -l').strip() == "1"


def test_tilde_and_home(shell):
    assert shell.out("echo ~").rstrip("/") == shell.home.rstrip("/")


def test_glob_expansion(shell):
    shell("touch alpha.txt beta.txt gamma.log")
    assert shell.out("echo *.txt") == "alpha.txt beta.txt"


def test_glob_with_no_match_stays_literal(shell):
    assert shell.out("echo *.nothing") == "*.nothing"


# ----------------------------------------------------------------- pipelines

def test_pipe_between_external_commands(shell):
    shell("seq 5 > numbers.txt")
    assert shell.out("cat numbers.txt | grep 3") == "3"


def test_pipe_from_a_builtin_to_a_command(shell):
    shell("a=1")
    assert "a=1" in shell.out("set | grep a=1")


def test_pipeline_status_is_the_last_command(shell):
    assert shell("echo x | grep nothing").code == 1


# -------------------------------------------------------------- redirection

def test_write_and_append(shell):
    shell("echo first > file.txt")
    shell("echo second >> file.txt")
    assert shell.out("cat file.txt") == "first\nsecond"


def test_read_from_a_file(shell):
    shell("echo content > input.txt")
    assert shell.out("wc -l < input.txt").strip() == "1"


def test_redirect_into_a_missing_directory_reports(shell):
    result = shell("echo x > nowhere/file.txt")
    assert result.code != 0
    assert "redirection failed" in plain.text(result)


def test_heredoc(shell):
    assert shell.out("cat << END\nline one\nEND") == "line one"


# ------------------------------------------------------------ control flow

def test_for_loop(shell):
    assert shell.out("for i in a b; do echo item $i; done") == "item a\nitem b"


def test_for_over_a_glob(shell):
    shell("touch x.md y.md")
    assert shell.out("for f in *.md; do echo $f; done") == "x.md\ny.md"


def test_while_loop(shell):
    script = "c=0; while test $c -lt 3; do c=$((c+1)); echo $c; done"
    assert shell.out(script) == "1\n2\n3"


def test_runaway_loop_is_stopped(shell):
    result = shell("while true; do echo x > /dev/null; done")
    assert "stopping" in plain.text(result)


def test_if_branches(shell):
    assert shell.out("if true; then echo yes; else echo no; fi") == "yes"
    assert shell.out("if false; then echo yes; else echo no; fi") == "no"


def test_case_branches(shell):
    script = "case %s in a*) echo starts-with-a;; *) echo other;; esac"
    assert shell.out(script % "abc") == "starts-with-a"
    assert shell.out(script % "zzz") == "other"


def test_functions(shell):
    shell('greet() { echo "hi $1"; }')
    assert shell.out("greet world") == "hi world"


def test_recursion_is_bounded(shell):
    shell("loop() { loop; }")
    result = shell("loop")
    assert "recursion" in plain.text(result)


def test_subshell_does_not_leak_variables(shell):
    shell("outer=1")
    shell("( outer=2 )")
    assert shell.out("echo $outer") == "1"


def test_group_does_leak(shell):
    shell("{ grouped=yes; }")
    assert shell.out("echo $grouped") == "yes"


# ----------------------------------------------------------- shell builtins

def test_cd_and_pwd(shell):
    shell("mkdir sub")
    shell("cd sub")
    assert shell.out("pwd").endswith("/sub")
    shell("cd ..")
    assert shell.out("pwd") == os.path.normpath(shell.home)


def test_cd_to_a_missing_directory(shell):
    result = shell("cd /definitely/not/here")
    assert result.code != 0
    assert "no such directory" in plain.text(result)


def test_cd_dash_returns(shell):
    shell("mkdir place; cd place")
    shell("cd -")
    assert shell.out("pwd") == os.path.normpath(shell.home)


def test_export_reaches_external_commands(shell):
    shell("export GREETING=hi")
    assert "GREETING=hi" in shell.out("env")


def test_unset(shell):
    shell("gone=1")
    shell("unset gone")
    assert shell.out("echo [$gone]") == "[]"


def test_alias(shell):
    shell("alias hi='echo hello'")
    assert shell.out("hi") == "hello"
    shell("unalias hi")
    assert shell("hi").code == 127


def test_test_command(shell):
    assert shell("test 1 -lt 2").code == 0
    assert shell("test 2 -lt 1").code == 1
    assert shell("test -d %s" % shell.home).code == 0
    assert shell("[ -n nonempty ]").code == 0
    assert shell("[ -z nonempty ]").code == 1


def test_which_distinguishes_builtins_from_programs(shell):
    assert "builtin" in shell.out("which echo")
    shell("alias aliased='echo x'")
    assert "alias" in shell.out("which aliased")


def test_source_runs_in_the_same_shell(shell):
    shell("echo 'sourced=yes' > script.sh")
    shell.ctx.run_script_text = lambda text: dispatch.run_line(text, shell.ctx)
    shell("source script.sh")
    assert shell.out("echo $sourced") == "yes"


def test_exit_marks_the_context(shell):
    shell("exit")
    assert shell.ctx.exit_requested


# ------------------------------------------------------- in-process backend

def test_inproc_ls_cat_and_grep(shell):
    shell("echo alpha > a.txt")
    assert "a.txt" in shell.out("ls")
    assert shell.out("cat a.txt") == "alpha"
    assert shell.out("grep alpha a.txt") == "alpha"


def test_inproc_head_tail_wc(shell):
    shell("seq 10 > n.txt")
    assert shell.out("head -n 2 n.txt") == "1\n2"
    assert shell.out("tail -n 2 n.txt") == "9\n10"
    assert shell.out("wc -l n.txt").strip() == "10"


def test_inproc_file_operations(shell):
    shell("mkdir -p deep/nested")
    shell("echo data > deep/nested/file.txt")
    shell("cp deep/nested/file.txt copy.txt")
    assert shell.out("cat copy.txt") == "data"
    shell("mv copy.txt moved.txt")
    assert "moved.txt" in shell.out("ls")
    shell("rm -r deep")
    assert "deep" not in shell.out("ls")


def test_inproc_reports_missing_files(shell):
    result = shell("cat nothing-here.txt")
    assert result.code != 0
    assert "cat" in plain.text(result)


def test_command_not_found_suggests(shell):
    result = shell("ehco hi")
    assert result.code == 127
    assert "echo" in (result.blocks[0].hint or "")


# ---------------------------------------------------- positional parameters

def test_quoted_at_splits_into_one_field_per_parameter(shell):
    """`"$@"` is the one expansion that splits inside quotes.

    Without it a wrapper like `run() { "$@"; }` hands its whole argument list
    over as a single word, and the command is never found — which is what the
    self-test script is built out of.
    """
    assert shell.out('run() { "$@"; }\nrun echo one two') == "one two"


def test_quoted_at_keeps_spaces_inside_each_parameter(shell):
    out = shell.out('show() { for a in "$@"; do echo "[$a]"; done; }\n'
                    'show "one two" three')
    assert out.split("\n") == ["[one two]", "[three]"]


def test_quoted_star_still_joins(shell):
    assert shell.out('show() { echo "[$*]"; }\nshow a b') == "[a b]"


def test_at_without_arguments_disappears(shell):
    assert shell.out('run() { echo "count $#"; }\nrun') == "count 0"


def test_at_outside_a_function_is_empty(shell):
    assert shell.out('echo "[$@]"') == "[]"


# ------------------------------------------------------ output as it happens

def test_output_is_handed_over_line_by_line():
    """A command that takes a while should be watched, not waited for."""
    from extcli_src.render import plain
    from extcli_src.shell.streams import TerminalSink

    seen = []
    sink = TerminalSink(live=seen.append)
    sink.write("one\ntw")
    assert [plain.text(result) for result in seen] == ["one"]
    sink.write("o\nthree\n")
    assert [plain.text(result) for result in seen] == ["one", "two\nthree"]
    # nothing is left for the end, and the unfinished tail is not lost
    sink.write("four")
    assert plain.text(sink.collected()) == ""
    assert [plain.text(result) for result in seen][-1] == "four"


def test_an_unfinished_line_waits_for_the_rest_of_itself():
    """Showing half a line and then showing it again with more on it is worse
    than waiting for the newline."""
    from extcli_src.shell.streams import TerminalSink

    seen = []
    sink = TerminalSink(live=seen.append)
    sink.write("half")
    assert seen == []


def test_blocks_and_text_keep_their_order():
    """Everything goes through the same door, or `host; ls` would print the
    host block after the listing it came before."""
    from extcli_src.render import blocks, plain
    from extcli_src.shell.streams import TerminalSink

    seen = []
    sink = TerminalSink(live=seen.append)
    sink.write_result(blocks.text("first"))
    sink.write("second\n")
    assert [plain.text(result) for result in seen] == ["first", "second"]


def test_without_a_live_hand_everything_still_arrives_at_the_end():
    from extcli_src.render import blocks, plain
    from extcli_src.shell.streams import TerminalSink

    sink = TerminalSink()
    sink.write("one\n")
    sink.write_result(blocks.text("two"))
    sink.write("three")
    assert plain.text(sink.collected()) == "one\ntwo\nthree"


# --------------------------------------------------- a terminal, not a pipe

def test_a_program_is_given_a_terminal_when_the_output_is_the_screen():
    """`fastfetch` came out with no colour at all. A program asks the fd it is
    writing to whether it is a terminal and decides everything from the
    answer — colour, line drawing, progress bars — and down a pipe it is right
    to say no."""
    import shutil

    from extcli_src.backends import system

    if not shutil.which("sh"):
        pytest.skip("no shell to ask")
    seen = []
    result = system.stream(["/bin/sh", "-c", "test -t 1 && echo yes || echo no"],
                           "", None, None, 10, seen.append, 100, 30)
    assert result.status == 0
    assert "".join(seen).strip() == "yes"


def test_the_program_is_told_how_big_the_screen_is():
    """A program formats to the width of the screen, and a terminal is the
    only thing it can ask."""
    import shutil

    from extcli_src.backends import system

    if not shutil.which("stty"):
        pytest.skip("no stty to ask with")
    seen = []
    system.stream(["/bin/sh", "-c", "stty size"], "", None, None, 10,
                  seen.append, 100, 30)
    assert "".join(seen).split() == ["30", "100"]


def test_the_executor_says_which_programs_are_being_fed():
    """The right-hand side of a pipe is fed; a command on its own is typed at.
    Getting this backwards is what left `ls | grep yaml` running for ever."""
    seen = []

    class Recording(object):
        name = "recording"

        def available(self):
            return True

        def which(self, name):
            return "/bin/" + name

        def has(self, name):
            return True

        def run(self, argv, stdin_text="", cwd=None, env=None, timeout=None,
                on_output=None, size=None, feed=False, on_channel=None):
            from extcli_src.backends.system import Result

            seen.append((argv[0], feed, on_channel is not None))
            return Result(0, "out\n", "")

    ctx = Context(registry=build_registry(), env=Env(cwd="/", home="/"),
                  backend=Recording())
    ctx.attach_input = lambda channel: None
    dispatch.run_line("alpha | beta", ctx)
    assert seen == [("alpha", False, True), ("beta", True, False)]


def test_a_fed_program_is_given_an_end_to_its_input():
    """`ls -la ... | grep yaml` never came back. grep was handed the terminal
    as its stdin, ls's output was written into it, and then grep waited for an
    end-of-file that a terminal cannot deliver."""
    import shutil

    from extcli_src.backends import system

    if not shutil.which("grep"):
        pytest.skip("no grep to feed")
    seen = []
    result = system.stream(["/bin/sh", "-c", "grep yaml"], "one\nyaml\n", None,
                           None, 10, seen.append, 100, 30, feed=True)
    assert result.status == 0
    assert "yaml" in "".join(seen)


def test_a_fed_program_still_writes_to_a_terminal():
    """Only its input comes from a pipe. What it writes still goes to the
    screen, and that is what it asks about before using colour."""
    import shutil

    from extcli_src.backends import system

    if not shutil.which("sh"):
        pytest.skip("no shell to ask")
    seen = []
    system.stream(["/bin/sh", "-c", "test -t 1 && echo yes || echo no"], "",
                  None, None, 10, seen.append, 100, 30, feed=True)
    assert "".join(seen).strip() == "yes"


def test_what_is_typed_while_a_program_runs_reaches_it():
    """Pressing send during a command used to print a new prompt. In a terminal
    what is typed then belongs to the program that is running."""
    import shutil

    from extcli_src.backends import system

    if not shutil.which("sh"):
        pytest.skip("no shell to type at")
    seen = []
    channels = []

    def attach(channel):
        channels.append(channel)
        if channel is not None:
            channel("hello\n")

    result = system.stream(["/bin/sh", "-c", "read line; echo got:$line"], "",
                           None, None, 10, seen.append, 100, 30,
                           on_channel=attach)
    assert result.status == 0
    assert "got:hello" in "".join(seen)
    # and it is taken away when the program ends
    assert channels[-1] is None


def test_a_fed_program_is_not_typed_at():
    """Its input is the pipe's, and a second writer would interleave with it."""
    import shutil

    from extcli_src.backends import system

    if not shutil.which("sh"):
        pytest.skip("no shell to ask")
    channels = []
    system.stream(["/bin/sh", "-c", "cat"], "x\n", None, None, 10,
                  lambda text: None, 100, 30, feed=True,
                  on_channel=channels.append)
    assert channels == []


def test_without_a_size_it_is_a_pipe_as_before():
    """Down a pipe the next command wants text, not a terminal."""
    import shutil

    from extcli_src.backends import system

    if not shutil.which("sh"):
        pytest.skip("no shell to ask")
    seen = []
    system.stream(["/bin/sh", "-c", "test -t 1 && echo yes || echo no"],
                  "", None, None, 10, seen.append)
    assert "".join(seen).strip() == "no"


# ------------------------------------------------------------------ colour

def test_the_ordinary_colours_are_understood():
    """`fastfetch` came out in one colour: the parser knew a reset and a 24-bit
    colour and nothing else, and every plain ESC[31m fell through."""
    from extcli_src.render import palette as palette_module
    from extcli_src.term.textview import parse_ansi

    palette = palette_module.Palette({
        "bg": 0xFF000000, "fg": 0xFFCCCCCC, "dim": 0xFF888888,
        "accent": 0xFF44AAFF, "error": 0xFFFF5555, "success": 0xFF55FF55,
        "warn": 0xFFFFFF55, "selection": 0xFF333333, "divider": 0xFF222222,
    })
    runs = parse_ansi("\x1b[31mred\x1b[0mplain", palette.role("fg"), palette)
    assert [run[0] for run in runs] == ["red", "plain"]
    assert runs[0][1] == palette.ansi_color(1)
    assert runs[1][1] == palette.role("fg")
    # bright, and one from the cube
    assert parse_ansi("\x1b[91mx", 0, palette)[0][1] == palette.ansi_color(9)
    assert parse_ansi("\x1b[38;5;196mx", 0, palette)[0][1] == \
        palette.ansi_color(196)


def test_a_background_is_read_as_a_background():
    from extcli_src.render import palette as palette_module
    from extcli_src.term.textview import parse_ansi

    palette = palette_module.Palette({
        "bg": 0xFF000000, "fg": 0xFFCCCCCC, "dim": 0xFF888888,
        "accent": 0xFF44AAFF, "error": 0xFFFF5555, "success": 0xFF55FF55,
        "warn": 0xFFFFFF55, "selection": 0xFF333333, "divider": 0xFF222222,
    })
    runs = parse_ansi("\x1b[48;5;17;32mgreen", palette.role("fg"), palette)
    assert runs[0][1] == palette.ansi_color(2)
    # and the background it stepped over is now drawn rather than skipped
    assert runs[0][2] == palette.ansi_color(17)


def test_a_sequence_that_cannot_be_obeyed_is_not_printed():
    """fastfetch turns line wrapping off before its logo, and `[?7l` came out
    as text: the pattern matched only digits and semicolons."""
    from extcli_src.term.textview import parse_ansi

    runs = parse_ansi("\x1b[?7lhello\x1b[?7h", 0)
    assert "".join(run[0] for run in runs) == "hello"
    assert "".join(run[0] for run in parse_ansi("\x1b]0;a title\x07x", 0)) == "x"


def test_a_moved_cursor_becomes_the_gap_it_would_have_left():
    """A program that puts two things side by side does not print the space
    between them: it prints the left one and moves the cursor to a column.
    That is how fastfetch puts its facts beside its logo, and dropping the
    sequence ran the two straight together."""
    from extcli_src.render.styles import base

    assert base.apply_controls("", "logo\x1b[21Gfacts") == \
        ([], "logo" + " " * 16 + "facts")
    # forward from where it stands, rather than to a column
    assert base.apply_controls("", "a\x1b[5Cb") == ([], "a     b")
    # a new line starts the counting again
    assert base.apply_controls("", "x\ny\x1b[11Gz") == \
        (["x"], "y" + " " * 9 + "z")


def test_a_line_carries_on_from_where_the_last_piece_left_off():
    """Output arrives in pieces, and a column is counted from the start of the
    line rather than from the start of whatever arrived last."""
    from extcli_src.render.styles import base

    finished, line = base.apply_controls("", "abc")
    assert finished == [] and line == "abc"
    assert base.apply_controls(line, "\x1b[6Gx") == ([], "abc  x")


def test_going_back_to_a_column_cuts_the_line_there():
    """Which is what a progress bar is: the same line, written again. The
    first version only moved forward, so an install came out as one line
    fifty percentages long."""
    from extcli_src.render.styles import base

    assert base.apply_controls("", "  1% #\r 50% ####\r100% ######\n") == \
        (["100% ######"], "")
    assert base.apply_controls("", "\x1b[1G  1% #\x1b[1G 50% ###") == \
        ([], " 50% ###")
    # and a backspace is the same move, one character wide
    assert base.apply_controls("", "abc\b\bX") == ([], "aX")


def test_a_tab_lands_on_the_next_stop():
    from extcli_src.render.styles import base

    assert base.apply_controls("", "a\tb\n") == (["a       b"], "")


def test_the_ordinary_colours_are_understood():
    """`fastfetch` came out in one colour: the parser knew a reset and a 24-bit
    colour and nothing else, and every plain ESC[31m fell through."""
    from extcli_src.render import palette as palette_module
    from extcli_src.term.textview import parse_ansi

    palette = palette_module.Palette({
        "bg": 0xFF000000, "fg": 0xFFCCCCCC, "dim": 0xFF888888,
        "accent": 0xFF44AAFF, "error": 0xFFFF5555, "success": 0xFF55FF55,
        "warn": 0xFFFFFF55, "selection": 0xFF333333, "divider": 0xFF222222,
    })
    runs = parse_ansi("\x1b[31mred\x1b[0mplain", palette.role("fg"), palette)
    assert [run[0] for run in runs] == ["red", "plain"]
    assert runs[0][1] == palette.ansi_color(1)
    assert runs[1][1] == palette.role("fg")
    # bright, and one from the cube
    assert parse_ansi("\x1b[91mx", 0, palette)[0][1] == palette.ansi_color(9)
    assert parse_ansi("\x1b[38;5;196mx", 0, palette)[0][1] == \
        palette.ansi_color(196)


def test_a_background_is_read_as_a_background():
    from extcli_src.render import palette as palette_module
    from extcli_src.term.textview import parse_ansi

    palette = palette_module.Palette({
        "bg": 0xFF000000, "fg": 0xFFCCCCCC, "dim": 0xFF888888,
        "accent": 0xFF44AAFF, "error": 0xFFFF5555, "success": 0xFF55FF55,
        "warn": 0xFFFFFF55, "selection": 0xFF333333, "divider": 0xFF222222,
    })
    runs = parse_ansi("\x1b[48;5;17;32mgreen", palette.role("fg"), palette)
    assert runs[0][1] == palette.ansi_color(2)
    # and the background it stepped over is now drawn rather than skipped
    assert runs[0][2] == palette.ansi_color(17)


def test_a_sequence_that_cannot_be_obeyed_is_not_printed():
    """fastfetch turns line wrapping off before its logo, and `[?7l` came out
    as text: the pattern matched only digits and semicolons."""
    from extcli_src.term.textview import parse_ansi

    runs = parse_ansi("\x1b[?7lhello\x1b[?7h", 0)
    assert "".join(run[0] for run in runs) == "hello"
    assert "".join(run[0] for run in parse_ansi("\x1b]0;a title\x07x", 0)) == "x"


def test_a_coloured_line_is_measured_by_what_is_drawn():
    """A coloured line is a dozen invisible characters longer than it looks.
    Counting them broke program output up long before the edge of the screen
    and left the right-hand third of it empty."""
    from extcli_src.render.styles import base

    line = base.colored("CPU:", 0xFF00FF) + " Cortex-A55*4 + Cortex-A78*2 (6)"
    assert base.visible_length(line) == 36
    assert len(line) > 50
    assert base.wrap(line, 40) == [line]


def test_a_line_too_long_is_cut_between_characters_not_inside_a_colour():
    """The halves of a cut escape are worse than useless: one paints everything
    after it and the other prints as text."""
    from extcli_src.render.styles import base

    letters = "abcdefghij" * 3
    word = base.colored(letters, 0x00FF00)
    pieces = base.wrap(word, 20)
    assert len(pieces) == 2
    assert base.visible_length(pieces[0]) == 20
    assert "".join(base.strip_codes(p) for p in pieces) == letters
    # and the colour is still a whole sequence on the side it was cut from
    assert pieces[0].startswith("\x1b[38;2;0;255;0m")


def test_blank_lines_at_the_end_do_not_push_the_prompt_down():
    """A program that finishes by moving its cursor down past a logo ends with
    a handful of them."""
    from extcli_src.render import plain
    from extcli_src.shell.streams import TerminalSink

    seen = []
    sink = TerminalSink(live=seen.append)
    sink.write("done\n\n\n\n")
    assert [plain.text(result) for result in seen] == ["done"]
    # and one that carries on gets them back, because then they are between
    # things rather than after everything
    sink.write("more\n")
    assert plain.text(seen[-1]) == "\n\n\nmore"


def test_a_line_that_only_moves_the_cursor_is_a_blank_line():
    """A program ending its output moves the cursor about, and those lines
    carry characters that draw nothing. Judged by what is in the string they
    looked like lines with something on them, and the prompt was pushed down
    the screen by six of them."""
    from extcli_src.render import plain
    from extcli_src.render.styles import base
    from extcli_src.shell.streams import TerminalSink

    assert base.is_blank("\x1b[K")
    assert base.is_blank("  \x1b[2K \x1b[0m")
    assert not base.is_blank("\x1b[31mx\x1b[0m")

    seen = []
    sink = TerminalSink(live=seen.append)
    sink.write("done\n\x1b[K\n\x1b[K\n")
    assert [plain.text(result) for result in seen] == ["done"]


def test_a_program_s_own_output_is_not_taken_apart_and_put_back():
    """It is terminal text already: its own colours, its own wrapping to a
    width we gave it, and its own carriage returns, which are how a progress
    bar rewrites the line it is on."""
    from extcli_src.shell.streams import TerminalSink

    raw = []
    blocks_seen = []
    sink = TerminalSink(live=blocks_seen.append, live_text=raw.append)
    sink.write("  12% ###\r  40% #####\r 100% #######\n")
    assert raw == ["  12% ###\r  40% #####\r 100% #######\n"]
    assert blocks_seen == []
    # nothing is left waiting for the end, either
    assert not sink.collected().blocks


def test_the_two_shapes_a_real_install_sends():
    """apk redraws with a carriage return, uv with "go to column one" and
    "erase the line". Both mean the same thing and both came out as a wall of
    text: one as fifty lines, the other as one line fifty percentages long."""
    from extcli_src.render.styles import base

    assert base.apply_controls("", "  0%  \r  1% #  \r100% #####\n") == \
        (["100% #####"], "")
    spinner = ("\x1b[1G\x1b[2K- Preparing... (0/2)"
               "\x1b[1G\x1b[2K\\ Preparing... (1/2)")
    assert base.apply_controls("", spinner) == ([], "\\ Preparing... (1/2)")


def test_a_running_program_can_be_interrupted():
    """^C did nothing: the program is started in a session of its own and has
    no controlling terminal, so the tty driver has no foreground group to
    signal. The console sends it, to the group, itself."""
    import shutil
    import threading

    from extcli_src.backends import system

    if not shutil.which("sh"):
        pytest.skip("no shell to interrupt")
    stopped = threading.Event()

    def attach(channel):
        if channel is None:
            return
        # give it a moment to be in the sleep rather than still starting
        threading.Timer(0.3, lambda: (channel.interrupt(),
                                      stopped.set())).start()

    started = __import__("time").time()
    result = system.stream(["/bin/sh", "-c", "sleep 30"], "", None, None, 20,
                           lambda text: None, 100, 30, on_channel=attach)
    assert stopped.is_set()
    assert __import__("time").time() - started < 10
    assert result.status != 0


def test_a_program_that_ignores_the_ask_is_stopped_anyway():
    """The second ^C is not refusable."""
    import shutil
    import threading
    import time

    from extcli_src.backends import system

    if not shutil.which("sh"):
        pytest.skip("no shell to interrupt")

    def attach(channel):
        if channel is None:
            return

        def press():
            channel.interrupt()
            time.sleep(0.3)
            channel.stop()

        threading.Timer(0.3, press).start()

    started = time.time()
    system.stream(["/bin/sh", "-c", "trap '' INT; sleep 30"], "", None, None,
                  20, lambda text: None, 100, 30, on_channel=attach)
    assert time.time() - started < 10
