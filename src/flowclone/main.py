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
from collections import deque

import numpy as np

from flowclone import cleanup, config, context, history, inject
from flowclone.audio import SAMPLE_RATE, MicRecorder
from flowclone.hotkey import ReleaseWatchdog, right_cmd_sources
from flowclone.stt import Transcriber, to_mx

CHUNK_SECONDS = 0.5
MIN_UTTERANCE_SECONDS = 0.35
# Absolute backstop: even if every release signal fails, finalize and paste
# rather than record forever.
MAX_RECORD_SECONDS = 90.0
# A press-to-release shorter than this is a tap (lock hands-free), not a hold.
# The release watchdog also waits this long, so it can't end a tap's session
# before the release handler has decided to lock it.
TAP_MAX_SECONDS = 0.35
# A locked session where speech never starts ends itself rather than sitting
# open until the 90 s cap — an accidental tap self-cancels quietly.
NO_SPEECH_STOP_SECONDS = 6.0


def paste_target_matches(expected: str | None, current: str | None) -> bool:
    """Fail closed unless the release-time app is still focused."""
    return expected is not None and expected == current


class SilenceStopper:
    """Decides when a hands-free recording has gone quiet for good.

    The speech threshold tracks the mic's ambient level: anchored to the
    quietest of the first few blocks, then re-anchored whenever sub-threshold
    noise persists — a fan spinning up mid-dictation must raise the floor, not
    postpone the stop forever. Speech must actually be heard once before
    silence can stop anything. Mid-speech dips get hysteresis (anything above
    half the speech threshold counts as still talking), but only within
    stop_after of the last clearly-voiced block, so noise sitting in that band
    can't starve the silence timer indefinitely. As a last resort, a level
    that stays flat for a whole stop_after window is machinery, not speech,
    and stops the recording no matter how loud it is.
    """

    FLOOR_BLOCKS = 5
    MIN_SPEECH_RMS = 0.008
    # Ambient floor ratchet: sustained sub-threshold noise lifts the floor a
    # few percent per 0.1 s block (fully re-anchors in a couple of seconds);
    # any quieter block snaps it straight back down.
    FLOOR_RISE = 1.03
    # Speech at 0.1 s granularity swings far more than this between blocks;
    # a window whose max/min stays under it is a fan or AC, not a voice.
    FLAT_RATIO = 1.5

    def __init__(self, stop_after: float) -> None:
        self.stop_after = stop_after
        self._floor_samples: list[float] = []
        self._floor: float | None = None
        self._started = False
        self._t0: float | None = None
        self._last_voiced = 0.0
        self._last_clear = 0.0
        self._recent: deque[tuple[float, float]] = deque()

    @property
    def speech_started(self) -> bool:
        return self._started

    def update(self, rms: float, now: float) -> bool:
        """Feed one audio block's RMS; True once the recording should stop."""
        if self._t0 is None:
            self._t0 = now
        if self._floor is None:
            self._floor_samples.append(rms)
            if len(self._floor_samples) < self.FLOOR_BLOCKS:
                return False
            # min, not median: the user often starts talking during the floor
            # window, and one quiet block is enough to anchor the ambient level.
            self._floor = min(self._floor_samples)
            return False
        thresh = max(self.MIN_SPEECH_RMS, 3.0 * self._floor)
        if rms >= thresh:
            self._started = True
            self._last_voiced = now
            self._last_clear = now
        else:
            self._floor = min(rms, self._floor * self.FLOOR_RISE)
            if (
                self._started
                and rms >= 0.5 * thresh
                and now - self._last_clear <= self.stop_after
            ):
                self._last_voiced = now
        self._recent.append((now, rms))
        while now - self._recent[0][0] > self.stop_after:
            self._recent.popleft()
        if self._started and self._is_flat(now):
            return True
        if not self._started:
            return now - self._t0 >= NO_SPEECH_STOP_SECONDS
        return now - self._last_voiced >= self.stop_after

    def _is_flat(self, now: float) -> bool:
        if now - self._recent[0][0] < 0.9 * self.stop_after:
            return False  # window doesn't span a full silence period yet
        levels = [rms for _, rms in self._recent]
        return max(levels) < self.FLAT_RATIO * max(min(levels), 1e-6)


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
        handsfree_cfg: config.HandsFreeConfig | None = None,
        on_history=None,
        on_notice=None,
    ) -> None:
        super().__init__(daemon=True)
        self.transcriber = transcriber
        self.hud = hud
        self.cleanup_cfg = cleanup_cfg or config.CleanupConfig()
        self.handsfree_cfg = handsfree_cfg or config.HandsFreeConfig()
        # Reports pipeline stage to the menu bar ("recording"/"processing"/
        # "idle"); a no-op when running headless from the terminal.
        self.on_state = on_state or (lambda _state: None)
        self.on_history = on_history or (lambda: None)
        self.on_notice = on_notice or (lambda _title, _message: None)
        self.stop_event = threading.Event()
        self.canceled = False
        self.t_release: float | None = None
        # Text before the release-time caret. None means the focused app would
        # not tell us, or focus moved before it could be sampled safely.
        self.before_caret: str | None = None
        self.ctx_source = "blind"
        self.ctx_ms = 0.0
        self.release_source = "hold"
        self.locked = False
        # The app focused when recording ends owns this dictation. Final STT
        # takes long enough for focus to change, so paste validates it again.
        self.target_app_id: str | None = None
        self.no_speech = False

    def release(self) -> None:
        if self.stop_event.is_set():
            return
        self.target_app_id = context.frontmost_app_id()
        self.t_release = time.perf_counter()
        self.stop_event.set()

    def lock(self) -> None:
        """Continue hands-free: the hold was a tap, not push-to-talk."""
        if not self.stop_event.is_set():
            self.locked = True

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
            if self.hud:
                self.hud.flash_error("⚠ microphone unavailable — check input device")
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

        # The tap's release event is not guaranteed to arrive: macOS disables
        # taps whose (GIL-starved) callback lags, and a dropped release used to
        # leave this loop running forever. The watchdog polls independent
        # sources for the physical key state instead — see hotkey.right_cmd_sources.
        watchdog = ReleaseWatchdog()
        stopper = SilenceStopper(self.handsfree_cfg.silence_stop_seconds)
        t_start = time.perf_counter()
        with self.transcriber.stream() as stream:
            while not self.stop_event.is_set():
                now = time.perf_counter()
                if (
                    not self.locked
                    and now - t_start > TAP_MAX_SECONDS
                    and watchdog.released(right_cmd_sources())
                ):
                    self.release_source = "watchdog"
                    self.release()
                    break
                if now - t_start > MAX_RECORD_SECONDS:
                    self.release_source = "cap"
                    self.release()
                    break
                block = mic.read(timeout=0.05)
                if block is None:
                    continue
                rms = float(np.sqrt(np.mean(block * block)))
                if self.hud:
                    self.hud.set_level(rms)
                quiet = stopper.update(rms, now)
                if self.locked and quiet:
                    self.no_speech = not stopper.speech_started
                    self.release_source = "no_speech" if self.no_speech else "silence"
                    self.release()
                    break
                blocks.append(block)
                pending.append(block)
                pending_len += len(block)
                if pending_len >= chunk_frames:
                    # Streaming decode slows as the utterance grows (~1s per
                    # partial at 30s+); a release that lands mid-loop must not
                    # wait behind one more partial nobody will see.
                    if self.stop_event.is_set():
                        break
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
            self._finish_ui()
            return  # the hold was a ⌘-shortcut, not dictation

        if self.no_speech:
            self._finish_ui()
            print("  (ignored: hands-free recording ended before speech started)")
            return

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
        if self.canceled:
            self._finish_ui()
            return  # Pause may have arrived while the batch pass was running.
        if cleanup.is_scratch_command(text, self.cleanup_cfg):
            self._scratch()
            return
        # Resolve context against the release-time target, after speech has
        # ended. This handles clicks or app switches during a dictation; the
        # final focus check below still protects the small gap before Cmd-V.
        if self.cleanup_cfg.context_aware and paste_target_matches(
            self.target_app_id, context.frontmost_app_id()
        ):
            t_ctx = time.perf_counter()
            self.before_caret = context.recall(self.target_app_id)
            self.ctx_source = "self"
            if self.before_caret is None:
                self.before_caret = context.read_before_caret()
                self.ctx_source = "ax" if self.before_caret is not None else "blind"
            self.ctx_ms = (time.perf_counter() - t_ctx) * 1000
        join = context.decide(self.before_caret) if self.before_caret is not None else None
        text = cleanup.clean(text, self.cleanup_cfg, join)
        batch_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        current_app_id = context.frontmost_app_id()
        outcome = "empty"
        if not text:
            paste_note = "nothing to paste"
        elif not paste_target_matches(self.target_app_id, current_app_id):
            outcome = "target_changed"
            paste_note = "NOT pasted — target changed; saved in history"
            self.on_notice(
                "Dictation saved",
                "The focused app changed during finalization. Use Paste Last Dictation.",
            )
        elif not inject.can_post_events():
            outcome = "accessibility_blocked"
            paste_note = "NOT pasted — grant Accessibility"
        elif inject.paste_text(text):
            outcome = "pasted"
            paste_note = "pasted"
            # The caret is now sitting at the end of this. It stays true until
            # the event tap sees a key or a click.
            context.remember(text, context.frontmost_app_id())
        else:
            outcome = "secure_input"
            paste_note = "NOT pasted — secure input field"
        if text:
            try:
                history.add(text, self.target_app_id, outcome)
                self.on_history()
            except OSError as exc:
                print(f"  history error: {exc}", file=sys.stderr)
        paste_ms = (time.perf_counter() - t0) * 1000
        total_ms = (time.perf_counter() - (self.t_release or t0)) * 1000

        self._finish_ui()
        print(f"» {text}")
        if self.cleanup_cfg.context_aware:
            if join is None:
                detail = "blind (no prior paste, and no caret from the app)"
            else:
                detail = (
                    f"{self.ctx_source} {self.before_caret[-32:]!r} → "
                    f"{'space + ' if join.space else 'no space, '}"
                    f"{'Capital' if join.capitalize else 'lowercase'}"
                )
            print(f"  [context {self.ctx_ms:.0f}ms: {detail}]")
        print(
            f"  [{duration:.1f}s audio · mic open {mic_open_ms:.0f}ms · "
            f"{n_partials} partials · finalize {batch_ms:.0f}ms · "
            f"{paste_note} {paste_ms:.0f}ms · release→done {total_ms:.0f}ms"
            + (
                f" · release via {self.release_source}"
                if self.release_source != "hold"
                else ""
            )
            + "]"
        )

    def _scratch(self) -> None:
        """Undo the last dictation instead of pasting the words "scratch that".

        Only acts when context still vouches for the caret: we pasted into this
        same app and no key or click has happened since. Anything less certain
        does nothing — a beeped no-op is recoverable, deleting 40 characters of
        someone else's text is not.
        """
        current_app_id = context.frontmost_app_id()
        last = context.recall(current_app_id)
        if not paste_target_matches(self.target_app_id, current_app_id):
            note = "NOT scratched — target changed"
        elif last is None:
            note = "nothing to scratch — no dictation still at the caret"
        elif not inject.can_post_events():
            note = "NOT scratched — grant Accessibility"
        elif inject.undo():
            context.invalidate()
            note = "scratched last dictation (undo)"
        else:
            note = "NOT scratched — secure input field"
        self._finish_ui()
        print(f"» (scratch that)\n  [{note}]")

    def _finish_ui(self) -> None:
        self.on_state("idle")
        if self.hud:
            self.hud.hide()


