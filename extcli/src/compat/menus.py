# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Entry points in the client's menus.

SDK 1.4.5.0 exposes this officially through BasePlugin.add_menu_item, with
MenuItemType covering every place extCLI wants to appear:

    DRAWER_MENU           the side menu
    MAIN_MENU             the chat list's overflow menu
    CHAT_ACTION_MENU      the three dots inside a chat
    MESSAGE_CONTEXT_MENU  long-press on a message
    PROFILE_ACTION_MENU   a profile's action menu

No hooks are needed for any of them.
"""

from ..utils import log

# menu type name -> our short name, used by settings and commands
TYPES = {
    "drawer": "DRAWER_MENU",
    "main": "MAIN_MENU",
    "chat": "CHAT_ACTION_MENU",
    "message": "MESSAGE_CONTEXT_MENU",
    "profile": "PROFILE_ACTION_MENU",
}

_registered = []


def menu_type(name):
    """The client's MenuItemType member for one of our short names."""
    try:
        from base_plugin import MenuItemType

        attr = TYPES.get(name, name)
        value = getattr(MenuItemType, attr, None)
        if value is None:
            log.error("menus: MenuItemType has no %s" % attr)
        return value
    except Exception as e:
        log.error("menus: MenuItemType unavailable", e)
        return None


def add(plugin, where, text, on_click, subtext=None, icon=None, priority=None,
        item_id=None, condition=None):
    """Adds one menu item. Returns its id, or None if the client refused.

    Argument names are taken from the SDK's MenuItemData; unexpected shapes are
    logged rather than raised so a client change costs a menu entry and not the
    whole plugin.
    """
    kind = menu_type(where)
    if kind is None:
        return None
    try:
        from base_plugin import MenuItemData
    except Exception as e:
        log.error("menus: MenuItemData unavailable", e)
        return None

    kwargs = {"menu_type": kind, "text": str(text), "on_click": on_click}
    if subtext:
        kwargs["subtext"] = str(subtext)
    if icon:
        kwargs["icon"] = str(icon)
    if priority is not None:
        kwargs["priority"] = int(priority)
    if item_id:
        kwargs["item_id"] = str(item_id)
    if condition is not None:
        kwargs["condition"] = condition

    try:
        data = MenuItemData(**kwargs)
    except TypeError as e:
        # a renamed or reordered field: report exactly what was rejected
        log.error("menus: MenuItemData(%s) rejected" % ", ".join(sorted(kwargs)), e)
        return None
    except Exception as e:
        log.error("menus: cannot build menu item %r" % text, e)
        return None

    try:
        result = plugin.add_menu_item(data)
    except Exception as e:
        log.error("menus: add_menu_item(%r) failed" % text, e)
        return None

    identifier = item_id or (str(result) if result is not None else None)
    if identifier:
        _registered.append(identifier)
    log.log("menus: added %r to %s" % (text, where), debug=True)
    return identifier


def remove_all(plugin):
    """Called on unload; the client also cleans up by plugin id."""
    for identifier in list(_registered):
        try:
            plugin.remove_menu_item(identifier)
        except Exception as e:
            log.log("menus: remove_menu_item(%s) failed: %s" % (identifier, e),
                    debug=True)
    _registered.clear()
