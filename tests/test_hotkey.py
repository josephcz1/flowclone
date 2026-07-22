"""The event tap's handler, driven with constructed events.

These events are never posted, only handed straight to HoldToTalk._handle, so
running the suite cannot type into whatever app happens to be focused.

Run: uv run pytest
"""

import Quartz
import pytest

from flowclone import context
from flowclone.hotkey import HoldToTalk
from flowclone.inject import SYNTHETIC_MARK

A_KEYCODE = 0  # kVK_ANSI_A


@pytest.fixture
def tap():
    """A handler wired to record its callbacks, with an empty paste memory."""
    calls = []
    hold = HoldToTalk(
        lambda: calls.append("press"),
        lambda: calls.append("release"),
        lambda: calls.append("cancel"),
    )
    hold.calls = calls
    context.invalidate()
    yield hold
    context.invalidate()


def key_event(synthetic: bool = False):
    event = Quartz.CGEventCreateKeyboardEvent(None, A_KEYCODE, True)
    if synthetic:
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGEventSourceUserData, SYNTHETIC_MARK
        )
    return event


def test_typing_invalidates_the_paste_memory(tap):
    context.remember("some text", "com.apple.Terminal")
    tap._handle(None, Quartz.kCGEventKeyDown, key_event(), None)
    assert context.recall("com.apple.Terminal") is None


def test_our_own_paste_does_not_invalidate(tap):
    """⌘V is posted by inject, so it must not clear what it just pasted."""
    context.remember("some text", "com.apple.Terminal")
    tap._handle(None, Quartz.kCGEventKeyDown, key_event(synthetic=True), None)
    assert context.recall("com.apple.Terminal") == "some text"


@pytest.mark.parametrize(
    "kind", [Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown]
)
def test_clicking_invalidates_the_paste_memory(tap, kind):
    """Clicking is the one way to move the caret with no keystroke at all."""
    context.remember("some text", "com.apple.Terminal")
    event = Quartz.CGEventCreateMouseEvent(None, kind, (10.0, 10.0), 0)
    tap._handle(None, kind, event, None)
    assert context.recall("com.apple.Terminal") is None


def test_typing_while_idle_does_not_fire_cancel(tap):
    """Invalidation happens on every keystroke; cancel only during a hold."""
    tap._handle(None, Quartz.kCGEventKeyDown, key_event(), None)
    assert tap.calls == []


def test_a_shortcut_during_a_hold_still_cancels(tap):
    """The pre-existing behavior: Right ⌘ + another key is a shortcut, not speech."""
    tap._held = True
    tap._handle(None, Quartz.kCGEventKeyDown, key_event(), None)
    assert tap.calls == ["cancel"]
    assert tap._canceled


def test_a_shortcut_cancels_only_once(tap):
    tap._held = True
    for _ in range(3):
        tap._handle(None, Quartz.kCGEventKeyDown, key_event(), None)
    assert tap.calls == ["cancel"]
