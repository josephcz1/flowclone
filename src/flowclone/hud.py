"""Floating live-preview panel: streaming partials rendered above every app.

A borderless, non-activating NSPanel anchored to the lower-right corner. Text
wraps at a fixed width and the panel grows UPWARD (bottom edge stays put) as
the transcript lengthens; past ~7 lines the oldest words are trimmed with an
ellipsis so the newest speech is always visible. It never takes focus and
ignores the mouse, so the target app keeps keyboard focus. All AppKit calls
happen on the main thread; worker threads must use the thread-safe
show/update/finalize/hide wrappers.
"""

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSFont,
    NSLineBreakByWordWrapping,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSTextField,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSObject
from Quartz import (
    CABasicAnimation,
    CAMediaTimingFunction,
    kCAMediaTimingFunctionEaseInEaseOut,
)

PILL_WIDTH = 380.0
LABEL_X = 26.0
LABEL_WIDTH = PILL_WIDTH - LABEL_X - 12.0
V_PAD = 8.0
LINE_HEIGHT = 16.0  # one line of the 12 pt system font
MAX_TEXT_HEIGHT = 7 * LINE_HEIGHT  # growth cap; beyond it the head is trimmed
CORNER_MARGIN = 20.0  # inset from the lower-right corner of the visible screen
CORNER_RADIUS = 15.0
DOT_PULSE_PERIOD = 0.6  # seconds per fade half-cycle (full pulse = 2×)
DOT_PULSE_MIN_ALPHA = 0.25  # dimmest point of the recording flicker


class HudPanel(NSObject):
    """Owns the panel. Must be alloc().init()'d on the main thread."""

    def init(self):
        self = objc.super(HudPanel, self).init()
        if self is None:
            return None

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PILL_WIDTH, LINE_HEIGHT + 2 * V_PAD),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setHasShadow_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        content = panel.contentView()
        content.setWantsLayer_(True)
        layer = content.layer()
        layer.setCornerRadius_(CORNER_RADIUS)
        layer.setMasksToBounds_(True)
        layer.setBackgroundColor_(
            NSColor.blackColor().colorWithAlphaComponent_(0.8).CGColor()
        )

        # The dot sits at the fixed bottom-left, beside the newest line of text
        # (text wraps top-down, so the latest words are always at the bottom).
        dot = NSTextField.labelWithString_("●")
        dot.setFrame_(NSMakeRect(12, V_PAD, 10, 14))
        dot.setFont_(NSFont.systemFontOfSize_(8))
        dot.setTextColor_(NSColor.systemRedColor())
        dot.setWantsLayer_(True)  # layer needed for the opacity pulse animation
        content.addSubview_(dot)

        label = NSTextField.labelWithString_("")
        label.setFrame_(NSMakeRect(LABEL_X, V_PAD, LABEL_WIDTH, LINE_HEIGHT))
        label.setFont_(NSFont.systemFontOfSize_(12))
        label.setTextColor_(NSColor.whiteColor())
        cell = label.cell()
        cell.setWraps_(True)
        cell.setLineBreakMode_(NSLineBreakByWordWrapping)
        content.addSubview_(label)

        self._panel = panel
        self._dot = dot
        self._label = label
        self._origin = (0.0, 0.0)
        self._anchor_to_screen()
        return self

    # ---- main-thread selectors ----

    def showText_(self, text):
        self._anchor_to_screen()
        self._dot.setTextColor_(NSColor.systemRedColor())
        self._start_pulse()  # flicker like a mic recording indicator while listening
        self._layout(text)
        self._panel.orderFrontRegardless()

    def updateText_(self, text):
        self._layout(text)

    def finalizeHud_(self, _):
        self._stop_pulse()  # steady gray dot while the batch pass runs
        self._dot.setTextColor_(NSColor.systemGrayColor())

    def hideHud_(self, _):
        self._stop_pulse()  # covers early-abort hides that skip finalize
        self._panel.orderOut_(None)

    # ---- helpers (plain Python, not exposed as selectors) ----

    @objc.python_method
    def _start_pulse(self):
        """Pulse the dot's opacity so it flickers like a recording indicator.

        Runs on Core Animation's render server (no main-thread/timer cost).
        """
        anim = CABasicAnimation.animationWithKeyPath_("opacity")
        anim.setFromValue_(1.0)
        anim.setToValue_(DOT_PULSE_MIN_ALPHA)
        anim.setDuration_(DOT_PULSE_PERIOD)
        anim.setAutoreverses_(True)
        anim.setRepeatCount_(float("inf"))
        anim.setTimingFunction_(
            CAMediaTimingFunction.functionWithName_(kCAMediaTimingFunctionEaseInEaseOut)
        )
        self._dot.layer().addAnimation_forKey_(anim, "pulse")

    @objc.python_method
    def _stop_pulse(self):
        layer = self._dot.layer()
        layer.removeAnimationForKey_("pulse")
        layer.setOpacity_(1.0)  # leave the dot solid once the flicker stops

    @objc.python_method
    def _layout(self, text):
        """Set the text, then grow the panel upward to fit (bottom edge fixed)."""
        text = text or ""
        self._label.setStringValue_(text)
        height = self._measure()
        if height > MAX_TEXT_HEIGHT:
            words = text.split()
            while words and height > MAX_TEXT_HEIGHT:
                words = words[8:]
                self._label.setStringValue_("…" + " ".join(words))
                height = self._measure()
        text_height = max(height, LINE_HEIGHT)
        self._label.setFrame_(NSMakeRect(LABEL_X, V_PAD, LABEL_WIDTH, text_height))
        x, y = self._origin
        self._panel.setFrame_display_(
            NSMakeRect(x, y, PILL_WIDTH, text_height + 2 * V_PAD), True
        )

    @objc.python_method
    def _measure(self):
        bounds = NSMakeRect(0, 0, LABEL_WIDTH, 100000.0)
        return self._label.cell().cellSizeForBounds_(bounds).height

    @objc.python_method
    def _anchor_to_screen(self):
        frame = self._screen_with_mouse().visibleFrame()
        x = frame.origin.x + frame.size.width - PILL_WIDTH - CORNER_MARGIN
        y = frame.origin.y + CORNER_MARGIN
        self._origin = (x, y)

    @objc.python_method
    def _screen_with_mouse(self):
        mouse = NSEvent.mouseLocation()
        for screen in NSScreen.screens():
            f = screen.frame()
            if (
                f.origin.x <= mouse.x <= f.origin.x + f.size.width
                and f.origin.y <= mouse.y <= f.origin.y + f.size.height
            ):
                return screen
        return NSScreen.screens()[0]

    # ---- thread-safe wrappers (call from any thread) ----

    @objc.python_method
    def show(self, text: str) -> None:
        self._call("showText:", text)

    @objc.python_method
    def update(self, text: str) -> None:
        self._call("updateText:", text)

    @objc.python_method
    def finalize(self) -> None:
        self._call("finalizeHud:", None)

    @objc.python_method
    def hide(self) -> None:
        self._call("hideHud:", None)

    @objc.python_method
    def _call(self, selector: str, obj) -> None:
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, obj, False)
