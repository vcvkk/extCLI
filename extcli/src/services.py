# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Wiring: builds the Services object commands run against.

This is the only place that knows both sides — compat/ (client) and shell/
(commands). Keeping it out of shell/ is what lets the command tests import
shell/ without pulling in Android.
"""

from . import policy
from .backends import probe as probe_module
from .compat import host, paths, plugins, reflect
from .compat import messaging as messaging_module
from .compat import settings as settings_module
from .shell.context import Services
from .term import bridge
from .utils import log


class ProbeService(object):
    """Adapter around backends.probe: commands should not have to know about
    cache paths or HostFacts."""

    def __init__(self):
        self._cached = None

    def result(self, force=False):
        if self._cached is not None and not force:
            return self._cached
        self._cached = probe_module.get(
            paths.tmp_dir(), paths.state_dir(), host.probe_facts(), force=force
        )
        return self._cached

    def rootfs_verdict(self, result=None):
        return probe_module.rootfs_verdict(result or self.result())

    def backends(self):
        return self.result().get("backends", [])

    def extra_checks(self):
        """Component health that the probe itself cannot see."""
        ok, detail = bridge.self_check()
        return [("renderer", ok, detail)]

    def summary_lines(self, force=False):
        return probe_module.summary_lines(
            self.result(force=force), extra_checks=self.extra_checks()
        )


def build(plugin=None):
    services = Services(
        host=host,
        plugins=plugins if plugins.available() else None,
        paths=paths,
        settings=settings_module if settings_module.available() else None,
        messaging=messaging_module if messaging_module.available() else None,
        probe=ProbeService(),
        log=log,
        policy=policy,
        reflect=reflect,
    )
    if services.plugins is None:
        log.error("services: plugins controller unavailable")
    return services
