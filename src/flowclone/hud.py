"""Floating live-preview panel: streaming partials rendered above every app.

A borderless, non-activating NSPanel anchored to the lower-right corner. Text
wraps at a fixed width and the panel grows UPWARD (bottom edge stays put) as
the transcript lengthens; past ~7 lines the oldest words are trimmed with an
ellipsis so the newest speech is always visible. A small level meter beside
the newest line dances red with the mic's input while recording — the instant
answer to "is it hearing me?" — and goes solid gray while the accurate batch
pass runs. It never takes focus and ignores the mouse, so the target app keeps
keyboard focus. All AppKit calls happen on the main thread; worker threads
must use the thread-safe show/update/finalize/hide/set_level/flash_error
wrappers.
"""

import math

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
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSNumber, NSObject

PILL_WIDTH = 380.0
V_PAD = 8.0
LINE_HEIGHT = 16.0  # one line of the 12 pt system font
MAX_TEXT_HEIGHT = 7 * LINE_HEIGHT  # growth cap; beyond it the head is trimmed
CORNER_MARGIN = 20.0  # inset from the lower-right corner of the visible screen
CORNER_RADIUS = 15.0
# The level meter: ascending bars beside the newest (bottom) line of text,
# lit red in proportion to the mic's RMS while recording.
BAR_COUNT = 5
BAR_WIDTH = 3.0
BAR_GAP = 2.0
METER_X = 12.0
LABEL_X = METER_X + BAR_COUNT * (BAR_WIDTH + BAR_GAP) + 3.0
LABEL_WIDTH = PILL_WIDTH - LABEL_X - 12.0
# How long an error flash (e.g. "mic unavailable") stays up before auto-hiding.
ERROR_FLASH_SECONDS = 2.5
# Perceptual mapping from RMS to lit bars. Log scale, because loudness is:
# ambient room noise sits near the floor, normal speech spans the middle.
RMS_FLOOR = 0.003
RMS_CEIL = 0.25


def bars_for_rms(rms: float) -> int:
    """How many of the BAR_COUNT bars to light; always ≥ 1 while recording,
    so the meter reads as 'live' even in silence."""
    if rms <= RMS_FLOOR:
        return 1
    frac = min(1.0, math.log(rms / RMS_FLOOR) / math.log(RMS_CEIL / RMS_FLOOR))
    return 1 + round(frac * (BAR_COUNT - 1))


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

        # The meter sits at the fixed bottom-left, beside the newest line of
        # text (text wraps top-down, so the latest words are always at the
        # bottom): ascending bars, bottom-aligned like an audio level meter.
        self._lit_color = NSColor.systemRedColor().CGColor()
        self._dim_color = NSColor.whiteColor().colorWithAlphaComponent_(0.18).CGColor()
        self._done_color = NSColor.systemGrayColor().CGColor()
        bars = []
        for i in range(BAR_COUNT):
            height = 4.0 + i * (LINE_HEIGHT - 4.0) / (BAR_COUNT - 1)
            bar = NSView.alloc().initWithFrame_(
                NSMakeRect(METER_X + i * (BAR_WIDTH + BAR_GAP), V_PAD, BAR_WIDTH, height)
            )
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(BAR_WIDTH / 2)
            bar.layer().setBackgroundColor_(self._dim_color)
            content.addSubview_(bar)
            bars.append(bar)

        label = NSTextField.labelWithString_("")
        label.setFrame_(NSMakeRect(LABEL_X, V_PAD, LABEL_WIDTH, LINE_HEIGHT))
        label.setFont_(NSFont.systemFontOfSize_(12))
        label.setTextColor_(NSColor.whiteColor())
        cell = label.cell()
        cell.setWraps_(True)
        cell.setLineBreakMode_(NSLineBreakByWordWrapping)
        content.addSubview_(label)

        self._panel = panel
        self._bars = bars
        self._label = label
        self._finalizing = False
        # Bumped by every show/flash; a pending flash auto-hide only fires if
        # its generation is still current, so it can never hide a session that
        # started after the flash.
        self._flash_gen = 0
        self._origin = (0.0, 0.0)
        self._anchor_to_screen()
        return self

    # ---- main-thread selectors ----

    def showText_(self, text):
        self._flash_gen += 1
        self._anchor_to_screen()
        self._finalizing = False
        self._paint_bars(1)
        self._layout(text)
        self._panel.orderFrontRegardless()

    def flashErrorText_(self, text):
        """Show `text` with the meter solid red, then auto-hide."""
        self._flash_gen += 1
        self._anchor_to_screen()
        self._finalizing = True  # freeze set_level so nothing repaints the bars
        for bar in self._bars:
            bar.layer().setBackgroundColor_(self._lit_color)
        self._layout(text)
        self._panel.orderFrontRegardless()
        self.performSelector_withObject_afterDelay_(
            "hideFlash:", NSNumber.numberWithLong_(self._flash_gen), ERROR_FLASH_SECONDS
        )

    def hideFlash_(self, gen):
        if gen.longValue() == self._flash_gen:
            self._panel.orderOut_(None)

    def updateText_(self, text):
        self._layout(text)

    def setLevelRms_(self, rms):
        if self._finalizing:
            return  # the last blocks drain after release; keep the gray state
        self._paint_bars(bars_for_rms(float(rms)))

    def finalizeHud_(self, _):
        self._finalizing = True
        for bar in self._bars:
            bar.layer().setBackgroundColor_(self._done_color)

    def hideHud_(self, _):
        self._panel.orderOut_(None)

    # ---- helpers (plain Python, not exposed as selectors) ----

    @objc.python_method
    def _paint_bars(self, lit: int):
        for i, bar in enumerate(self._bars):
            bar.layer().setBackgroundColor_(
                self._lit_color if i < lit else self._dim_color
            )

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
    def set_level(self, rms: float) -> None:
        """Feed one mic block's RMS; drives the level meter. Any thread."""
        self._call("setLevelRms:", float(rms))

    @objc.python_method
    def flash_error(self, text: str) -> None:
        """Show an error pill that hides itself after ERROR_FLASH_SECONDS."""
        self._call("flashErrorText:", text)

    @objc.python_method
    def finalize(self) -> None:
        self._call("finalizeHud:", None)

    @objc.python_method
    def hide(self) -> None:
        self._call("hideHud:", None)

    @objc.python_method
    def _call(self, selector: str, obj) -> None:
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, obj, False)
