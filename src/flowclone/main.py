"""FlowClone Milestone 2 CLI: hold Right ⌘ anywhere, speak, live partials in this
terminal, release → final text printed with per-stage latency.

Run:
    uv run flowclone --selftest   # verify model + mic + permissions, no hotkey
    uv run flowclone              # the real thing
"""

import argparse
import fcntl
import os
import shutil
import signal
import sys
import tempfile
import threading
import time

import numpy as np

from flowclone import cleanup, config, inject
from flowclone.audio import SAMPLE_RATE, MicRecorder
from flowclone.stt import Transcriber, to_mx

CHUNK_SECONDS = 0.5
MIN_UTTERANCE_SECONDS = 0.35


def _live_line(text: str) -> None:
    cols = shutil.get_terminal_size().columns
    tail = text.replace("\n", " ").strip()
    budget = max(20, cols - 8)
    if len(tail) > budget:
        tail = "…" + tail[-(budget - 1) :]
    sys.stdout.write("\r\x1b[2K  🎤 " + tail)
    sys.stdout.flush()


class DictationSession(threading.Thread):
    """One press-to-release recording: streams partials, batch-finalizes."""

    def __init__(
        self,
        transcriber: Transcriber,
        hud=None,
        cleanup_cfg: config.CleanupConfig | None = None,
        on_state=None,
    ) -> None:
        super().__init__(daemon=True)
        self.transcriber = transcriber
        self.hud = hud
        self.cleanup_cfg = cleanup_cfg or config.CleanupConfig()
        # Reports pipeline stage to the menu bar ("recording"/"processing"/
        # "idle"); a no-op when running headless from the terminal.
        self.on_state = on_state or (lambda _state: None)
        self.stop_event = threading.Event()
        self.canceled = False
        self.t_release: float | None = None

    def release(self) -> None:
        self.t_release = time.perf_counter()
        self.stop_event.set()

    def cancel(self) -> None:
        self.canceled = True
        self.stop_event.set()

    def run(self) -> None:
        t_press = time.perf_counter()
        mic = MicRecorder()
        try:
            mic.start()
        except Exception as exc:
            print(f"\n  mic error: {exc}", file=sys.stderr)
            return
        mic_open_ms = (time.perf_counter() - t_press) * 1000
        try:
            self._record_and_transcribe(mic, mic_open_ms)
        finally:
            # Reclaim MLX's ~1.1 GB buffer pool now that the paste has landed;
            # runs on every exit (finalized, canceled, or too-short).
            self.transcriber.release_cache()

    def _record_and_transcribe(self, mic, mic_open_ms: float) -> None:
        self.on_state("recording")
        if self.hud:
            self.hud.show("listening…")
        _live_line("listening…")

        blocks: list[np.ndarray] = []
        pending: list[np.ndarray] = []
        pending_len = 0
        chunk_frames = int(CHUNK_SECONDS * SAMPLE_RATE)
        n_partials = 0

        with self.transcriber.stream() as stream:
            while not self.stop_event.is_set():
                block = mic.read(timeout=0.05)
                if block is None:
                    continue
                blocks.append(block)
                pending.append(block)
                pending_len += len(block)
                if pending_len >= chunk_frames:
                    stream.add_audio(to_mx(np.concatenate(pending)))
                    pending = []
                    pending_len = 0
                    n_partials += 1
                    partial = stream.result.text
                    if self.hud:
                        self.hud.update(partial or "listening…")
                    _live_line(partial)
        mic.stop()
        # Drain the queue: the callback keeps delivering ~0.1s blocks up to the
        # moment the stream stops, and people release the key on their last
        # syllable — without this the final word gets clipped.
        while True:
            block = mic.read(timeout=0.01)
            if block is None:
                break
            blocks.append(block)

        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

        if self.canceled:
            self.on_state("idle")
            if self.hud:
                self.hud.hide()
            return  # the hold was a ⌘-shortcut, not dictation

        audio = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
        duration = len(audio) / SAMPLE_RATE
        if duration < MIN_UTTERANCE_SECONDS:
            self.on_state("idle")
            if self.hud:
                self.hud.hide()
            print(f"  (ignored: {duration * 1000:.0f}ms is too short)")
            return

        self.on_state("processing")
        if self.hud:
            self.hud.finalize()  # gray dot while the accurate batch pass runs
        t0 = time.perf_counter()
        text = self.transcriber.batch_text(to_mx(audio)).strip()
        text = cleanup.clean(text, self.cleanup_cfg)
        batch_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        if not text:
            paste_note = "nothing to paste"
        elif not inject.can_post_events():
            paste_note = "NOT pasted — grant Accessibility"
        elif inject.paste_text(text):
            paste_note = "pasted"
        else:
            paste_note = "NOT pasted — secure input field"
        paste_ms = (time.perf_counter() - t0) * 1000
        total_ms = (time.perf_counter() - (self.t_release or t0)) * 1000

        self.on_state("idle")
        if self.hud:
            self.hud.hide()
        print(f"» {text}")
        print(
            f"  [{duration:.1f}s audio · mic open {mic_open_ms:.0f}ms · "
            f"{n_partials} partials · finalize {batch_ms:.0f}ms · "
            f"{paste_note} {paste_ms:.0f}ms · release→done {total_ms:.0f}ms]"
        )


