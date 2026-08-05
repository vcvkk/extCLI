# SPDX-License-Identifier: Apache-2.0

"""Walks the syntax tree and runs it.

Pipelines are run sequentially through in-memory buffers rather than with real
concurrent processes. For builtins that is equivalent, and for external commands
it costs nothing in a console where output is read after the fact — while
avoiding threads, deadlocks on full pipes, and partial output arriving on the UI
thread out of order.

Two kinds of command coexist: builtins, which return rich blocks, and external
programs, which return text. Both are written to the same sink; only the sink
knows whether structure survives.
"""

import os

from ..render import blocks
from . import expand, nodes
from .lexer import ShellSyntaxError
from .registry import CommandError
from .streams import (FileSink, MemorySink, NullSink, Source, TerminalSink)

MAX_LOOP_ITERATIONS = 10000
MAX_RECURSION = 32


class Outcome(object):
    """What running a script produced: an exit status and what to display."""

    def __init__(self, status=0, result=None):
        self.status = int(status)
        self.result = result if result is not None else blocks.Result()

    @property
    def ok(self):
        return self.status == 0


class Executor(object):
    def __init__(self, ctx, registry, backend=None):
        self.ctx = ctx
        self.registry = registry
        self.backend = backend
        self.env = ctx.env
        self._depth = 0

    # -------------------------------------------------------------- entry point

    def run(self, script):
        """Runs a parsed script and returns an Outcome."""
        terminal = TerminalSink(live=getattr(self.ctx, "live", None),
                                live_text=getattr(self.ctx, "live_text", None))
        # the console's own stdin: whoever is typing, with no end to it
        status = self.run_script(script, terminal, Source(is_terminal=True))
        return Outcome(status, terminal.collected())

    def run_text(self, source):
        from .parser import parse

        try:
            script = parse(source)
        except ShellSyntaxError as e:
            return Outcome(2, blocks.error("syntax error: %s" % e.message))
        return self.run(script)

    # ------------------------------------------------------------------ nodes

    def run_script(self, script, stdout, stdin):
        status = self.env.status
        for node, _separator in script.items:
            status = self.run_node(node, stdout, stdin)
            self.env.status = status
            if self.env.exit_requested:
                break
        return status

    def run_node(self, node, stdout, stdin):
        kind = node.kind
        if kind == "simple":
            return self.run_simple(node, stdout, stdin)
        if kind == "pipeline":
            return self.run_pipeline(node, stdout, stdin)
        if kind == "andor":
            return self.run_andor(node, stdout, stdin)
        if kind == "script":
            return self.run_script(node, stdout, stdin)
        if kind in ("group", "subshell"):
            return self.run_group(node, stdout, stdin)
        if kind == "if":
            return self.run_if(node, stdout, stdin)
        if kind == "for":
            return self.run_for(node, stdout, stdin)
        if kind == "while":
            return self.run_while(node, stdout, stdin)
        if kind == "case":
            return self.run_case(node, stdout, stdin)
        if kind == "function":
            self.env.define_function(node.name, node.body)
            return 0
        self._fail(stdout, "unsupported construct: %s" % kind)
        return 2

    def run_andor(self, node, stdout, stdin):
        status = self.run_node(node.left, stdout, stdin)
        self.env.status = status
        if node.operator == "&&" and status != 0:
            return status
        if node.operator == "||" and status == 0:
            return status
        return self.run_node(node.right, stdout, stdin)

    def run_pipeline(self, node, stdout, stdin):
        current_input = stdin
        status = 0
        last = len(node.commands) - 1
        for index, command in enumerate(node.commands):
            if index == last:
                status = self.run_node(command, stdout, current_input)
            else:
                buffer = MemorySink()
                status = self.run_node(command, buffer, current_input)
                current_input = Source(buffer.text)
        if node.negate:
            status = 0 if status != 0 else 1
        return status

    def run_group(self, node, stdout, stdin):
        with _Redirections(self, node.redirects, stdout, stdin) as (out, inp):
            if node.kind == "subshell":
                # variable changes must not leak out of ( )
                saved = dict(self.env.variables)
                saved_cwd = self.env.cwd
                try:
                    return self.run_script(node.body, out, inp)
                finally:
                    self.env.variables = saved
                    self.env.cwd = saved_cwd
            return self.run_script(node.body, out, inp)

    def run_if(self, node, stdout, stdin):
        with _Redirections(self, node.redirects, stdout, stdin) as (out, inp):
            for condition, body in node.clauses:
                if self.run_script(condition, NullSink(), inp) == 0:
                    return self.run_script(body, out, inp)
            if node.otherwise is not None:
                return self.run_script(node.otherwise, out, inp)
            return 0

    def run_for(self, node, stdout, stdin):
        with _Redirections(self, node.redirects, stdout, stdin) as (out, inp):
            items = []
            for word in node.items:
                items.extend(self.expand(word))
            status = 0
            for count, item in enumerate(items):
                if count >= MAX_LOOP_ITERATIONS:
                    self._fail(out, "for: too many iterations, stopping")
                    return 2
                self.env.set(node.name, item)
                status = self.run_script(node.body, out, inp)
                if self.env.exit_requested:
                    break
            return status

    def run_while(self, node, stdout, stdin):
        with _Redirections(self, node.redirects, stdout, stdin) as (out, inp):
            status = 0
            iterations = 0
            while True:
                condition = self.run_script(node.condition, NullSink(), inp)
                wanted = condition != 0 if node.until else condition == 0
                if not wanted:
                    break
                iterations += 1
                if iterations > MAX_LOOP_ITERATIONS:
                    self._fail(out, "loop ran %d times, stopping"
                               % MAX_LOOP_ITERATIONS)
                    return 2
                status = self.run_script(node.body, out, inp)
                if self.env.exit_requested:
                    break
            return status

    def run_case(self, node, stdout, stdin):
        with _Redirections(self, node.redirects, stdout, stdin) as (out, inp):
            subject = self.expand_string(node.word)
            for patterns, body in node.branches:
                for pattern in patterns:
                    if expand.matches_pattern(subject, self.expand_string(pattern)):
                        return self.run_script(body, out, inp)
            return 0

    # ----------------------------------------------------------------- simple

    def run_simple(self, node, stdout, stdin):
        assignments = [(name, self.expand_string(value))
                       for name, value in node.assignments]

        words = []
        for word in node.words:
            words.extend(self.expand(word))

        if not words:
            # `a=1` on its own sets a variable for the rest of the session
            for name, value in assignments:
                self.env.set(name, value)
            return 0

        words = self._apply_alias(words)

        with _Redirections(self, node.redirects, stdout, stdin) as (out, inp):
            if out is None:
                return 1
            saved = self._apply_temporary(assignments)
            try:
                return self.dispatch(words, out, inp)
            finally:
                self._restore_temporary(saved)

    def _apply_alias(self, words):
        alias = self.env.alias(words[0])
        if not alias:
            return words
        from .lexer import tokenize

        try:
            replacement = [token.value.text for token in tokenize(alias)
                           if token.kind == "word"]
        except ShellSyntaxError:
            return words
        return replacement + words[1:]

    def _apply_temporary(self, assignments):
        """`VAR=x command` sets VAR only for that command."""
        saved = []
        for name, value in assignments:
            saved.append((name, self.env.variables.get(name),
                          name in self.env.exported))
            self.env.set(name, value, export=True)
        return saved

    def _restore_temporary(self, saved):
        for name, previous, was_exported in saved:
            if previous is None:
                self.env.variables.pop(name, None)
            else:
                self.env.variables[name] = previous
            if not was_exported:
                self.env.exported.discard(name)

    def dispatch(self, words, stdout, stdin):
        """Runs one resolved command: function, builtin, or external program."""
        name, args = words[0], words[1:]

        body = self.env.function(name)
        if body is not None:
            return self._run_function(name, body, args, stdout, stdin)

        command = self.registry.get(name)
        if command is not None:
            return self._run_builtin(command, name, args, stdout, stdin)

        return self._run_external(words, stdout, stdin)

    def _run_function(self, name, body, args, stdout, stdin):
        if self._depth >= MAX_RECURSION:
            self._fail(stdout, "%s: too much recursion" % name)
            return 2
        saved_positional = self.env.positional
        self.env.positional = list(args)
        self._depth += 1
        try:
            return self.run_node(body, stdout, stdin)
        finally:
            self._depth -= 1
            self.env.positional = saved_positional

    def _run_builtin(self, command, name, args, stdout, stdin):
        ctx = self.ctx
        previous_stdin = getattr(ctx, "stdin", None)
        ctx.stdin = stdin
        try:
            result = command.run(ctx, args)
        except CommandError as e:
            result = e.as_result()
        except Exception as e:  # a broken builtin must not kill the shell
            result = blocks.error("%s: %s: %s" % (name, type(e).__name__, e))
        finally:
            ctx.stdin = previous_stdin

        if result is None:
            return 0
        stdout.write_result(result)
        return result.code

    def _run_external(self, words, stdout, stdin):
        name = words[0]
        no_backend = self.backend is None or not self.backend.available()
        if no_backend or not self.backend.which(name):
            # a typo deserves a suggestion whether or not a backend exists;
            # the missing-backend note is only worth saying when there is no
            # closer answer to give
            fallback = ("no execution backend on this device; type 'help'"
                        if no_backend else None)
            self._not_found(stdout, name, fallback)
            return 127
        # A program that takes a while should be watched, not waited for. Only
        # when its output is going to the screen: down a pipe the next command
        # wants the whole text, not a running commentary.
        result = self.backend.run(
            words,
            stdin_text=stdin.read() if stdin else "",
            cwd=self.env.cwd,
            env=self.env.environment(),
            on_output=stdout.write if stdout.is_terminal else None,
            # a program formats to the width of the screen, and it can only ask
            # a terminal how wide that is
            size=getattr(self.ctx, "screen", None) if stdout.is_terminal
            else None,
            # the left side of a pipe has already said everything it is going
            # to; without this the program waits for an end that a terminal
            # cannot give, which is what `ls | grep yaml` did
            feed=stdin is not None and not stdin.is_terminal,
            # and with nothing feeding it, whoever is at the console can
            on_channel=getattr(self.ctx, "attach_input", None)
            if stdin is None or stdin.is_terminal else None,
        )
        if result.out:
            stdout.write(result.out)
        if result.err:
            # stderr belongs in the same stream a terminal shows
            stdout.write(result.err if result.err.endswith("\n") else result.err + "\n")
        return result.status

    # ------------------------------------------------------------- expansion

    def expand(self, word):
        return expand.expand_word(word, self.env, self._substitute)

    def expand_string(self, word):
        return expand.expand_to_string(word, self.env, self._substitute)

    def _substitute(self, command_text):
        """$( ... ) — runs the inner script and returns its output."""
        if self._depth >= MAX_RECURSION:
            return ""
        from .parser import parse

        try:
            script = parse(command_text)
        except ShellSyntaxError:
            return ""
        buffer = MemorySink()
        self._depth += 1
        try:
            self.run_script(script, buffer, Source())
        finally:
            self._depth -= 1
        return buffer.text.rstrip("\n")

    # ------------------------------------------------------------------ error

    def _fail(self, sink, message, hint=None):
        sink.write_result(blocks.error(message, hint))

    def _backend_commands(self):
        try:
            names = self.backend.commands() if self.backend else None
        except Exception:
            return ()
        return names or ()

    def _not_found(self, sink, name, fallback_hint=None):
        """A mistyped command should point at the right one, not just fail."""
        from .registry import suggest

        candidates = set(self.registry.names(include_aliases=True))
        candidates.update(self.env.functions)
        candidates.update(self.env.aliases)
        # and whatever the backends offer: with a rootfs most of what can be
        # typed is Alpine's, and a suggestion drawn only from the builtins
        # would never name the thing that was meant
        candidates.update(self._backend_commands())
        hint = suggest(name, candidates)
        if hint is None:
            hint = fallback_hint or "type 'help' to see what is available"
        sink.write_result(blocks.error("command not found: %s" % name, hint, 127))


