# SPDX-License-Identifier: Apache-2.0

"""Built-in commands and the registry they populate."""

from ..registry import Registry
from . import config as config_cmd
from . import help as help_cmd
from . import host as host_cmd
from . import log as log_cmd
from . import plugin as plugin_cmd
from . import rootfs as rootfs_cmd
from . import search as search_cmd
from . import send as send_cmd
from . import session as session_cmd
from . import shellcmds


def build_registry():
    """The command set the console starts with."""
    registry = Registry()
    registry.register(host_cmd.build())
    registry.register(config_cmd.build())
    registry.register(plugin_cmd.build(), aliases=("plugins",))
    registry.register(log_cmd.build())
    registry.register(send_cmd.build())
    registry.register(search_cmd.build())
    registry.register(rootfs_cmd.build())
    for command in session_cmd.build_all():
        registry.register(command)
    for command in shellcmds.build_all():
        # `[` is the same command as `test`, as in every shell
        aliases = ("[",) if command.name == "test" else ()
        registry.register(command, aliases=aliases)
    # help needs the finished registry, so it goes last
    registry.register(help_cmd.HelpCommand(registry), aliases=("?",))
    return registry
