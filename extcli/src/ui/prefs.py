# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Reading the plugin's own settings.

The settings page stores selectors as indices, so both the page and the console
have to agree on what index 1 means. Keeping that mapping in one module is the
only way it stays true.
"""

RENDERERS = ("views", "fast")
DEFAULT_RENDERER_INDEX = 0

# the console is a screen, not a dialog: it has no header and fills the display
SURFACES = ("screen", "sheet")
DEFAULT_SURFACE_INDEX = 0

THEMES = ("termux", "default", "amoled")
TEXT_SIZES = (10, 11, 12, 13, 14, 16)

DEFAULT_THEME_INDEX = 0
DEFAULT_TEXT_SIZE_INDEX = 2  # 12sp


def _get(key, default):
    from ..compat import store

    return store.get(key, default)


def _index(key, default_index, count):
    try:
        value = int(_get(key, default_index))
    except (TypeError, ValueError):
        value = default_index
    return value if 0 <= value < count else default_index


def theme_name():
    return THEMES[_index("theme", DEFAULT_THEME_INDEX, len(THEMES))]


def text_size():
    return TEXT_SIZES[_index("text_size_index", DEFAULT_TEXT_SIZE_INDEX,
                             len(TEXT_SIZES))]


def style_name():
    """The style the on-screen console renders with."""
    from ..render import styles

    name = str(_get("style", styles.CONSOLE_DEFAULT))
    return name if name in styles.names() else styles.CONSOLE_DEFAULT


def renderer():
    """Which terminal widget to build: stock views, or the dex renderer."""
    return RENDERERS[_index("renderer", DEFAULT_RENDERER_INDEX, len(RENDERERS))]


def console_surface():
    """Where the console opens: a bottom sheet over the chat, or its own screen."""
    return SURFACES[_index("console_surface", DEFAULT_SURFACE_INDEX,
                           len(SURFACES))]


def entry_enabled(where):
    return bool(_get("entry_" + where, True))


def debug_logs():
    return bool(_get("debug_logs", False))


def tools_offered():
    """Has the question about the tools been put once already?

    Asked once and remembered whichever way it was answered: somebody who said
    no meant no, and somebody who said yes has them.
    """
    return bool(_get("tools_offered", False))


def remember_tools_offered():
    from ..compat import store

    store.set("tools_offered", True)


def auto_setup():
    """Should the rootfs make itself ready without being asked?

    On, because the alternative is a console that answers "not found" to `apk`
    until the user has run four commands they have never heard of. Off is for
    somebody who wants the phone left alone until they say so.
    """
    return bool(_get("auto_setup", True))


def mount_values():
    """Which of the paths a guest can see.

    Stored one switch per mount. `mounts.enabled` puts the rootfs back if a
    stored state somehow has none of them on, because a console with nowhere
    to open is not a state anyone can act on.
    """
    from ..rootfs import mounts

    return {mount.key: bool(_get(mount.setting, mount.key in mounts.DEFAULT_ON))
            for mount in mounts.MOUNTS}


def mount_hosts():
    """Where each mount really is on this phone."""
    from ..compat import paths
    from ..rootfs import mounts

    return {
        mounts.ROOT: paths.rootfs_dir(),
        mounts.SDCARD: paths.storage_dir(),
        mounts.EXTERA: paths.files_dir(),
        mounts.EXTCLI: paths.data_dir(),
        mounts.PATCH: paths.patch_dir(),
    }


def mount_table():
    """[(guest path, host path)] for the loader, honouring the switches."""
    from ..rootfs import mounts

    return mounts.table(mount_values(), mount_hosts())