def _load_transcriber() -> Transcriber:
    quantization = config.load_model().quantization
    print(f"loading model… (quantization: {quantization})", flush=True)
    t0 = time.perf_counter()
    transcriber = Transcriber(quantization=quantization)
    transcriber.warmup()
    print(f"model ready in {time.perf_counter() - t0:.1f}s (warm)")
    return transcriber


def _acquire_single_instance_lock():
    """Exclusive flock held for the process lifetime; None if another daemon owns it.

    Stacked daemons each grab the hotkey, so one dictation would record —
    and paste — once per instance. The OS releases the lock on any exit.
    """
    path = os.path.join(tempfile.gettempdir(), f"flowclone-{os.getuid()}.lock")
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _already_running_message() -> None:
    print(
        "FlowClone is already running — another instance owns the hotkey.\n"
        "Quit it first (menu-bar icon → Quit, or Ctrl-C in its Terminal window).",
        file=sys.stderr,
    )


def run_daemon() -> int:
    """Default: the menu-bar app. rumps owns the run loop; the event tap and HUD
    attach to it. Falls back to the terminal daemon via `--terminal`."""
    from flowclone import menubar

    lock = _acquire_single_instance_lock()
    if lock is None:
        _already_running_message()
        return 1

    print("FlowClone running in the menu bar — hold RIGHT ⌘ anywhere to dictate.")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    menubar.run()
    return 0


def run_terminal_daemon() -> int:
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    from flowclone.hotkey import HoldToTalk
    from flowclone.hud import HudPanel

    lock = _acquire_single_instance_lock()
    if lock is None:
        _already_running_message()
        return 1

    cleanup_cfg = config.load()

    # A real (Dock-less) AppKit app: its run loop both pumps the event tap and
    # renders the live-preview panel.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    transcriber = _load_transcriber()
    if not inject.can_post_events():
        inject.request_post_event_access()
        print(
            "⚠ pasting needs Accessibility permission for this terminal app\n"
            "  (System Settings → Privacy & Security → Accessibility).\n"
            "  Until granted, transcripts only print here."
        )
    hud = HudPanel.alloc().init()
    state: dict = {"session": None}

    def on_press() -> None:
        session = state["session"]
        if session is not None and session.is_alive():
            return
        state["session"] = DictationSession(transcriber, hud, cleanup_cfg)
        state["session"].start()

    def on_release() -> None:
        if state["session"] is not None:
            state["session"].release()

    def on_cancel() -> None:
        if state["session"] is not None:
            state["session"].cancel()

    print("hold RIGHT ⌘ anywhere, speak, release. Ctrl-C here to quit.")
    # CFRunLoopRun is a C loop Python never preempts, so a Python-level SIGINT
    # handler would only fire on the next keystroke; default disposition quits now.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        HoldToTalk(on_press, on_release, on_cancel).install()
    except PermissionError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    app.run()
    return 0


