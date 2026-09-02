# pyright: reportMissingImports=false
# SPDX-License-Identifier: Apache-2.0

"""Java callbacks, made once.

Chaquopy turns a Python class into something Java can call through
`dynamic_proxy(SomeInterface)`. The obvious way to use it is the way this
plugin used it everywhere — define the class inside the function that needs
one, close over whatever it should do, and return an instance:

    def _runnable(function):
        class _Run(dynamic_proxy(Runnable)):
            def run(self):
                function()
        return _Run()

That is wrong, and it fails in a way that gives no hint of why. Every call
makes a *new* class. The generated Java proxy class is shared between all of
them, so the mapping from a Java proxy back to the Python class that should
handle it has many candidates and picks one; an instance of any of the others
then arrives at Chaquopy's own machinery without the fields that machinery
installs, and the callback dies with

    AttributeError: '_Run' object has no attribute '_chaquopyGetDict'

somewhere inside `Handler.handleCallback`, with nothing in the trace pointing
at the definition that caused it. The terminal's redraw timer went through
this on every frame of output, so it was a matter of when rather than whether.

So: one class per interface, built the first time it is wanted and kept. What
the callback should do is passed to the instance instead of captured by the
class — which is what the SDK's own `OnClickListener` does, and it has always
worked.

Building is deferred rather than done at import: `java` does not exist off a
device, and every test in this repository imports the modules that use these.
"""

from ..utils import log

_classes = {}


def _once(name, build):
    """The class for one interface, built the first time and then kept.

    The whole point of this module is that `build` runs once. Nothing here
    may be tempted into rebuilding on failure: a second class is the bug.
    """
    if name not in _classes:
        _classes[name] = build()
    return _classes[name]


def _report(what, error):
    log.error("proxies: %s failed" % what, error)


# ------------------------------------------------------------------ runnable


def runnable(function):
    """A `java.lang.Runnable` that calls `function`."""
    return _runnable_class()(function)


