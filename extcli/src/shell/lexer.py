# SPDX-License-Identifier: Apache-2.0

"""Tokenizer for the shell.

Quoting has to survive tokenization, because whether a `$x` gets expanded and
whether the result is split into fields both depend on the quotes it was written
in. So a word is not a string but a list of (quote, text) parts, and the
expander decides what each part means.
"""

# quoting of a word part
LITERAL = "literal"   # bare text: expand, split, glob
SINGLE = "single"     # 'text': nothing happens to it
DOUBLE = "double"     # "text": expand, but do not split or glob

# token kinds
WORD = "word"
OPERATOR = "operator"
NEWLINE = "newline"
EOF = "eof"

OPERATORS = (
    "&&", "||", ";;", "<<", ">>", ">&", "<&",
    "|", "&", ";", "(", ")", "<", ">",
)

# operators that may be preceded by a file descriptor number
REDIRECT_OPERATORS = ("<", ">", ">>", "<<", ">&", "<&")


class ShellSyntaxError(Exception):
    """Raised for input the parser cannot make sense of."""

    def __init__(self, message, position=None):
        super().__init__(message)
        self.message = message
        self.position = position


class Word(object):
    """A word as written, with its quoting preserved."""

    def __init__(self, parts=None):
        self.parts = list(parts or [])

    def add(self, quote, text):
        """Appends text, merging into the previous part when the quoting matches.

        Merging is not cosmetic: parts are the unit the expander works on, so a
        `$HOME` arriving one character at a time would never be recognised as a
        variable, and `a=1` would not look like an assignment.
        """
        if not text:
            return
        if self.parts and self.parts[-1][0] == quote:
            previous_quote, previous_text = self.parts[-1]
            self.parts[-1] = (previous_quote, previous_text + text)
            return
        self.parts.append((quote, text))

    @property
    def text(self):
        """The word with quotes removed and nothing expanded."""
        return "".join(text for _quote, text in self.parts)

    def is_quoted(self):
        return any(quote != LITERAL for quote, _ in self.parts)

    def starts_literal(self, prefix):
        for quote, text in self.parts:
            if quote != LITERAL:
                return False
            return text.startswith(prefix)
        return False

    def __repr__(self):
        return "Word(%r)" % self.text

    def __eq__(self, other):
        return isinstance(other, Word) and other.parts == self.parts


class Token(object):
    def __init__(self, kind, value, position=0, fd=None, quoted=False):
        self.kind = kind
        self.value = value          # str for operators, Word for words
        self.position = position
        self.fd = fd                # file descriptor for redirections
        self.quoted = quoted

    @property
    def text(self):
        return self.value.text if isinstance(self.value, Word) else self.value

    def is_op(self, *values):
        return self.kind == OPERATOR and self.value in values

    def __repr__(self):
        return "Token(%s, %r)" % (self.kind, self.text)


def tokenize(source):
    """Splits a command line into tokens. Raises ShellSyntaxError on bad quoting."""
    tokens = []
    i = 0
    length = len(source)
    word = None
    word_start = 0

    def flush():
        nonlocal word
        if word is not None:
            tokens.append(Token(WORD, word, word_start, quoted=word.is_quoted()))
            word = None

    while i < length:
        char = source[i]

        # comment: runs to the end of the line
        if char == "#" and word is None:
            while i < length and source[i] != "\n":
                i += 1
            continue

        if char == "\\":
            if i + 1 >= length:
                # trailing backslash: treat as a literal, the line is incomplete
                i += 1
                continue
            nxt = source[i + 1]
            if nxt == "\n":       # line continuation
                i += 2
                continue
            if word is None:
                word, word_start = Word(), i
            word.add(SINGLE, nxt)  # escaped: never expanded
            i += 2
            continue

        if char == "'":
            end = source.find("'", i + 1)
            if end == -1:
                raise ShellSyntaxError("unterminated single quote", i)
            if word is None:
                word, word_start = Word(), i
            word.add(SINGLE, source[i + 1:end])
            i = end + 1
            continue

        if char == '"':
            start = i
            text, i = _read_double_quoted(source, i)
            if word is None:
                word, word_start = Word(), start
            word.add(DOUBLE, text)
            continue

        if char == "\n":
            flush()
            tokens.append(Token(NEWLINE, "\n", i))
            i += 1
            continue

        if char in " \t":
            flush()
            i += 1
            continue

        if char == "$" and i + 1 < length and source[i + 1] == "(":
            # command substitution is a single unit, even with spaces inside
            start = i
            text, i = _read_balanced(source, i + 1, "(", ")")
            if word is None:
                word, word_start = Word(), start
            word.add(LITERAL, "$(" + text + ")")
            continue

        if char == "`":
            end = source.find("`", i + 1)
            if end == -1:
                raise ShellSyntaxError("unterminated backquote", i)
            if word is None:
                word, word_start = Word(), i
            word.add(LITERAL, "$(" + source[i + 1:end] + ")")
            i = end + 1
            continue

        operator = _match_operator(source, i)
        if operator:
            # a number glued to a redirection is a file descriptor
            fd = None
            if operator in REDIRECT_OPERATORS and word is not None:
                text = word.text
                if text.isdigit() and not word.is_quoted():
                    fd = int(text)
                    word = None
            flush()
            tokens.append(Token(OPERATOR, operator, i, fd=fd))
            i += len(operator)
            continue

        if word is None:
            word, word_start = Word(), i
        word.add(LITERAL, char)
        i += 1

    flush()
    tokens.append(Token(EOF, "", length))
    return tokens


def _match_operator(source, i):
    for operator in OPERATORS:
        if source.startswith(operator, i):
            return operator
    return None


def _read_double_quoted(source, i):
    """Reads a "..." run starting at the quote; returns (text, next index)."""
    out = []
    i += 1
    while i < len(source):
        char = source[i]
        if char == "\\" and i + 1 < len(source):
            nxt = source[i + 1]
            if nxt in '"\\$`':
                out.append(nxt)
                i += 2
                continue
            if nxt == "\n":
                i += 2
                continue
            out.append(char)
            i += 1
            continue
        if char == '"':
            return "".join(out), i + 1
        if char == "$" and source.startswith("$(", i):
            text, i = _read_balanced(source, i + 1, "(", ")")
            out.append("$(" + text + ")")
            continue
        out.append(char)
        i += 1
    raise ShellSyntaxError("unterminated double quote", i)


def _read_balanced(source, i, opener, closer):
    """Reads a balanced group; `i` points at the opener. Returns (inner, next)."""
    depth = 0
    start = i + 1
    while i < len(source):
        char = source[i]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return source[start:i], i + 1
        elif char == "'":
            end = source.find("'", i + 1)
            i = end if end != -1 else len(source)
        elif char == '"':
            _, i = _read_double_quoted(source, i)
            continue
        i += 1
    raise ShellSyntaxError("unterminated %s%s" % (opener, closer), start)