class _Redirections(object):
    """Applies a command's redirections for the duration of a `with` block."""

    def __init__(self, executor, redirects, stdout, stdin):
        self.executor = executor
        self.redirects = redirects or []
        self.stdout = stdout
        self.stdin = stdin
        self.opened = []

    def __enter__(self):
        out, inp = self.stdout, self.stdin
        for redirect in self.redirects:
            operator = redirect.operator
            try:
                if operator == "<<":
                    inp = Source(redirect.heredoc or "")
                    continue
                target = self.executor.expand_string(redirect.target) \
                    if redirect.target is not None else ""
                if operator == "<":
                    inp = Source.from_file(self.executor.env.host(target))
                elif operator in (">", ">>"):
                    path = self.executor.env.host(target)
                    directory = os.path.dirname(path)
                    if directory and not os.path.isdir(directory):
                        raise IOError("no such directory: %s" % directory)
                    sink = FileSink(path, append=(operator == ">>"))
                    self.opened.append(sink)
                    out = sink
                elif operator in (">&", "<&"):
                    # only 2>&1 and 1>&2 are meaningful here: both streams
                    # already share one sink, so this is a no-op
                    pass
            except Exception as e:
                self.executor._fail(self.stdout, "redirection failed: %s" % e)
                return None, inp
        return out, inp

    def __exit__(self, exc_type, exc, tb):
        for sink in self.opened:
            sink.close()
        return False
