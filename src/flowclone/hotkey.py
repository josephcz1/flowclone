"""Global hold-to-talk hotkey: listen-only Quartz event tap on Right ⌘.

Requires Input Monitoring permission for the hosting terminal app
(System Settings → Privacy & Security → Input Monitoring).
"""

import Quartz

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
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) | Quartz.CGEventMaskBit(
            Quartz.kCGEventKeyDown
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
        elif type_ == Quartz.kCGEventKeyDown and self._held and not self._canceled:
            if (
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
                == SYNTHETIC_MARK
            ):
                return event  # our own paste event, not the user typing
            self._canceled = True
            self._on_cancel()
        return event
