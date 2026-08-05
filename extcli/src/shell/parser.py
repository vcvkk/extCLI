# SPDX-License-Identifier: Apache-2.0

"""Recursive-descent parser for the shell subset extCLI supports.

Covered: pipelines, && and ||, ; and & separators, redirections including
here-documents, variable assignments, if/elif/else, for, while/until, case,
{ } groups, ( ) subshells and function definitions.

Not covered: job control beyond parsing `&`, coprocesses, arrays. Those are
either meaningless in a plugin or better served by a real shell through the
system backend.
"""

from . import nodes
from .lexer import (DOUBLE, EOF, LITERAL, NEWLINE, OPERATOR, SINGLE, WORD,
                    ShellSyntaxError, Word, tokenize)

RESERVED = frozenset({
    "if", "then", "elif", "else", "fi",
    "for", "in", "do", "done",
    "while", "until",
    "case", "esac",
    "{", "}", "!",
})

# words that end a command list, so the caller knows to stop
TERMINATORS = frozenset({"then", "elif", "else", "fi", "do", "done", "esac", "}"})


def parse(source):
    """Parses a whole script. Returns a nodes.Script."""
    return Parser(tokenize(source)).parse_script()


class Parser(object):
    def __init__(self, tokens):
        self.tokens = tokens
        self.index = 0
        # here-document bodies are collected after the line that requests them
        self._pending_heredocs = []

    # ------------------------------------------------------------- token help

    @property
    def current(self):
        return self.tokens[self.index]

    def peek(self, offset=1):
        position = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[position]

    def advance(self):
        token = self.tokens[self.index]
        if self.index < len(self.tokens) - 1:
            self.index += 1
        return token

    def at_eof(self):
        return self.current.kind == EOF

    def skip_newlines(self):
        while self.current.kind == NEWLINE:
            self.advance()

    def word_text(self, token):
        return token.value.text if token.kind == WORD else token.value

    def is_reserved(self, token, *names):
        if token.kind != WORD:
            return False
        if token.value.is_quoted():
            return False
        text = token.value.text
        return text in (names or RESERVED)

    def expect_reserved(self, name):
        if not self.is_reserved(self.current, name):
            raise ShellSyntaxError("expected %r, found %r"
                                   % (name, self.current.text),
                                   self.current.position)
        return self.advance()

    # ---------------------------------------------------------------- script

    def parse_script(self, stop_words=()):
        script = nodes.Script()
        while True:
            self.skip_newlines()
            if self.at_eof():
                break
            if stop_words and self.is_reserved(self.current, *stop_words):
                break
            # ')' ends a subshell, ';;' ends a case branch: both belong to the
            # caller, not to this list
            if self.current.is_op(")", ";;"):
                break
            node = self.parse_and_or()
            separator = ";"
            if self.current.is_op(";", "&"):
                separator = self.advance().value
            elif self.current.kind == NEWLINE:
                self.advance()
                self.collect_heredocs()
            script.add(node, separator)
        return script

    def parse_and_or(self):
        node = self.parse_pipeline()
        while self.current.is_op("&&", "||"):
            operator = self.advance().value
            self.skip_newlines()
            right = self.parse_pipeline()
            node = nodes.AndOr(node, operator, right)
        return node

    def parse_pipeline(self):
        negate = False
        while self.is_reserved(self.current, "!"):
            self.advance()
            negate = not negate
        commands = [self.parse_command()]
        while self.current.is_op("|"):
            self.advance()
            self.skip_newlines()
            commands.append(self.parse_command())
        if len(commands) == 1 and not negate:
            return commands[0]
        return nodes.Pipeline(commands, negate)

    # --------------------------------------------------------------- commands

    def parse_command(self):
        token = self.current
        if token.is_op("("):
            self.advance()
            body = self.parse_script()
            if not self.current.is_op(")"):
                raise ShellSyntaxError("expected ')'", self.current.position)
            self.advance()
            return nodes.Subshell(body, self.parse_redirects())
        if self.is_reserved(token, "{"):
            self.advance()
            body = self.parse_script(stop_words=("}",))
            self.expect_reserved("}")
            return nodes.Group(body, self.parse_redirects())
        if self.is_reserved(token, "if"):
            return self.parse_if()
        if self.is_reserved(token, "for"):
            return self.parse_for()
        if self.is_reserved(token, "while", "until"):
            return self.parse_while()
        if self.is_reserved(token, "case"):
            return self.parse_case()
        if self._looks_like_function():
            return self.parse_function()
        return self.parse_simple()

    def _looks_like_function(self):
        """`name ( )` — a definition, not a command called `name`."""
        if self.current.kind != WORD:
            return False
        if self.is_reserved(self.current):
            return False
        return self.peek().is_op("(") and self.peek(2).is_op(")")

    def parse_function(self):
        name = self.word_text(self.advance())
        self.advance()  # (
        self.advance()  # )
        self.skip_newlines()
        body = self.parse_command()
        return nodes.FunctionDef(name, body)

    def parse_simple(self):
        assignments = []
        words = []
        redirects = []

        while True:
            token = self.current
            if token.kind == WORD:
                if not words and _is_assignment(token.value):
                    name, value = _split_assignment(token.value)
                    assignments.append((name, value))
                    self.advance()
                    continue
                words.append(self.advance().value)
                continue
            if token.kind == OPERATOR and token.value in ("<", ">", ">>", "<<",
                                                          ">&", "<&"):
                redirects.append(self.parse_redirect())
                continue
            break

        command = nodes.Simple(words, assignments, redirects)
        if command.is_empty():
            raise ShellSyntaxError("expected a command, found %r" % self.current.text,
                                   self.current.position)
        return command

    def parse_redirects(self):
        redirects = []
        while self.current.kind == OPERATOR and self.current.value in (
                "<", ">", ">>", "<<", ">&", "<&"):
            redirects.append(self.parse_redirect())
        return redirects

    def parse_redirect(self):
        token = self.advance()
        operator, fd = token.value, token.fd
        if operator == "<<":
            target = self.current
            if target.kind != WORD:
                raise ShellSyntaxError("expected a here-document delimiter",
                                       target.position)
            self.advance()
            redirect = nodes.Redirect(operator, None, fd, heredoc="")
            self._pending_heredocs.append((redirect, target.value.text))
            return redirect
        if self.current.kind != WORD:
            raise ShellSyntaxError("expected a redirection target, found %r"
                                   % self.current.text, self.current.position)
        return nodes.Redirect(operator, self.advance().value, fd)

    def collect_heredocs(self):
        """Reads here-document bodies that follow the current line."""
        while self._pending_heredocs:
            redirect, delimiter = self._pending_heredocs.pop(0)
            lines = []
            while True:
                if self.at_eof():
                    break
                token = self.current
                if token.kind == WORD and token.value.text == delimiter:
                    self.advance()
                    break
                if token.kind == NEWLINE:
                    self.advance()
                    lines.append("")
                    continue
                lines.append(self._raw_line())
            redirect.heredoc = "\n".join(line for line in lines if line != "") + "\n"

    def _raw_line(self):
        """Words up to the next newline, joined with single spaces.

        Here-documents lose exact inner spacing this way; it is a deliberate
        simplification — the alternative is a second lexer mode, and here-docs
        in a phone console are used for short blobs.
        """
        parts = []
        while not self.at_eof() and self.current.kind != NEWLINE:
            parts.append(self.advance().text)
        return " ".join(parts)

    # ------------------------------------------------------ compound commands

    def parse_if(self):
        self.expect_reserved("if")
        clauses = []
        condition = self.parse_script(stop_words=("then",))
        self.expect_reserved("then")
        body = self.parse_script(stop_words=("elif", "else", "fi"))
        clauses.append((condition, body))

        otherwise = None
        while self.is_reserved(self.current, "elif"):
            self.advance()
            elif_condition = self.parse_script(stop_words=("then",))
            self.expect_reserved("then")
            elif_body = self.parse_script(stop_words=("elif", "else", "fi"))
            clauses.append((elif_condition, elif_body))
        if self.is_reserved(self.current, "else"):
            self.advance()
            otherwise = self.parse_script(stop_words=("fi",))
        self.expect_reserved("fi")
        return nodes.If(clauses, otherwise, self.parse_redirects())

    def parse_for(self):
        self.expect_reserved("for")
        if self.current.kind != WORD:
            raise ShellSyntaxError("expected a variable name after 'for'",
                                   self.current.position)
        name = self.word_text(self.advance())
        items = []
        self.skip_newlines()
        if self.is_reserved(self.current, "in"):
            self.advance()
            while self.current.kind == WORD and not self.is_reserved(self.current, "do"):
                items.append(self.advance().value)
            if self.current.is_op(";"):
                self.advance()
        else:
            # `for x; do` iterates the positional parameters
            items = [Word([(LITERAL, "$@")])]
            if self.current.is_op(";"):
                self.advance()
        self.skip_newlines()
        self.expect_reserved("do")
        body = self.parse_script(stop_words=("done",))
        self.expect_reserved("done")
        return nodes.For(name, items, body, self.parse_redirects())

    def parse_while(self):
        keyword = self.word_text(self.advance())
        condition = self.parse_script(stop_words=("do",))
        self.expect_reserved("do")
        body = self.parse_script(stop_words=("done",))
        self.expect_reserved("done")
        return nodes.While(condition, body, until=(keyword == "until"),
                           redirects=self.parse_redirects())

    def parse_case(self):
        self.expect_reserved("case")
        if self.current.kind != WORD:
            raise ShellSyntaxError("expected a word after 'case'",
                                   self.current.position)
        subject = self.advance().value
        self.skip_newlines()
        self.expect_reserved("in")
        branches = []
        while True:
            self.skip_newlines()
            if self.is_reserved(self.current, "esac") or self.at_eof():
                break
            patterns = []
            if self.current.is_op("("):
                self.advance()
            while True:
                if self.current.kind != WORD:
                    raise ShellSyntaxError("expected a case pattern, found %r"
                                           % self.current.text,
                                           self.current.position)
                patterns.append(self.advance().value)
                if self.current.is_op("|"):
                    self.advance()
                    continue
                break
            if not self.current.is_op(")"):
                raise ShellSyntaxError("expected ')' after a case pattern",
                                       self.current.position)
            self.advance()
            body = self.parse_script(stop_words=("esac",))
            branches.append((patterns, body))
            if self.current.is_op(";;"):
                self.advance()
        self.expect_reserved("esac")
        return nodes.Case(subject, branches, self.parse_redirects())


def _is_assignment(word):
    """`name=value` written unquoted before the command name."""
    if not word.parts:
        return False
    quote, text = word.parts[0]
    if quote != LITERAL or "=" not in text:
        return False
    name = text.split("=", 1)[0]
    if not name or name[0].isdigit():
        return False
    return all(char == "_" or char.isalnum() for char in name)


def _split_assignment(word):
    """Splits `name=value` into (name, Word(value)) keeping the value's quoting."""
    quote, text = word.parts[0]
    name, _, first_value = text.partition("=")
    parts = []
    if first_value:
        parts.append((quote, first_value))
    parts.extend(word.parts[1:])
    return name, Word(parts)


# re-exported so callers do not need the lexer directly
__all__ = ["parse", "Parser", "ShellSyntaxError", "DOUBLE", "SINGLE", "LITERAL"]
