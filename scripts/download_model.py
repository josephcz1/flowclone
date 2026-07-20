"""One-time fetch of the parakeet model weights into the local HF cache."""

import time

from parakeet_mlx import from_pretrained

MODEL = "mlx-community/parakeet-tdt-0.6b-v2"

t0 = time.perf_counter()
model = from_pretrained(MODEL)
print(f"model ready in {time.perf_counter() - t0:.1f}s: {type(model).__name__}")
