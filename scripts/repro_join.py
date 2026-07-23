"""Reproduce the reported "sentence.And" symptom in a single dictation.

The cross-dictation diagnosis assumes parakeet's own batch output is correctly
spaced and that the missing space appears only where two separate pastes meet.
That was never tested. This synthesizes two sentences spoken back to back in one
utterance and prints what actually comes out of the model, before and after
cleanup.
"""

import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowclone import cleanup, config  # noqa: E402
from flowclone.stt import SAMPLE_RATE, Transcriber, to_mx  # noqa: E402

PHRASE = "This is my first sentence. And this is my second sentence."
AIFF = Path(__file__).resolve().parents[1] / "_repro.wav"


def synth() -> np.ndarray:
    # AIFF is big-endian only; float samples need a WAVE container.
    subprocess.run(
        [
            "say", "-r", "180",
            "--file-format=WAVE", "--data-format=LEF32@16000",
            "-o", str(AIFF), PHRASE,
        ],
        check=True,
    )
    import soundfile as sf

    audio, sr = sf.read(str(AIFF), dtype="float32")
    assert sr == SAMPLE_RATE, sr
    return audio if audio.ndim == 1 else audio[:, 0]


def main() -> None:
    audio = synth()
    print(f"spoke  : {PHRASE!r}  ({len(audio) / SAMPLE_RATE:.1f}s)")
    print("loading model…", flush=True)
    raw = Transcriber().batch_text(to_mx(audio)).strip()
    print(f"parakeet: {raw!r}")
    print(f"cleaned : {cleanup.clean(raw, config.load(), None)!r}")
    AIFF.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
