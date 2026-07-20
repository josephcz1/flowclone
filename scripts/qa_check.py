"""Final QA: imports, API constants, and full round-trips of every subsystem.

Usage: uv run python scripts/qa_check.py
Exits non-zero if any check fails.
"""

import sys
import time

import numpy as np


def check(name, fn) -> bool:
    t0 = time.perf_counter()
    try:
        result = fn()
        extra = f" — {result}" if isinstance(result, str) else ""
        print(f"  ✓ {name}{extra} ({(time.perf_counter() - t0) * 1000:.0f}ms)")
        return True
    except Exception as exc:
        print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
        return False


def imports():
    import flowclone.audio  # noqa: F401
    import flowclone.cleanup  # noqa: F401
    import flowclone.config  # noqa: F401
    import flowclone.hotkey  # noqa: F401
    import flowclone.hud  # noqa: F401
    import flowclone.inject  # noqa: F401
    import flowclone.main  # noqa: F401
    import flowclone.menubar  # noqa: F401
    import flowclone.stt  # noqa: F401

    return "all nine modules"


def cleanup_pipeline():
    from flowclone.cleanup import clean
    from flowclone.config import load

    cfg = load()
    out = clean("Um, so I I asked cloud to help.", cfg)
    assert out == "So I asked Claude to help.", repr(out)
    return f"config.toml loaded ({len(cfg.dictionary)} dict entries) · clean() ok"


def quartz_constants():
    import Quartz

    names = (
        "kCGEventFlagsChanged",
        "kCGEventKeyDown",
        "kCGEventTapDisabledByTimeout",
        "kCGEventTapDisabledByUserInput",
        "kCGKeyboardEventKeycode",
        "kCGEventSourceUserData",
        "kCGEventFlagMaskCommand",
        "kCGSessionEventTap",
        "kCGHeadInsertEventTap",
        "kCGEventTapOptionListenOnly",
        "kCGHIDEventTap",
    )
    for name in names:
        getattr(Quartz, name)
    return f"{len(names)} constants present"


def stt_roundtrip():
    """Mirror the real session order: stream partials, exit, then batch."""
    from flowclone.stt import Transcriber, to_mx

    t = Transcriber()
    t.warmup()
    tone = (0.05 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(
        np.float32
    )
    with t.stream() as s:
        for i in range(0, 16000, 8000):
            s.add_audio(to_mx(tone[i : i + 8000]))
        _ = s.result.text
    text = t.batch_text(to_mx(tone))
    return f"stream→exit→batch ok (tone transcribes to {text.strip()!r})"


def clipboard():
    from flowclone import inject

    assert inject.clipboard_roundtrip_test(), "round-trip failed"
    return (
        f"roundtrip ok · secure_input={inject.secure_input_active()} · "
        f"can_post={inject.can_post_events()}"
    )


def mic_open_close():
    from flowclone.audio import MicRecorder

    m = MicRecorder()
    m.start()
    time.sleep(0.3)
    got = 0
    while True:
        b = m.read(timeout=0.01)
        if b is None:
            break
        got += len(b)
    m.stop()
    return f"captured {got} frames in 0.3s"


def main() -> int:
    checks = [
        ("module imports", imports),
        ("cleanup pipeline (M5)", cleanup_pipeline),
        ("Quartz constants used by hotkey/inject", quartz_constants),
        ("clipboard machinery", clipboard),
        ("mic open/read/close", mic_open_close),
        ("stt stream→batch round-trip", stt_roundtrip),
    ]
    print("QA checks:")
    failed = sum(not check(name, fn) for name, fn in checks)
    print("QA: ALL PASSED" if failed == 0 else f"QA: {failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
