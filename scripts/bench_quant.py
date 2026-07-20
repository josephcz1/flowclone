"""Compare bf16 vs 8-bit vs 4-bit quantization of parakeet-tdt-0.6b-v2 on a clip.

Isolates the quantization effect: every variant starts from the SAME v2 weights
(quantized in-memory with mlx.nn.quantize), so any accuracy/latency/memory delta
is quantization alone, not a different model release.

Each variant runs in its own subprocess so the resident-weight memory reading is
clean (two models never coexist in one process).

Usage: uv run python scripts/bench_quant.py [samples/spike16k.wav]
"""

import json
import re
import subprocess
import sys
import time

import mlx.core as mx
import mlx.nn as nn
from parakeet_mlx import from_pretrained
from parakeet_mlx.audio import load_audio

from flowclone.stt import STREAM_CONTEXT, _quantizable

MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
REF_PATH = "samples/spike_text.txt"
GROUP_SIZE = 64
MODES = ["bf16", "8bit", "4bit"]


def _normalize(text: str) -> list[str]:
    """Lowercase, drop punctuation — WER is measured on content words only."""
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def word_error_rate(ref: str, hyp: str) -> float:
    r, h = _normalize(ref), _normalize(hyp)
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
    return d[len(r)][len(h)] / max(1, len(r))


def run_variant(mode: str, clip: str) -> dict:
    bits = {"8bit": 8, "4bit": 4}.get(mode)
    t0 = time.perf_counter()
    model = from_pretrained(MODEL)
    if bits is not None:
        # Same predicate the daemon uses: quantize all but self_attn, which the
        # streaming path swaps and would break on quantized weights.
        nn.quantize(model, group_size=GROUP_SIZE, bits=bits, class_predicate=_quantizable)
    mx.eval(model.parameters())
    load_ms = (time.perf_counter() - t0) * 1000

    # Exercise the STREAMING path too — the live HUD uses it, and full-model
    # quantization silently breaks it while leaving the batch path fine.
    stream_ok = True
    try:
        audio = load_audio(clip, 16000, mx.float32)
        with model.transcribe_stream(context_size=STREAM_CONTEXT) as s:
            for i in range(0, audio.shape[0], 8000):
                s.add_audio(audio[i : i + 8000])
            _ = s.result.text
    except Exception as exc:  # noqa: BLE001
        stream_ok = False
        print(f"stream FAILED [{mode}]: {type(exc).__name__}: {exc}", file=sys.stderr)

    # Cold pass absorbs Metal kernel compilation; warm is the real per-use cost.
    t0 = time.perf_counter()
    model.transcribe(clip)
    cold_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    text = model.transcribe(clip).text.strip()
    warm_ms = (time.perf_counter() - t0) * 1000

    # Resident weights = live-array memory once the transient pool is dropped.
    mx.clear_cache()
    weights_mb = mx.get_active_memory() / 1e6

    ref = open(REF_PATH).read().strip()
    return {
        "mode": mode,
        "load_ms": round(load_ms),
        "cold_ms": round(cold_ms),
        "warm_ms": round(warm_ms),
        "weights_mb": round(weights_mb),
        "wer": round(word_error_rate(ref, text), 3),
        "stream_ok": stream_ok,
        "text": text,
    }


def main() -> None:
    clip = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "samples/spike16k.wav"

    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]
        print(json.dumps(run_variant(mode, clip)))
        return

    results = []
    for mode in MODES:
        out = subprocess.run(
            [sys.executable, __file__, clip, "--mode", mode],
            capture_output=True,
            text=True,
        )
        line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
        if not line:
            print(f"variant {mode} failed:\n{out.stderr[-500:]}", file=sys.stderr)
            continue
        results.append(json.loads(line[-1]))

    base = next((r for r in results if r["mode"] == "bf16"), None)
    print(f"\nclip: {clip}")
    print(f"{'variant':<8}{'weights':>10}{'warm':>9}{'cold':>9}{'load':>9}{'WER':>8}{'ΔWER':>8}{'stream':>8}")
    for r in results:
        dwer = f"{r['wer'] - base['wer']:+.3f}" if base else "—"
        save = f" (-{100 * (1 - r['weights_mb'] / base['weights_mb']):.0f}%)" if base and r["mode"] != "bf16" else ""
        stream = "ok" if r["stream_ok"] else "BROKEN"
        print(
            f"{r['mode']:<8}{r['weights_mb']:>7}MB{save:<8}{r['warm_ms']:>6}ms"
            f"{r['cold_ms']:>7}ms{r['load_ms']:>7}ms{r['wer']:>8.3f}{dwer:>8}{stream:>8}"
        )
    print("\ntranscripts:")
    for r in results:
        print(f"  [{r['mode']}] {r['text']}")


if __name__ == "__main__":
    main()
