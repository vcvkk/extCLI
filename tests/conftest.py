# SPDX-License-Identifier: Apache-2.0

"""Makes the plugin importable off device.

On a device the client loads extcli/src as a package, so the code uses relative
imports throughout. To reproduce that here the directory is registered as a
package named `extcli_src`, which is what the tests import from. Loading it
under a single name matters: importing the same module twice under two names
would give the tests different class objects than the code uses.
"""

import importlib.util
import sys
from pathlib import Path

PACKAGE = "extcli_src"
SRC = Path(__file__).resolve().parent.parent / "extcli" / "src"


def _install():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, SRC / "__init__.py", submodule_search_locations=[str(SRC)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_install()
