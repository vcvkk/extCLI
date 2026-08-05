# SPDX-License-Identifier: Apache-2.0

"""Entry point from the console to the shell.

One line in, one Result out. The parsing and execution live in parser.py and
executor.py; this keeps the console from having to know about either, and gives
every caller — console, `.cli` in a chat, scripts — the same behaviour.
"""

from ..render import blocks
from . import env as env_module
from .executor import Executor
from .lexer import ShellSyntaxError, tokenize


def ensure_env(ctx, home=None):
    """Gives a Context a shell session if it does not have one yet."""
    if ctx.env is None:
        start = home or _default_home(ctx)
        ctx.env = env_module.Env(cwd=start, home=start)
    return ctx.env


def _default_home(ctx):
    paths = getattr(ctx.services, "paths", None)
    for getter in ("home_dir", "data_dir", "files_dir"):
        method = getattr(paths, getter, None)
        if method is None:
            continue
        try:
            path = method()
        except Exception:
            continue
        if path:
            return path
    import os

    return os.getcwd()


def run_line(line, ctx, commands=None, backend=None):
    """Runs one command line. Never raises; failures come back as blocks."""
    registry = commands or ctx.registry
    if registry is None:
        return blocks.error("no commands registered")

    text = (line or "").strip()
    if not text:
        return blocks.Result()

    ensure_env(ctx)
    executor = Executor(ctx, registry, backend or ctx.backend)
    outcome = executor.run_text(text)

    ctx.env.status = outcome.status
    if ctx.env.exit_requested:
        ctx.request_exit()
        ctx.env.exit_requested = False

    result = outcome.result
    result.code = outcome.status
    return result


def split(line):
    """Words of a line, for completion. Tolerates an unfinished quote."""
    try:
        return [token.value.text for token in tokenize(line) if token.kind == "word"]
    except ShellSyntaxError:
        return line.replace('"', " ").replace("'", " ").split()
