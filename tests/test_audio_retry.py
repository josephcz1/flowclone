"""MicRecorder.start() re-initializes PortAudio when the first open fails.

PortAudio snapshots the device list once per init; a long-running daemon whose
audio setup changed since then fails every open until re-init.
"""

from flowclone import audio


class FakeStream:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True


def test_reinit_and_retry_after_failed_open(monkeypatch):
    calls = []

    def make_stream(**_kwargs):
        calls.append("open")
        if "initialize" not in calls:
            raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")
        return FakeStream()

    monkeypatch.setattr(audio.sd, "InputStream", make_stream)
    monkeypatch.setattr(audio.sd, "_terminate", lambda: calls.append("terminate"))
    monkeypatch.setattr(audio.sd, "_initialize", lambda: calls.append("initialize"))

    rec = audio.MicRecorder()
    rec.start()
    assert calls == ["open", "terminate", "initialize", "open"]
    assert rec._stream.started


def test_no_reinit_when_first_open_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(audio.sd, "InputStream", lambda **kw: FakeStream())
    monkeypatch.setattr(audio.sd, "_terminate", lambda: calls.append("terminate"))
    monkeypatch.setattr(audio.sd, "_initialize", lambda: calls.append("initialize"))

    rec = audio.MicRecorder()
    rec.start()
    assert calls == []
    assert rec._stream.started


def test_failure_after_reinit_propagates(monkeypatch):
    def always_fail(**_kwargs):
        raise RuntimeError("PaErrorCode -9986")

    monkeypatch.setattr(audio.sd, "InputStream", always_fail)
    monkeypatch.setattr(audio.sd, "_terminate", lambda: None)
    monkeypatch.setattr(audio.sd, "_initialize", lambda: None)

    rec = audio.MicRecorder()
    try:
        rec.start()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the second failure to propagate")