def _runnable_class():
    def build():
        from java import dynamic_proxy
        from java.lang import Runnable

        class Run(dynamic_proxy(Runnable)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def run(self):
                try:
                    self.function()
                except Exception as e:
                    _report("a scheduled call", e)

        return Run

    return _once("runnable", build)


# ------------------------------------------------------------------ touching


def touch_listener(function):
    """`View.OnTouchListener`. `function(view, event)` returns True if handled."""
    return _touch_class()(function)


def _touch_class():
    def build():
        from android.view import View
        from java import dynamic_proxy

        class Touch(dynamic_proxy(View.OnTouchListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onTouch(self, view, event):
                try:
                    return bool(self.function(view, event))
                except Exception as e:
                    _report("a touch", e)
                    return False

        return Touch

    return _once("touch", build)


def long_click_listener(function):
    """`View.OnLongClickListener`. Returning False lets the press fall through
    to the view's own handler, which is how text selection ever runs."""
    return _long_click_class()(function)


def _long_click_class():
    def build():
        from android.view import View
        from java import dynamic_proxy

        class LongClick(dynamic_proxy(View.OnLongClickListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onLongClick(self, view):
                try:
                    return bool(self.function(view))
                except Exception as e:
                    _report("a long press", e)
                    return True

        return LongClick

    return _once("long_click", build)


# ------------------------------------------------------------------- layout


def layout_listener(function):
    """`View.OnLayoutChangeListener`.

    `function(view, left, top, right, bottom, old_left, old_top, old_right,
    old_bottom)` — the nine Android passes, unchanged, because a caller that
    wants to know whether the width moved needs both widths.
    """
    return _layout_class()(function)


def _layout_class():
    def build():
        from android.view import View
        from java import dynamic_proxy

        class Layout(dynamic_proxy(View.OnLayoutChangeListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onLayoutChange(self, view, left, top, right, bottom,
                               old_left, old_top, old_right, old_bottom):
                try:
                    self.function(view, left, top, right, bottom,
                                  old_left, old_top, old_right, old_bottom)
                except Exception as e:
                    _report("a layout change", e)

        return Layout

    return _once("layout", build)


def pre_draw_listener(function):
    """`ViewTreeObserver.OnPreDrawListener`.

    `function()` returns True to let the frame be drawn, False to skip it —
    which is what a listener that has just changed a size wants, so nothing is
    shown at the wrong one.
    """
    return _pre_draw_class()(function)


def _pre_draw_class():
    def build():
        from android.view import ViewTreeObserver
        from java import dynamic_proxy

        class PreDraw(dynamic_proxy(ViewTreeObserver.OnPreDrawListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onPreDraw(self):
                try:
                    return bool(self.function())
                except Exception as e:
                    _report("a pre-draw", e)
                    return True

        return PreDraw

    return _once("pre_draw", build)


def insets_listener(function):
    """`View.OnApplyWindowInsetsListener`. `function(view, insets)` returns the
    insets to pass on."""
    return _insets_class()(function)


def _insets_class():
    def build():
        from android.view import View
        from java import dynamic_proxy

        class Insets(dynamic_proxy(View.OnApplyWindowInsetsListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onApplyWindowInsets(self, view, insets):
                try:
                    return self.function(view, insets)
                except Exception as e:
                    _report("window insets", e)
                    return insets

        return Insets

    return _once("insets", build)


# -------------------------------------------------------------------- input


def text_watcher(on_changed):
    """`TextWatcher`, of which only `onTextChanged` is ever wanted."""
    return _text_watcher_class()(on_changed)


def _text_watcher_class():
    def build():
        from android.text import TextWatcher
        from java import dynamic_proxy

        class Watcher(dynamic_proxy(TextWatcher)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def beforeTextChanged(self, text, start, count, after):
                return None

            def onTextChanged(self, text, start, before, count):
                try:
                    self.function(str(text))
                except Exception as e:
                    _report("an input change", e)

            def afterTextChanged(self, editable):
                return None

        return Watcher

    return _once("text_watcher", build)


def editor_action_listener(function):
    """`TextView.OnEditorActionListener`. `function(view)` — the action id is
    not passed on because every caller here wants "they pressed enter"."""
    return _editor_action_class()(function)


def _editor_action_class():
    def build():
        from android.widget import TextView
        from java import dynamic_proxy

        class EditorAction(dynamic_proxy(TextView.OnEditorActionListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onEditorAction(self, view, action_id, event):
                try:
                    self.function(view)
                except Exception as e:
                    _report("an editor action", e)
                return True

        return EditorAction

    return _once("editor_action", build)


def key_listener(function):
    """`View.OnKeyListener`. `function(code, event)` returns True if it took
    the key, False to let the field have it."""
    return _key_class()(function)


def _key_class():
    def build():
        from android.view import View
        from java import dynamic_proxy

        class Keys(dynamic_proxy(View.OnKeyListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onKey(self, view, code, event):
                try:
                    return bool(self.function(int(code), event))
                except Exception as e:
                    _report("a key", e)
                    return False

        return Keys

    return _once("key", build)


# ------------------------------------------------------------------- others


def focus_listener(function):
    """`View.OnFocusChangeListener`. `function(has_focus)`."""
    return _focus_class()(function)


def _focus_class():
    def build():
        from android.view import View
        from java import dynamic_proxy

        class Focus(dynamic_proxy(View.OnFocusChangeListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onFocusChange(self, view, has_focus):
                try:
                    self.function(bool(has_focus))
                except Exception as e:
                    _report("a focus change", e)

        return Focus

    return _once("focus", build)


def selection_callback(on_start, on_end):
    """`ActionMode.Callback`, for the text-selection toolbar.

    Only its life is wanted, not its menu: `onPrepareActionMode` returning
    False leaves the toolbar exactly as the system built it, and nothing here
    claims any of its items.
    """
    return _selection_class()(on_start, on_end)


def _selection_class():
    def build():
        from android.view import ActionMode
        from java import dynamic_proxy

        class Selection(dynamic_proxy(ActionMode.Callback)):
            def __init__(self, on_start, on_end):
                super().__init__()
                self.on_start = on_start
                self.on_end = on_end

            def onCreateActionMode(self, mode, menu):
                try:
                    self.on_start()
                except Exception as e:
                    _report("a selection starting", e)
                return True

            def onPrepareActionMode(self, mode, menu):
                return False

            def onActionItemClicked(self, mode, item):
                return False

            def onDestroyActionMode(self, mode):
                try:
                    self.on_end()
                except Exception as e:
                    _report("a selection ending", e)

        return Selection

    return _once("selection", build)


def dismiss_listener(function):
    """`DialogInterface.OnDismissListener`."""
    return _dismiss_class()(function)


def _dismiss_class():
    def build():
        from android.content import DialogInterface
        from java import dynamic_proxy

        class Dismiss(dynamic_proxy(DialogInterface.OnDismissListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onDismiss(self, dialog):
                try:
                    self.function(dialog)
                except Exception as e:
                    _report("a dismiss", e)

        return Dismiss

    return _once("dismiss", build)


def animation_listener(function):
    """`DynamicAnimation.OnAnimationUpdateListener`. `function(value)`."""
    return _animation_class()(function)


def _animation_class():
    def build():
        from androidx.dynamicanimation.animation import DynamicAnimation
        from java import dynamic_proxy

        class Update(dynamic_proxy(DynamicAnimation.OnAnimationUpdateListener)):
            def __init__(self, function):
                super().__init__()
                self.function = function

            def onAnimationUpdate(self, animation, value, velocity):
                try:
                    self.function(value)
                except Exception as e:
                    _report("an animation update", e)

        return Update

    return _once("animation", build)


def names():
    """Which proxy classes have been built, for the diagnostics page."""
    return sorted(_classes)
