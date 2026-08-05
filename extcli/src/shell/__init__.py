# SPDX-License-Identifier: Apache-2.0

"""The command layer.

For now `dispatch` splits a line with shlex and hands the words to a command
from `registry`. The full POSIX shell (variables, pipes, redirection, control
flow) replaces the parsing half in the next stage — the registry and the
Command interface are built to survive that, since builtins will be exactly the
same objects then.

Nothing in this package imports Android APIs: commands reach the client only
through the services on their Context, which is what lets the tests run here.
"""
