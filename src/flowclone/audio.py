"""Microphone capture: 16 kHz mono float32 blocks on a queue.

The stream opens on start() and closes on stop() (on-demand, so the macOS
orange mic indicator only shows while the hotkey is held). Open cost is logged
by the caller; if it ever proves to clip first syllables, switch to a
persistent stream that discards blocks while idle.
"""

import queue

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
BLOCK_FRAMES = 1600  # 0.1 s per callback


class MicRecorder:
    def __init__(self) -> None:
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        self._queue = queue.Queue()
        try:
            self._stream = self._open()
        except Exception:
            # PortAudio snapshots the CoreAudio device list once at init and
            # never refreshes it, so after the audio setup changes under a
            # long-running daemon (sleep/wake, headphones, a conference app's
            # virtual device) every open fails even though the mic is fine.
            # Re-initializing takes a fresh snapshot; safe here because this
            # process never holds another stream open.
            sd._terminate()
            sd._initialize()
            self._stream = self._open()

    def _open(self) -> sd.InputStream:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_FRAMES,
            callback=self._on_block,
        )
        stream.start()
        return stream

    def _on_block(self, indata, frames, time_info, status) -> None:
        self._queue.put(indata[:, 0].copy())

    def read(self, timeout: float = 0.05) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
