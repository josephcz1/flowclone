"""Milestone 7: menu-bar app wrapping the hold-to-talk daemon (rumps).

Shows a status icon that reflects the pipeline stage (idle / recording /
processing), a Pause toggle that disables the hotkey without quitting, and a
shortcut to edit config.toml. rumps owns the main run loop, so the Quartz event
tap and the live-preview HUD both attach to it — the same run loop that used to
be driven by a bare NSApplication in the terminal flow.

All UI mutation happens on the main thread: the model loads on a background
thread and worker threads report state via AppHelper.callAfter.
"""

import subprocess
import threading

import rumps
from PyObjCTools.AppHelper import callAfter

from flowclone import config, inject
from flowclone.config import CONFIG_PATH

# menu-bar title per pipeline state
TITLES = {
    "loading": "⏳",
    "idle": "🎤",
    "recording": "🔴",
    "processing": "✨",
    "paused": "⏸",
}
READY_HINT = "Ready — hold Right ⌘ to dictate"


class FlowCloneApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("FlowClone", title=TITLES["loading"], quit_button="Quit FlowClone")
        self.status_item = rumps.MenuItem("Loading model…")
        self.pause_item = rumps.MenuItem("Pause dictation", callback=self.toggle_pause)
        self.menu = [
            self.status_item,
            None,
            self.pause_item,
            rumps.MenuItem("Edit config…", callback=self.edit_config),
        ]
        self.cfg = config.load()
        self.transcriber = None
        self.hud = None
        self.hotkey = None
        self.paused = False
        self._session = None
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
        self.transcriber = transcriber
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
        if self.transcriber is None or self.paused:
            return
        if self._session is not None and self._session.is_alive():
            return
        from flowclone.main import DictationSession

        self._session = DictationSession(
            self.transcriber,
            self.hud,
            self.cfg,
            on_state=lambda state: callAfter(self.set_state, state),
        )
        self._session.start()

    def on_release(self) -> None:
        if self._session is not None:
            self._session.release()

    def on_cancel(self) -> None:
        if self._session is not None:
            self._session.cancel()

    # ---- menu actions ----

    def toggle_pause(self, _) -> None:
        self.paused = not self.paused
        if self.paused:
            self.pause_item.title = "Resume dictation"
            self.title = TITLES["paused"]
            self.status_item.title = "Paused — dictation off"
        else:
            self.pause_item.title = "Pause dictation"
            self.set_state("idle")
            self.status_item.title = READY_HINT

    def edit_config(self, _) -> None:
        subprocess.run(["open", str(CONFIG_PATH)], check=False)

    # ---- helpers ----

    def set_state(self, state: str) -> None:
        if self.paused and state == "idle":
            self.title = TITLES["paused"]
            return
        self.title = TITLES.get(state, TITLES["idle"])

    def _notify(self, title: str, message: str) -> None:
        try:
            rumps.notification("FlowClone", title, message)
        except Exception:
            pass  # notifications need a bundled app; ignore when run as a script


def run() -> None:
    FlowCloneApp().run()
