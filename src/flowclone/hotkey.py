"""Global hold-to-talk hotkey: listen-only Quartz event tap on Right ⌘.

The tap is already watching every key and click to tell a dictation apart from a
⌘-shortcut, so it is also the cheapest possible place to notice that the caret
has moved: any real press clears context's memory of what it last pasted. That
is a direct call rather than another callback because it must happen no matter
which frontend built the tap, and a frontend that forgot to wire it would
silently paste against a stale caret.

Requires Input Monitoring permission for the hosting terminal app
(System Settings → Privacy & Security → Input Monitoring).
"""

import Quartz

from flowclone import context
from flowclone.inject import SYNTHETIC_MARK

RIGHT_CMD_KEYCODE = 54
# NX_DEVICERCMDKEYMASK — device-specific flag bit that distinguishes the right
# ⌘ key from the left one (kCGEventFlagMaskCommand covers both).
RIGHT_CMD_DEVICE_MASK = 0x0010


class HoldToTalk:
    """Fires on_press when Right ⌘ goes down and on_release when it comes up.

    If any other key is typed while the hotkey is held, the hold was a keyboard
    shortcut (e.g. ⌘V): on_cancel fires and the eventual release is swallowed.
    """

    def __init__(self, on_press, on_release, on_cancel) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._held = False
        self._canceled = False
        self._tap = None

    def install(self) -> None:
        """Create the tap and attach it to the CURRENT thread's run loop."""
        # Mouse-down is here only to invalidate the caret memory: clicking is the
        # one way to move the caret that produces no keystroke.
        mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseDown)
        )
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._handle,
            None,
        )
        if self._tap is None:
            raise PermissionError(
                "Could not create the keyboard event tap. Grant Input Monitoring to "
                "your terminal app (System Settings → Privacy & Security → Input "
                "Monitoring), then rerun."
            )
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
        )
        Quartz.CGEventTapEnable(self._tap, True)

    def run_forever(self) -> None:
        self.install()
        Quartz.CFRunLoopRun()

    def _handle(self, proxy, type_, event, refcon):
        if type_ in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            Quartz.CGEventTapEnable(self._tap, True)
            return event

        if type_ == Quartz.kCGEventFlagsChanged:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            if keycode == RIGHT_CMD_KEYCODE:
                down = bool(Quartz.CGEventGetFlags(event) & RIGHT_CMD_DEVICE_MASK)
                if down and not self._held:
                    self._held = True
                    self._canceled = False
                    self._on_press()
                elif not down and self._held:
                    self._held = False
                    if not self._canceled:
                        self._on_release()
                    self._canceled = False
        elif type_ == Quartz.kCGEventKeyDown:
            if (
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
                == SYNTHETIC_MARK
            ):
                return event  # our own paste event, not the user typing
            # A real keystroke, held or not: the caret is no longer where we
            # left it, so what we pasted last says nothing about it now.
            context.invalidate()
            if self._held and not self._canceled:
                self._canceled = True
                self._on_cancel()
        elif type_ in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown):
            context.invalidate()
        return event
