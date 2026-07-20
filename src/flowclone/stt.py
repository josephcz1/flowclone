"""parakeet-mlx wrapper: preload once, warm up, stream partials, batch-finalize."""

import mlx.core as mx
import numpy as np
from parakeet_mlx import from_pretrained
from parakeet_mlx.audio import get_logmel

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"
SAMPLE_RATE = 16000
STREAM_CONTEXT = (256, 256)  # (left, right) encoder frames; picked by the M1 benchmark


def to_mx(chunk: np.ndarray) -> mx.array:
    """float32 numpy audio -> float32 mx array.

    Must stay float32: parakeet's get_logmel views the FFT's complex64 output
    as pairs of the input dtype, which only works for 4-byte floats (its own
    load_audio always returns float32 too, ignoring its dtype parameter).
    """
    return mx.array(chunk)


class Transcriber:
    def __init__(self) -> None:
        self.model = from_pretrained(MODEL_ID)

    def warmup(self) -> None:
        """Absorb Metal kernel compilation so the first real dictation is fast."""
        silence = mx.zeros(SAMPLE_RATE // 2)
        try:
            self.batch_text(silence)
        except Exception:
            pass
        try:
            with self.stream() as s:
                s.add_audio(silence)
                _ = s.result.text
        except Exception:
            pass

    def stream(self):
        """Streaming session (context manager) for live partials while recording."""
        return self.model.transcribe_stream(context_size=STREAM_CONTEXT)

    def batch_text(self, audio_mx: mx.array) -> str:
        """Full-utterance transcription — the accurate pass used for committed text."""
        mel = get_logmel(audio_mx, self.model.preprocessor_config)
        return self.model.generate(mel)[0].text
