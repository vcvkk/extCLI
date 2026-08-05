# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

from typing import Any, List

from base_plugin import BasePlugin

from . import main

# Entry point only. Everything else lives in main.py so this file can stay
# uncompiled (see compilationIgnore in .elyxbuilder/config.yml) — the loader
# needs to read the class out of plain source.


class Main(BasePlugin):
    def __init__(self):
        super().__init__()
        main.start_init(self)

    def on_plugin_load(self):
        main.load_plugin(self)

    def on_plugin_unload(self):
        main.unload_plugin(self)

    def on_send_message_hook(self, account: int, params: Any):
        return main.on_send_message_hook(self, account, params)

    def create_settings(self) -> List[Any]:
        return main.create_settings(self)
