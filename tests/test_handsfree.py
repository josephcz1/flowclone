"""Hands-free mode: tap-to-lock decisions and the silence stopper, driven with
synthetic RMS streams and a fake session — no mic, no model, no key events."""

import time

import numpy as np
import pytest

from flowclone import config
from flowclone.main import (
    NO_SPEECH_STOP_SECONDS,
    TAP_MAX_SECONDS,
    DictationSession,
    SessionController,
    SilenceStopper,
    paste_target_matches,
)

AMBIENT = 0.002
SPEECH = 0.05


def feed(stopper, levels, dt=0.1, t0=0.0):
    """Run a block sequence through the stopper; return the stop time or None."""
    for i, rms in enumerate(levels):
        if stopper.update(rms, t0 + i * dt):
            return t0 + i * dt
    return None


def test_stops_after_silence_follows_speech():
    s = SilenceStopper(stop_after=1.5)
    levels = [AMBIENT] * 5 + [SPEECH] * 20 + [AMBIENT] * 30
    stopped_at = feed(s, levels)
    assert stopped_at is not None
    # last voiced block is index 24 (t=2.4); stop at >= 2.4 + 1.5
    assert 3.9 <= stopped_at < 4.2


def test_keeps_alive_through_quiet_speech_dips():
    s = SilenceStopper(stop_after=1.0)
    # dips to just above half the threshold must count as still talking
    thresh_after_floor = max(SilenceStopper.MIN_SPEECH_RMS, 3.0 * AMBIENT)
    dip = 0.6 * thresh_after_floor
    levels = [AMBIENT] * 5 + ([SPEECH] * 3 + [dip] * 8) * 5
    assert feed(s, levels) is None


def test_never_speaking_self_cancels():
    s = SilenceStopper(stop_after=1.5)
    stopped_at = feed(s, [AMBIENT] * 200)
    assert stopped_at is not None
    assert stopped_at >= NO_SPEECH_STOP_SECONDS
    assert not s.speech_started


def test_no_stop_while_still_talking():
    s = SilenceStopper(stop_after=1.5)
    assert feed(s, [AMBIENT] * 5 + [SPEECH] * 100) is None
    assert s.speech_started


class FakeSession:
    def __init__(self, *args, **kwargs):
        self.locked = False
        self.released = False
        self.canceled = False
        self.release_source = "hold"
        self._alive = False

        class _Ev:
            def __init__(self):
                self._set = False

            def is_set(self):
                return self._set

            def set(self):
                self._set = True

        self.stop_event = _Ev()

    def start(self):
        self._alive = True

    def is_alive(self):
        # Like threading.Thread: stays alive until run() finishes, which
        # includes the finalize/paste phase after stop_event is set.
        return self._alive

    def lock(self):
        if not self.stop_event.is_set():
            self.locked = True

    def release(self):
        self.released = True
        self.stop_event.set()

    def cancel(self):
        self.canceled = True
        self.stop_event.set()


def make_controller(enabled=True):
    return SessionController(
        transcriber=None,
        handsfree_cfg=config.HandsFreeConfig(enabled=enabled),
        session_factory=FakeSession,
    )


def test_quick_tap_locks_hands_free():
    c = make_controller()
    c.on_press()
    c.on_release()  # immediate release = a tap
    assert c.session.locked
    assert not c.session.released


def test_hold_releases_normally(monkeypatch):
    c = make_controller()
    c.on_press()
    c._pressed_at = time.perf_counter() - (TAP_MAX_SECONDS + 0.1)
    c.on_release()
    assert c.session.released
    assert not c.session.locked


def test_second_tap_stops_a_locked_session():
    c = make_controller()
    c.on_press()
    c.on_release()
    session = c.session
    assert session.locked
    c.on_press()  # the stop tap
    assert session.released
    assert session.release_source == "tap"
    c.on_release()  # the stop tap's release must not lock or restart anything
    assert c.session is session


def test_tap_is_a_short_hold_when_handsfree_disabled():
    c = make_controller(enabled=False)
    c.on_press()
    c.on_release()
    assert c.session.released
    assert not c.session.locked


