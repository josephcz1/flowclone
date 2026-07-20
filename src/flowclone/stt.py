"""parakeet-mlx wrapper: preload once, warm up, stream partials, batch-finalize."""

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from parakeet_mlx import from_pretrained
from parakeet_mlx.audio import get_logmel

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"
SAMPLE_RATE = 16000
STREAM_CONTEXT = (256, 256)  # (left, right) encoder frames; picked by the M1 benchmark
QUANT_GROUP_SIZE = 64
QUANT_BITS = {"8bit": 8, "4bit": 4}


def _quantizable(path: str, module) -> bool:
    """Which modules nn.quantize may touch.

    Everything with an mlx `to_quantized` (Linear/Embedding) EXCEPT the conformer
    self-attention: parakeet's streaming path swaps each layer's `self_attn` for a
    fresh, unquantized attention module and copies the weights across, which fails
    on quantized tensors (they carry extra scales/biases). Leaving self_attn in
    bf16 costs a little memory but keeps the live-preview stream working.
    """
    return hasattr(module, "to_quantized") and "self_attn" not in path


def to_mx(chunk: np.ndarray) -> mx.array:
    """float32 numpy audio -> float32 mx array.

    Must stay float32: parakeet's get_logmel views the FFT's complex64 output
    as pairs of the input dtype, which only works for 4-byte floats (its own
    load_audio always returns float32 too, ignoring its dtype parameter).
    """
    return mx.array(chunk)


class Transcriber:
    def __init__(self, quantization: str = "8bit") -> None:
        self.model = from_pretrained(MODEL_ID)
        # 8-bit weights transcribe identically to bf16 on our benchmark at ~39%
        # less RAM (4-bit ~60%); quantizing the loaded v2 weights in-memory keeps
        # us on the exact model we validated — no separate download.
        bits = QUANT_BITS.get(quantization)
        if bits is not None:
            nn.quantize(
                self.model,
                group_size=QUANT_GROUP_SIZE,
                bits=bits,
                class_predicate=_quantizable,
            )

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

    def release_cache(self) -> None:
        """Free MLX's transient buffer pool between dictations.

        A single transcription grows MLX's reusable-buffer cache to ~1.1 GB and
        the allocator keeps it resident forever, so an idle daemon holds ~2.3 GB
        instead of the ~1.3 GB of actual model weights. Dropping the pool costs
        the next dictation nothing measurable (compiled Metal kernels survive;
        only raw buffers are reallocated), so we clear it once each dictation
        ends — off the latency-critical path, after the paste has landed.
        """
        mx.clear_cache()
