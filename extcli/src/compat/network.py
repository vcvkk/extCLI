# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""What resolvers this phone is using.

There is no /etc/resolv.conf on Android to copy: resolution goes through netd,
and the servers are only visible through the framework. ConnectivityManager
knows them per network, which is the right granularity — the answer changes
when the phone moves between wifi and mobile data, and a guest holding the old
one gets a timeout rather than an error that says so.

The old way was the `net.dns1` system properties. They are gone on anything
recent and are not worth reading; when this cannot answer, the caller has a
fallback that works from anywhere.
"""

from ..utils import log


def dns_servers():
    """The active network's resolvers, in order, or ()."""
    try:
        from android.content import Context
        from org.telegram.messenger import ApplicationLoader

        context = ApplicationLoader.applicationContext
        manager = context.getSystemService(Context.CONNECTIVITY_SERVICE)
        if manager is None:
            return ()
        network = manager.getActiveNetwork()
        if network is None:
            return ()
        properties = manager.getLinkProperties(network)
        if properties is None:
            return ()
        found = []
        for address in properties.getDnsServers():
            text = str(address.getHostAddress() or "").strip()
            if text and text not in found:
                found.append(text)
        return tuple(found)
    except Exception as e:
        log.log("network: cannot read the phone's resolvers: %s" % e,
                debug=True)
        return ()
