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
# How often the recovery timer checks that the tap is still alive (see below).
RECOVER_INTERVAL_SECONDS = 2.0


# Last flagsChanged seen by the NSEvent global monitor; None until the first
# one arrives. AppKit's monitor is the witness the tap can't be: its native
# side never lags (so macOS never kills it) and its events queue for the main
# loop instead of dropping, so a release is at worst late, never lost.
_monitor_down: bool | None = None


def _monitor_saw_flags(event) -> None:
    global _monitor_down
    _monitor_down = bool(event.modifierFlags() & RIGHT_CMD_DEVICE_MASK)


def right_cmd_sources() -> tuple[bool | None, bool | None, bool | None]:
    """Each source's belief about whether Right ⌘ is held; None means blind.

    The tap cannot be the only witness to the release: macOS silently disables
    a tap whose callback responds too slowly, and streaming inference starves
    the Python callback of the GIL for long enough to trigger exactly that — a
    dropped release left the recording running forever. No single alternative
    is trustworthy either (CGEventSourceKeyState turned out not to see modifier
    keys at all), hence several, arbitrated by ReleaseWatchdog.
    """
    state = Quartz.kCGEventSourceStateCombinedSessionState
    return (
        bool(Quartz.CGEventSourceKeyState(state, RIGHT_CMD_KEYCODE)),
        bool(Quartz.CGEventSourceFlagsState(state) & RIGHT_CMD_DEVICE_MASK),
        _monitor_down,
    )


def right_cmd_is_down() -> bool:
    return any(bool(s) for s in right_cmd_sources())


class ReleaseWatchdog:
    """Ends the hold when every source that has proven itself says 'up'.

    A source proves itself by reporting 'down' during this hold; only proven
    sources get a vote on the release. A source that is blind on this system
    (permanently False/None) therefore can neither end a dictation early nor
    keep one alive.
    """

    def __init__(self) -> None:
        self._armed: set[int] = set()

    def released(self, sources) -> bool:
        for i, down in enumerate(sources):
            if down:
                self._armed.add(i)
        return bool(self._armed) and not any(sources[i] for i in self._armed)


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
        self._recover_timer = None
        self._flags_monitor = None

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
        # A disabled-by-timeout tap normally re-enables itself from its own
        # disable notification, but that notification is itself a starved
        # callback — this timer is the recovery path that needs no events.
        self._recover_timer = Quartz.CFRunLoopTimerCreate(
            None,
            Quartz.CFAbsoluteTimeGetCurrent() + RECOVER_INTERVAL_SECONDS,
            RECOVER_INTERVAL_SECONDS,
            0,
            0,
            lambda _timer, _info: self._recover(right_cmd_is_down()),
            None,
        )
        Quartz.CFRunLoopAddTimer(
            Quartz.CFRunLoopGetCurrent(), self._recover_timer, Quartz.kCFRunLoopCommonModes
        )
        from AppKit import NSEvent, NSEventMaskFlagsChanged

        self._flags_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, _monitor_saw_flags
        )

    def _recover(self, key_down: bool) -> None:
        """Undo what a dead tap left behind: re-enable it, resync `_held`.

        A release missed by the tap leaves `_held` True, which would make the
        NEXT press look like a continuation and get ignored. The session that
        was recording has already ended itself via right_cmd_is_down, so no
        callback fires here — this only restores the state machine.
        """
        if self._tap is not None and not Quartz.CGEventTapIsEnabled(self._tap):
            Quartz.CGEventTapEnable(self._tap, True)
        if self._held and not key_down:
            self._held = False
            self._canceled = False

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