class SessionController:
    """Press/release/cancel logic shared by the menu-bar and terminal frontends.

    Holding Right ⌘ is push-to-talk. A quick tap (released within
    TAP_MAX_SECONDS) locks the recording hands-free; it ends on the next tap,
    on silence (SilenceStopper), or at the length cap.
    """

    def __init__(
        self,
        transcriber,
        hud=None,
        cleanup_cfg=None,
        handsfree_cfg=None,
        on_state=None,
        on_history=None,
        on_notice=None,
        session_factory=None,
    ) -> None:
        self.transcriber = transcriber
        self.hud = hud
        self.cleanup_cfg = cleanup_cfg
        self.handsfree_cfg = handsfree_cfg or config.HandsFreeConfig()
        self.on_state = on_state
        self.on_history = on_history
        self.on_notice = on_notice
        self.session_factory = session_factory or DictationSession
        self.session = None
        self._pressed_at = 0.0

    def on_press(self) -> None:
        if self.session is not None and self.session.is_alive():
            if self.session.locked:
                self.session.release_source = "tap"
                self.session.release()
            return  # not locked: still finalizing a previous dictation
        self._pressed_at = time.perf_counter()
        self.session = self.session_factory(
            self.transcriber,
            self.hud,
            self.cleanup_cfg,
            on_state=self.on_state,
            handsfree_cfg=self.handsfree_cfg,
            on_history=self.on_history,
            on_notice=self.on_notice,
        )
        self.session.start()

    def on_release(self) -> None:
        session = self.session
        if session is None or session.stop_event.is_set():
            return  # nothing recording, or this is the stop-tap's own release
        if (
            self.handsfree_cfg.enabled
            and time.perf_counter() - self._pressed_at < TAP_MAX_SECONDS
        ):
            session.lock()
        else:
            session.release()

    def on_cancel(self) -> None:
        if self.session is not None:
            self.session.cancel()

    def cancel_active(self) -> None:
        """Stop and discard an active recording or in-flight finalization."""
        if self.session is not None and self.session.is_alive():
            self.session.release_source = "pause"
            self.session.cancel()


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
    controller = SessionController(
        transcriber, hud, cleanup_cfg, handsfree_cfg=config.load_handsfree()
    )

    print(
        "hold RIGHT ⌘ anywhere, speak, release — or quick-tap it to go "
        "hands-free (tap again or pause to finish). Ctrl-C here to quit."
    )
    # CFRunLoopRun is a C loop Python never preempts, so a Python-level SIGINT
    # handler would only fire on the next keystroke; default disposition quits now.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        HoldToTalk(
            controller.on_press, controller.on_release, controller.on_cancel
        ).install()
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
    for text, levels in (
        ("Um, so basically", (0.02, 0.09, 0.04)),
        ("Um, so basically I want to refactor", (0.15, 0.06, 0.20)),
        (
            "Um, so basically I want to refactor the API endpoint in the repo",
            (0.03, 0.12, 0.002),
        ),
        (
            "…so basically I want to refactor the API endpoint in the repo so that "
            "Claude can parse the JSON config faster",
            (0.08, 0.25, 0.05),
        ),
    ):
        hud.updateText_(text)
        for rms in levels:
            hud.setLevelRms_(rms)
            pump(0.23)
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
