# SPDX-License-Identifier: Apache-2.0

"""Java callbacks are built once.

This is a regression test for a crash that took a while to read:

    com.chaquo.python.PyException: AttributeError:
        '_Run' object has no attribute '_chaquopyGetDict'
      at <python>.java.chaquopy.set_this(class.pxi:1381)
      ...
      at $Proxy7.run(Unknown Source)
      at android.os.Handler.handleCallback(Handler.java:1082)

Every `dynamic_proxy` class in this plugin was defined inside the function
that needed one, so a new class was made on every call — the terminal's redraw
timer made one per frame of output, and a held soft key made one per repeat
tick. The generated Java proxy class is shared between all of them, so the way
back from a Java proxy to the Python class that should handle it has many
candidates; an instance of the wrong one reaches Chaquopy's own machinery
without the fields that machinery installs, and dies.

Nothing in that trace points at the definition that caused it, so the useful
thing to leave behind is not a fix but a rule that can be checked: outside
`compat/proxies.py`, no module defines one. These tests check the rule.
"""

import ast
from pathlib import Path

import pytest

from extcli_src.compat import proxies

SRC = Path(__file__).resolve().parent.parent / "extcli" / "src"
HOME = SRC / "compat" / "proxies.py"


def sources():
    return sorted(path for path in SRC.rglob("*.py")
                  if "__pycache__" not in path.parts)


def test_only_one_module_makes_proxy_classes():
    """A class per call is the bug; a class per interface is the fix."""
    guilty = [path for path in sources()
              if path != HOME and "dynamic_proxy" in path.read_text()]
    assert guilty == [], [str(path.relative_to(SRC)) for path in guilty]


def test_every_proxy_class_in_that_module_is_built_by_the_cache():
    """A `dynamic_proxy` subclass reached any other way would be built afresh
    on every call, which is the thing this module exists to stop."""
    found = proxy_classes(HOME.read_text())
    assert found, "no proxy classes found — the check is not looking at them"
    for name, parent in found:
        # every one of them sits inside a `build()` that `_once` calls
        assert parent == "build", (name, parent)


def proxy_classes(source):
    """[(class name, the function it is defined in)] for every proxy class."""
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_is_proxy(base) for base in node.bases):
            continue
        out.append((node.name, _function_around(node, parents)))
    return out


def _is_proxy(base):
    return (isinstance(base, ast.Call)
            and isinstance(base.func, ast.Name)
            and base.func.id == "dynamic_proxy")


def _function_around(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef):
            return node.name
    return None


def test_the_check_would_notice_a_class_defined_the_old_way():
    """Otherwise the test above passes by not looking."""
    bad = proxy_classes(
        "def helper(fn):\n"
        "    class Run(dynamic_proxy(Runnable)):\n"
        "        pass\n"
        "    return Run()\n")
    assert bad == [("Run", "helper")]


def test_the_cache_hands_back_the_same_class(monkeypatch):
    """The whole point. Two calls, one class."""
    made = []

    def build():
        made.append(1)
        return type("Fake", (), {})

    monkeypatch.setattr(proxies, "_classes", {})
    first = proxies._once("test", build)
    second = proxies._once("test", build)
    assert first is second
    assert made == [1]


def test_a_build_that_fails_is_not_quietly_retried_into_two_classes():
    """`_once` must not swallow a failure and try again later: the second
    attempt would succeed and there would be two classes, which is the bug it
    exists to prevent."""
    source = HOME.read_text()
    body = source[source.index("def _once("):source.index("def _report(")]
    assert "except" not in body


def test_every_interface_the_plugin_uses_has_a_helper():
    """So that adding a listener means calling one of these rather than
    reaching for `dynamic_proxy` again."""
    for name in ("runnable", "touch_listener", "long_click_listener",
                 "layout_listener", "pre_draw_listener", "insets_listener",
                 "text_watcher", "editor_action_listener", "key_listener",
                 "focus_listener", "selection_callback", "dismiss_listener",
                 "animation_listener"):
        assert callable(getattr(proxies, name)), name


def test_the_helpers_need_no_device_until_they_are_called():
    """`java` does not exist here, and every test in this repository imports
    the modules that use these."""
    assert proxies.names() == []
    with pytest.raises(Exception):
        proxies.runnable(lambda: None)
    # and a failed build left nothing half-made behind
    assert proxies.names() == []


def test_the_callback_is_carried_by_the_instance_and_not_the_class():
    """Which is what lets there be one class: the SDK's own OnClickListener
    works the same way, and always has."""
    source = HOME.read_text()
    for chunk in source.split("    def build():")[1:]:
        assert "def __init__(self" in chunk
        assert "super().__init__()" in chunk


def test_the_keys_the_console_sends_straight_to_a_program():
    """They moved out of a proxy class defined per console; they are the same
    keys and they must stay a plain table."""
    from extcli_src.ui import console

    assert console.RAW_CODES[67] == "\x7f"      # backspace, the one that matters
    assert console.RAW_CODES[66] == "\r"
    assert console.RAW_CODES[19] == "\x1b[A"


def test_a_raw_key_is_only_taken_while_a_program_is_running():
    from extcli_src.ui import console

    class Session(object):
        def __init__(self, channel):
            self.channel = channel
            self.typed = []

        def _program_channel(self):
            return self.channel

        def type_raw(self, sequence):
            self.typed.append(sequence)
            return True

    class Event(object):
        def __init__(self, action):
            self.action = action

        def getAction(self):
            return self.action

    idle = Session(None)
    assert console._raw_key(idle)(67, Event(0)) is False
    assert idle.typed == []

    busy = Session(object())
    assert console._raw_key(busy)(67, Event(0)) is True
    assert busy.typed == ["\x7f"]
    # a key going up is not a second press
    assert console._raw_key(busy)(67, Event(1)) is False
    # and a key nothing is listening for is left to the field
    assert console._raw_key(busy)(99, Event(0)) is False
    assert busy.typed == ["\x7f"]
