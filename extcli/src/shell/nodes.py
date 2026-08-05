# SPDX-License-Identifier: Apache-2.0

"""Syntax tree the parser produces and the executor walks."""


class Node(object):
    kind = "node"


class Script(Node):
    """A sequence of commands separated by ';', '&' or newlines."""

    kind = "script"

    def __init__(self, items=None):
        # items: [(node, separator)] with separator in {";", "&"}
        self.items = list(items or [])

    def add(self, node, separator=";"):
        self.items.append((node, separator))

    def __len__(self):
        return len(self.items)


class AndOr(Node):
    kind = "andor"

    def __init__(self, left, operator, right):
        self.left = left
        self.operator = operator   # "&&" or "||"
        self.right = right


class Pipeline(Node):
    kind = "pipeline"

    def __init__(self, commands, negate=False):
        self.commands = list(commands)
        self.negate = negate


class Redirect(object):
    """One redirection: operator, optional fd, and its target word."""

    def __init__(self, operator, target, fd=None, heredoc=None):
        self.operator = operator   # "<" ">" ">>" "<<" ">&" "<&"
        self.target = target       # Word, or None for a here-document
        self.fd = fd
        self.heredoc = heredoc     # text for "<<"

    def default_fd(self):
        if self.fd is not None:
            return self.fd
        return 0 if self.operator in ("<", "<<", "<&") else 1


class Simple(Node):
    kind = "simple"

    def __init__(self, words=None, assignments=None, redirects=None):
        self.words = list(words or [])              # [Word]
        self.assignments = list(assignments or [])  # [(name, Word)]
        self.redirects = list(redirects or [])      # [Redirect]

    def is_empty(self):
        return not self.words and not self.assignments and not self.redirects


class Group(Node):
    """{ ...; } — runs in the current shell."""

    kind = "group"

    def __init__(self, body, redirects=None):
        self.body = body
        self.redirects = list(redirects or [])


class Subshell(Node):
    """( ...; ) — same thing here, but variable changes do not leak out."""

    kind = "subshell"

    def __init__(self, body, redirects=None):
        self.body = body
        self.redirects = list(redirects or [])


class If(Node):
    kind = "if"

    def __init__(self, clauses=None, otherwise=None, redirects=None):
        # clauses: [(condition Script, body Script)] — if plus any elifs
        self.clauses = list(clauses or [])
        self.otherwise = otherwise
        self.redirects = list(redirects or [])


class For(Node):
    kind = "for"

    def __init__(self, name, items, body, redirects=None):
        self.name = name
        self.items = list(items)   # [Word]
        self.body = body
        self.redirects = list(redirects or [])


class While(Node):
    kind = "while"

    def __init__(self, condition, body, until=False, redirects=None):
        self.condition = condition
        self.body = body
        self.until = until
        self.redirects = list(redirects or [])


class Case(Node):
    kind = "case"

    def __init__(self, word, branches=None, redirects=None):
        self.word = word
        # branches: [([Word patterns], Script body)]
        self.branches = list(branches or [])
        self.redirects = list(redirects or [])


class FunctionDef(Node):
    kind = "function"

    def __init__(self, name, body):
        self.name = name
        self.body = body
