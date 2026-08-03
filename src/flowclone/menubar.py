"""Milestone 7: menu-bar app wrapping the hold-to-talk daemon (rumps).

Shows a status icon that reflects the pipeline stage (idle / recording /
processing), recovery/history actions, Pause and login toggles, and a native
Preferences window. rumps owns the main run loop, so the Quartz event
tap and the live-preview HUD both attach to it — the same run loop that used to
be driven by a bare NSApplication in the terminal flow.

All UI mutation happens on the main thread: the model loads on a background
thread and worker threads report state via AppHelper.callAfter.
"""

import threading
from pathlib import Path

import rumps
from PyObjCTools.AppHelper import callAfter

from flowclone import config, context, history, inject, login

# Emoji fallback per pipeline state, used only if SF Symbol rendering fails.
TITLES = {
    "loading": "⏳",
    "idle": "🎤",
    "recording": "🔴",
    "processing": "✨",
    "paused": "⏸",
}
# SF Symbol per state, rendered to template PNGs at startup: monochrome
# outlines that the system tints to match the menu bar (light/dark/tinted).
ICON_SYMBOLS = {
    "loading": "hourglass",
    "idle": "mic",
    "recording": "mic.fill",
    "processing": "waveform",
    "paused": "mic.slash",
}
ICON_CACHE = Path.home() / "Library" / "Caches" / "FlowClone"
ICON_PIXELS = 44  # rumps shows the icon at 20 pt; 44 px keeps it Retina-sharp
READY_HINT = "Ready — hold Right ⌘ to dictate"


def _render_symbol(symbol: str, path: Path) -> bool:
    """Rasterize an SF Symbol to a black-on-transparent PNG for template use."""
    from AppKit import (
        NSBitmapImageRep,
        NSCompositingOperationSourceOver,
        NSDeviceRGBColorSpace,
        NSFontWeightRegular,
        NSGraphicsContext,
        NSImage,
        NSImageSymbolConfiguration,
        NSMakeRect,
        NSPNGFileType,
        NSZeroRect,
    )

    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
    if image is None:
        return False
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        ICON_PIXELS * 0.72, NSFontWeightRegular
    )
    image = image.imageWithSymbolConfiguration_(cfg) or image

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, ICON_PIXELS, ICON_PIXELS, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    size = image.size()
    scale = min((ICON_PIXELS * 0.9) / size.width, (ICON_PIXELS * 0.9) / size.height)
    w, h = size.width * scale, size.height * scale
    image.drawInRect_fromRect_operation_fraction_(
        NSMakeRect((ICON_PIXELS - w) / 2, (ICON_PIXELS - h) / 2, w, h),
        NSZeroRect,
        NSCompositingOperationSourceOver,
        1.0,
    )
    NSGraphicsContext.restoreGraphicsState()
    png = rep.representationUsingType_properties_(NSPNGFileType, None)
    return bool(png.writeToFile_atomically_(str(path), True))


def render_state_icons() -> dict[str, str] | None:
    """Template icon path per state, or None so callers fall back to TITLES."""
    try:
        ICON_CACHE.mkdir(parents=True, exist_ok=True)
        icons = {}
        for state, symbol in ICON_SYMBOLS.items():
            path = ICON_CACHE / f"menubar-{symbol.replace('.', '-')}.png"
            if not _render_symbol(symbol, path):
                return None
            icons[state] = str(path)
        return icons
    except Exception:
        return None


class FlowCloneApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("FlowClone", title=None, quit_button="Quit FlowClone")
        self.icons = render_state_icons()
        self.template = True  # let the system tint the icon for the menu bar
        self._show_state_glyph("loading")
        self.status_item = rumps.MenuItem("Loading model…")
        self.pause_item = rumps.MenuItem("Pause dictation", callback=self.toggle_pause)
        self.copy_last_item = rumps.MenuItem(
            "Copy Last Dictation", callback=self.copy_last
        )
        self.paste_last_item = rumps.MenuItem(
            "Paste Last Dictation", callback=self.paste_last
        )
        self.history_item = rumps.MenuItem("Recent Dictations")
        self.history_item.add(rumps.MenuItem("No history yet"))
        self.login_item = rumps.MenuItem(
            "Launch at Login", callback=self.toggle_launch_at_login
        )
        self.login_item.state = int(login.enabled())
        self.menu = [
            self.status_item,
            None,
            self.pause_item,
            self.paste_last_item,
            self.copy_last_item,
            self.history_item,
            None,
            self.login_item,
            rumps.MenuItem("Preferences…", callback=self.show_preferences),
        ]
        self.cfg = config.load()
        self.transcriber = None
        self.hud = None
        self.hotkey = None
        self.paused = False
        self.controller = None
        self.preferences = None
        self._refresh_history_menu()
        # Heavy setup waits until the run loop is live so the icon appears now.
        callAfter(self._start)

    # ---- startup (main thread) ----

    def _start(self) -> None:
        from flowclone.hud import HudPanel

        self.hud = HudPanel.alloc().init()
        if not inject.can_post_events():
            inject.request_post_event_access()
        threading.Thread(target=self._load_model, daemon=True).start()

    def _load_model(self) -> None:
        from flowclone.stt import Transcriber

        transcriber = Transcriber(quantization=config.load_model().quantization)
        transcriber.warmup()
        callAfter(self._model_ready, transcriber)

    def _model_ready(self, transcriber) -> None:
        from flowclone.main import SessionController

        self.transcriber = transcriber
        self.controller = SessionController(
            transcriber,
            self.hud,
            self.cfg,
            handsfree_cfg=config.load_handsfree(),
            on_state=lambda state: callAfter(self.set_state, state),
            on_history=lambda: callAfter(self._refresh_history_menu),
            on_notice=lambda title, message: callAfter(
                self._notify, title, message
            ),
        )
        self._install_hotkey()

    def _install_hotkey(self) -> None:
        from flowclone.hotkey import HoldToTalk

        self.hotkey = HoldToTalk(self.on_press, self.on_release, self.on_cancel)
        try:
            self.hotkey.install()  # attaches the tap to rumps' run loop
        except PermissionError as exc:
            self.status_item.title = "Grant Input Monitoring, then relaunch"
            self._notify("Permission needed", str(exc))
            return
        self.set_state("idle")
        self.status_item.title = READY_HINT

    # ---- hotkey callbacks (run on the tap's run loop = main thread) ----

    def on_press(self) -> None:
        if self.controller is None or self.paused:
            return
        self.controller.on_press()

    def on_release(self) -> None:
        if self.controller is not None:
            self.controller.on_release()

    def on_cancel(self) -> None:
        if self.controller is not None:
            self.controller.on_cancel()

    # ---- menu actions ----

    def toggle_pause(self, _) -> None:
        self.paused = not self.paused
        if self.paused:
            if self.controller is not None:
                self.controller.cancel_active()
            self.pause_item.title = "Resume dictation"
            self._show_state_glyph("paused")
            self.status_item.title = "Paused — dictation off"
        else:
            self.pause_item.title = "Pause dictation"
            self.set_state("idle")
            self.status_item.title = READY_HINT

    def show_preferences(self, _) -> None:
        from flowclone.preferences import PreferencesWindow

        if self.preferences is None:
            self.preferences = PreferencesWindow.alloc().init()
        self.preferences.show(self._preferences_saved)

    def _preferences_saved(self, restart_required: bool) -> None:
        self.cfg = config.load()
        if self.controller is not None:
            self.controller.cleanup_cfg = self.cfg
            self.controller.handsfree_cfg = config.load_handsfree()
        if restart_required:
            self._notify(
                "Preferences saved",
                "Model precision will change the next time FlowClone launches.",
            )

    def copy_last(self, _) -> None:
        entries = history.recent()
        if not entries:
            self._notify("No dictations", "Transcript history is empty.")
            return
        inject.copy_text(entries[0].text)
        self._notify("Copied", "The last dictation is on the clipboard.")

    def paste_last(self, _) -> None:
        entries = history.recent()
        if not entries:
            self._notify("No dictations", "Transcript history is empty.")
            return
        if not inject.can_post_events():
            self._notify("Accessibility needed", "FlowClone cannot post paste events.")
            return
        text = entries[0].text
        if inject.paste_text(text):
            context.remember(text, context.frontmost_app_id())
        else:
            self._notify("Paste blocked", "Secure Input is active in the focused app.")

    def clear_history(self, _) -> None:
        history.clear()
        self._refresh_history_menu()

    def _copy_history_entry(self, text: str) -> None:
        inject.copy_text(text)
        self._notify("Copied", "The selected dictation is on the clipboard.")

    def _refresh_history_menu(self) -> None:
        self.history_item.clear()
        entries = history.recent()
        if not entries:
            self.history_item.add(rumps.MenuItem("No history yet"))
            return
        for index, entry in enumerate(entries[:10], start=1):
            snippet = " ".join(entry.text.split())
            if len(snippet) > 52:
                snippet = snippet[:51] + "…"
            item = rumps.MenuItem(
                f"{index}. {snippet}",
                callback=lambda _, text=entry.text: self._copy_history_entry(text),
            )
            self.history_item.add(item)
        self.history_item.add(rumps.MenuItem("Clear History", callback=self.clear_history))

    def toggle_launch_at_login(self, _) -> None:
        try:
            if login.enabled():
                login.disable()
            else:
                login.enable()
            self.login_item.state = int(login.enabled())
        except login.LoginItemError as exc:
            self.login_item.state = int(login.enabled())
            self._notify("Launch at Login", str(exc))

    # ---- helpers ----

    def set_state(self, state: str) -> None:
        if self.paused and state == "idle":
            state = "paused"
        self._show_state_glyph(state if state in TITLES else "idle")

    def _show_state_glyph(self, state: str) -> None:
        if self.icons is not None:
            self.icon = self.icons[state]
        else:
            self.title = TITLES[state]

    def _notify(self, title: str, message: str) -> None:
        try:
            rumps.notification("FlowClone", title, message)
        except Exception:
            pass  # notifications need a bundled app; ignore when run as a script


def run() -> None:
    FlowCloneApp().run()