def run_selftest() -> int:
    transcriber = _load_transcriber()

    print("keyboard tap (Input Monitoring)…", flush=True)
    import Quartz

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
        lambda proxy, type_, event, refcon: event,
        None,
    )
    if tap is None:
        print(
            "  ✗ blocked — grant Input Monitoring to this terminal app "
            "(System Settings → Privacy & Security → Input Monitoring), rerun after."
        )
    else:
        print("  ✓ ok")

    print("paste machinery (Accessibility + clipboard)…", flush=True)
    if inject.secure_input_active():
        print("  (secure input currently active — pastes would be skipped right now)")
    if inject.can_post_events():
        print("  ✓ can post ⌘V (Accessibility granted)")
    else:
        inject.request_post_event_access()
        print(
            "  ✗ cannot post ⌘V — grant Accessibility to this terminal app "
            "(System Settings → Privacy & Security → Accessibility), rerun after."
        )
    print(
        "  ✓ clipboard save/set/restore ok"
        if inject.clipboard_roundtrip_test()
        else "  ✗ clipboard round-trip FAILED"
    )

    print("microphone + transcription — SPEAK NOW (3 seconds)…", flush=True)
    mic = MicRecorder()
    t0 = time.perf_counter()
    try:
        mic.start()
    except Exception as exc:
        print(f"  ✗ could not open mic: {exc}")
        return 1
    open_ms = (time.perf_counter() - t0) * 1000
    blocks: list[np.ndarray] = []
    deadline = time.time() + 3.0
    while time.time() < deadline:
        block = mic.read(timeout=0.1)
        if block is not None:
            blocks.append(block)
    mic.stop()

    if not blocks:
        print("  ✗ no audio captured — check Microphone permission for this terminal app.")
        return 1
    audio = np.concatenate(blocks)
    peak = float(np.abs(audio).max())
    print(
        f"  mic open {open_ms:.0f}ms · captured {len(audio) / SAMPLE_RATE:.1f}s · peak {peak:.3f}"
        + ("  (pure silence — Microphone permission likely denied)" if peak < 1e-4 else "")
    )

    t0 = time.perf_counter()
    text = transcriber.batch_text(to_mx(audio)).strip()
    print(f"  transcribed in {(time.perf_counter() - t0) * 1000:.0f}ms: {text!r}")
    print("selftest done.")
    return 0


def run_hudtest() -> int:
    """Render the live-preview pill with fake partials for a few seconds."""
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from Foundation import NSDate, NSRunLoop

    from flowclone.hud import HudPanel

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    hud = HudPanel.alloc().init()

    def pump(seconds: float) -> None:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(seconds)
        )

    hud.showText_("listening…")
    pump(0.8)
    for text in (
        "Um, so basically",
        "Um, so basically I want to refactor",
        "Um, so basically I want to refactor the API endpoint in the repo",
        "…so basically I want to refactor the API endpoint in the repo so that "
        "Claude can parse the JSON config faster",
    ):
        hud.updateText_(text)
        pump(0.7)
    hud.finalizeHud_(None)
    pump(0.6)
    hud.hideHud_(None)
    pump(0.2)
    print("hudtest done — a dark pill should have appeared in the lower-right corner.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="FlowClone hold-to-talk dictation")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="check model/mic/permissions and exit",
    )
    parser.add_argument(
        "--hudtest",
        action="store_true",
        help="render the live-preview pill with fake text and exit",
    )
    parser.add_argument(
        "--terminal",
        action="store_true",
        help="run headless in the terminal instead of the menu bar",
    )
    args = parser.parse_args()
    if args.hudtest:
        raise SystemExit(run_hudtest())
    if args.selftest:
        raise SystemExit(run_selftest())
    raise SystemExit(run_terminal_daemon() if args.terminal else run_daemon())


if __name__ == "__main__":
    main()
