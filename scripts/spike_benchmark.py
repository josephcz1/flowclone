"""Milestone 1 spike: batch vs streaming latency/accuracy for parakeet-mlx.

Usage: uv run python scripts/spike_benchmark.py [samples/spike16k.wav]
"""

import sys
import time
import wave

import mlx.core as mx
from parakeet_mlx import from_pretrained
from parakeet_mlx.audio import load_audio

MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
SAMPLE_RATE = 16000
CHUNK_SECONDS = 0.5  # cadence at which the mic callback would feed the stream
STREAM_CONFIGS = [(256, 256), (128, 64)]  # (left, right) encoder-frame context


def clip_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def bench_streaming(model, audio: mx.array, context_size: tuple[int, int]) -> None:
    chunk = int(CHUNK_SECONDS * SAMPLE_RATE)
    latencies: list[float] = []
    partials: list[str] = []
    t_total = time.perf_counter()
    with model.transcribe_stream(context_size=context_size) as stream:
        for start in range(0, audio.shape[0], chunk):
            t0 = time.perf_counter()
            stream.add_audio(audio[start : start + chunk])
            latencies.append(time.perf_counter() - t0)
            partials.append(stream.result.text)
        final_text = stream.result.text
    total = time.perf_counter() - t_total

    ms = sorted(x * 1000 for x in latencies)
    mean = sum(ms) / len(ms)
    p95 = ms[int(0.95 * (len(ms) - 1))]
    print(f"\n--- streaming context_size={context_size} ---")
    print(f"{len(ms)} chunks of {CHUNK_SECONDS}s; total compute {total:.2f}s")
    print(f"per-chunk latency: mean {mean:.0f}ms / p95 {p95:.0f}ms / max {ms[-1]:.0f}ms")
    print(f"keeps up with real time (max < {CHUNK_SECONDS * 1000:.0f}ms): {ms[-1] < CHUNK_SECONDS * 1000}")
    first_words = next((i for i, p in enumerate(partials) if p.strip()), None)
    if first_words is not None:
        print(f"first non-empty partial at t={CHUNK_SECONDS * (first_words + 1):.1f}s of audio")
    for i in (1, 4, 8):
        if i < len(partials):
            print(f"  partial@{CHUNK_SECONDS * (i + 1):.1f}s: {partials[i].strip()!r}")
    print(f"final text: {final_text.strip()!r}")


def main() -> None:
    clip = sys.argv[1] if len(sys.argv) > 1 else "samples/spike16k.wav"
    duration = clip_duration(clip)
    print(f"clip: {clip} ({duration:.1f}s)")

    t0 = time.perf_counter()
    model = from_pretrained(MODEL)
    print(f"model load: {time.perf_counter() - t0:.2f}s")

    t0 = time.perf_counter()
    model.transcribe(clip)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    warm_result = model.transcribe(clip)
    warm = time.perf_counter() - t0

    print("\n--- batch ---")
    print(f"cold (incl. Metal compile): {cold:.2f}s")
    print(f"warm: {warm:.2f}s  (RTF {duration / warm:.0f}x realtime)")
    print(f"batch text: {warm_result.text.strip()!r}")

    audio = load_audio(clip, SAMPLE_RATE, mx.bfloat16)
    for ctx in STREAM_CONFIGS:
        bench_streaming(model, audio, ctx)


if __name__ == "__main__":
    main()