def test_press_while_finalizing_does_nothing():
    c = make_controller()
    c.on_press()
    session = c.session
    session.release()  # finishing, but thread notionally still alive
    session._alive = True
    c.on_press()
    assert c.session is session


def test_pause_cancels_active_session():
    c = make_controller()
    c.on_press()
    c.cancel_active()
    assert c.session.canceled
    assert c.session.stop_event.is_set()
    assert c.session.release_source == "pause"


def test_release_locks_the_focused_app(monkeypatch):
    monkeypatch.setattr(
        "flowclone.main.context.frontmost_app_id", lambda: "com.example.target"
    )
    session = DictationSession(transcriber=None)
    session.release()
    assert session.target_app_id == "com.example.target"


def test_paste_target_validation_fails_closed():
    assert paste_target_matches("com.example.target", "com.example.target")
    assert not paste_target_matches("com.example.target", "com.example.other")
    assert not paste_target_matches(None, "com.example.target")
    assert not paste_target_matches("com.example.target", None)


def test_no_speech_session_never_calls_batch_stt(monkeypatch):
    from flowclone import main

    class FakeMic:
        def __init__(self):
            self.blocks = [np.full(1600, AMBIENT, dtype=np.float32) for _ in range(8)]
            self.stopped = False

        def start(self):
            pass

        def read(self, timeout=0.05):
            return self.blocks.pop(0) if self.blocks else None

        def stop(self):
            self.stopped = True

    class FakeStream:
        result = type("Result", (), {"text": ""})()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def add_audio(self, _audio):
            pass

    class FakeTranscriber:
        def __init__(self):
            self.batch_calls = 0

        def stream(self):
            return FakeStream()

        def batch_text(self, _audio):
            self.batch_calls += 1
            return "hallucinated text"

        def release_cache(self):
            pass

    mic = FakeMic()
    monkeypatch.setattr(main, "MicRecorder", lambda: mic)
    monkeypatch.setattr(main, "NO_SPEECH_STOP_SECONDS", 0.0)
    monkeypatch.setattr(main, "right_cmd_sources", lambda: (False, False, None))
    monkeypatch.setattr(main.context, "frontmost_app_id", lambda: "com.example.app")
    transcriber = FakeTranscriber()
    session = DictationSession(transcriber)
    session.locked = True
    session.run()

    assert session.no_speech
    assert transcriber.batch_calls == 0
    assert mic.stopped


def test_focus_change_saves_history_without_pasting(monkeypatch):
    from flowclone import main

    class FakeMic:
        def __init__(self):
            self.blocks = [np.full(1600, SPEECH, dtype=np.float32) for _ in range(5)]

        def read(self, timeout=0.05):
            return self.blocks.pop(0) if self.blocks else None

        def stop(self):
            pass

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    class FakeTranscriber:
        def stream(self):
            return FakeStream()

        def batch_text(self, _audio):
            return "Recovered transcript."

    saved = []
    notices = []
    monkeypatch.setattr(main, "to_mx", lambda audio: audio)
    monkeypatch.setattr(main.context, "frontmost_app_id", lambda: "app.other")
    monkeypatch.setattr(
        main.inject,
        "paste_text",
        lambda _text: pytest.fail("focus mismatch must not paste"),
    )
    monkeypatch.setattr(
        main.history,
        "add",
        lambda text, app, outcome: saved.append((text, app, outcome)),
    )
    session = DictationSession(
        FakeTranscriber(),
        on_notice=lambda title, message: notices.append((title, message)),
    )
    session.target_app_id = "app.target"
    session.stop_event.set()
    session._record_and_transcribe(FakeMic(), 0.0)

    assert saved == [("Recovered transcript.", "app.target", "target_changed")]
    assert notices and notices[0][0] == "Dictation saved"


def test_handsfree_config_defaults_and_clamping(tmp_path):
    assert config.load_handsfree(tmp_path / "missing.toml") == config.HandsFreeConfig()
    p = tmp_path / "config.toml"
    p.write_text("[handsfree]\nenabled = false\nsilence_stop_seconds = 99\n")
    cfg = config.load_handsfree(p)
    assert cfg.enabled is False
    assert cfg.silence_stop_seconds == 10.0
