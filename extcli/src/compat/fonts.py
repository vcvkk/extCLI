# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Monospace typeface for the console.

The client already ships one (assets/fonts/rmono.ttf), so extCLI does not
bundle a font of its own. Falls back to the platform monospace family.
"""

from ..utils import log

_ASSET = "fonts/rmono.ttf"
_cached = None
_tried = False


def mono_typeface():
    global _cached, _tried
    if _tried:
        return _cached
    _tried = True
    try:
        from android.graphics import Typeface
        from org.telegram.messenger import ApplicationLoader

        assets = ApplicationLoader.applicationContext.getAssets()
        _cached = Typeface.createFromAsset(assets, _ASSET)
        log.log("fonts: loaded %s" % _ASSET, debug=True)
    except Exception as e:
        log.error("fonts: %s unavailable, falling back to monospace" % _ASSET, e)
        try:
            from android.graphics import Typeface

            _cached = Typeface.MONOSPACE
        except Exception:
            _cached = None
    return _cached
