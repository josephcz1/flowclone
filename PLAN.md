# FlowClone — local Wispr Flow for macOS

## Goal

Wispr Flow's dictation experience — hold a key anywhere, speak in sporadic/interrupted thoughts full of filler words, release, and clean polished text appears at the cursor — but **fast, accurate, and 100% local/free**. Primary use case: prompting Claude and other AIs without lag killing enthusiasm.

Hardware makes this very feasible: **M4 Pro, 24 GB RAM, macOS 15.1**, with `uv`, Homebrew, ffmpeg, and Ollama (llama3.2:3b already pulled) installed. Benchmarks show Parakeet TDT 0.6B transcribes ~1 min of audio in ~0.5 s on M4-class chips — so end-to-end "release key → text appears" of **0.3–0.6 s** (instant mode) and **~1–1.5 s** (AI-polished mode) is realistic. Wispr Flow itself runs ~0.5–1 s.

## Decision: build a lean custom tool, not fork Epicenter

- **Epicenter/Whispering** (suggested starting point): good app, but it's an AGPL Tauri + Svelte + Bun cross-platform monorepo. Customizing the one thing that matters most here (the cleanup pipeline for personal speech patterns) means working through a large codebase. Use it as a **reference implementation**, not a base.
- **Handy** (github.com/cjpais/Handy, MIT): closest existing "free local Wispr Flow" — worth installing as a 15-min baseline/fallback, but it lacks the filler-word/false-start LLM cleanup that is need #1.
- **Custom (~600 lines of Python)**: exactly the needed pipeline, tunable to one voice and vocab, no bloat. Python-first because on M4 Pro the models are the bottleneck, not the glue language; port to Swift later only if desired.

## Architecture

Menu-bar daemon with this pipeline:

```
hold hotkey (default: Right ⌘, configurable; hold-to-talk or toggle)
  → mic capture (sounddevice, 16 kHz mono, starts instantly — model preloaded
      at launch + one warmup inference to absorb the ~1.8 s Metal compile)
  → WHILE HELD (live preview): ~0.5 s audio chunks feed parakeet-mlx
      transcribe_stream; the raw partial transcript renders live in a floating
      HUD pill (bottom-center, non-activating — focus stays in the target
      field), so you watch yourself "type" with your voice as you speak
  → release → finalize STT: batch re-transcription of the full utterance
      (DECIDED by M1 data: 0.26 s for a 22 s clip on this machine — negligible
      cost, and batch clearly beats streaming text quality; streaming context
      (256,256) drives the HUD preview only) — model:
      mlx-community/parakeet-tdt-0.6b-v2, punctuation+caps included
  → cleanup (0 ms, deterministic): filler strip (um/uh/like/you know…),
      stutter dedupe ("the the"), personal dictionary ("claude", "wispr", …)
  → polish (optional, local LLM): Ollama llama3.2:3b kept warm (keep_alive=-1),
      strict "edit, don't answer" rewrite prompt — fixes false starts and
      self-corrections ("do X— no wait, Y" → "do Y"). ~0.4–1.0 s.
      Modes: auto (skip for short utterances) | always | off. 2.5 s timeout →
      falls back to regex-only result, so polish can never cause an indefinite wait.
  → inject: save clipboard → set transcript → synthetic ⌘V (CGEvent) → restore clipboard
  → feedback: menu bar state (idle/rec/processing) + subtle start/stop sounds
```

Project layout:

```
pyproject.toml            # uv project, Python pinned to 3.12 (MLX has no 3.14 wheels)
config.toml               # hotkey, polish mode, personal dictionary (kept in-project)
src/flowclone/
  main.py     # daemon entry, wires pipeline, per-stage latency logging
  hotkey.py   # Quartz event tap hold-to-talk state machine (pyobjc)
  audio.py    # mic ring-buffer capture
  stt.py      # parakeet-mlx wrapper, model preloaded
  cleanup.py  # filler regex, stutter dedupe, personal dictionary
  polish.py   # ollama client, strict prompt, warmup, timeout fallback
  inject.py   # pasteboard save/set/restore + ⌘V; secure-input detection
  hud.py      # live-preview floating panel + rumps menu bar, sounds, notifications
  config.py   # loads/validates config.toml
tests/        # cleanup/polish unit tests with sloppy-speech fixtures
samples/      # recorded clips of real rambling dictation for benchmarks
```

## Milestones (each independently testable)

1. **Feasibility spike** — `uv init` (pin 3.12), add `parakeet-mlx`, download model, generate an ~18 s test clip, benchmark **both modes**: batch RTF, and streaming (per-chunk latency must beat real time; compare streaming final text vs batch text). *Accept: batch <0.7 s for a ~15 s clip; streaming keeps up with the mic; tech vocab ("Claude", "API", "repo") comes out right. Output decides the commit strategy (streaming-final vs batch re-pass). If parakeet fails → pivot to mlx-whisper large-v3-turbo.*
2. **Hold-to-talk core with live partials** — hotkey + audio + streaming STT wired; partial transcript updates live in the terminal while the key is held; final on release. Grant Terminal mic + Input Monitoring/Accessibility. *Accept: partials appear within ~1 s of speaking and update steadily; final transcript <1 s after release.*
3. **Text injection** — clipboard paste with restore; works in Claude Code, browser, TextEdit; skips secure input fields (password boxes) with a warning beep.
4. **Live-preview HUD** — floating translucent pill (pyobjc `NSPanel`, non-activating, always-on-top, bottom-center) showing the live partial transcript + recording dot while the key is held; vanishes on commit. Raw partials are shown; cleanup/polish apply to the committed text (same tradeoff Wispr Flow makes). *Accept: focus never leaves the target app; no flicker; updates ≤0.7 s apart.*
5. **Cleanup engine** — record 5 real sloppy dictations as fixtures; tune filler list, stutter dedupe, personal dictionary; unit tests. *This is where personal speech patterns get encoded.*
6. **AI polish** — Ollama integration, warm model at daemon start; strict rewrite prompt with few-shot examples (critical failure mode to test: dictating a *question* must not get *answered*). Compare llama3.2:3b vs qwen3:4b on the fixtures. Measure added latency; tune `auto` threshold.
7. **Make it an app** — rumps menu bar with status states, start/stop sounds, error notifications, `launchd` LaunchAgent for login start, README documenting the permission grants.
8. **Stretch (post-MVP, pick by taste)** — waveform animation in the HUD; "scratch that" command; per-app polish prompts (formal in docs, terse in Claude Code) via frontmost bundle id; transcription history in the menu.

**Optional milestone 0** (15 min, anytime): install Handy to calibrate what local latency feels like and have a fallback while building.

## Milestone 1 results (2026-07-18, M4 Pro) — PASSED

Synthetic 21.8 s clip (`samples/spike16k.wav`, macOS `say` reading `samples/spike_text.txt`); scripts in `scripts/`:

- Model load 0.63 s; first inference 1.82 s (Metal compile — daemon does a warmup pass at startup); **batch warm 0.26 s = 84× realtime**.
- Streaming @0.5 s chunks, context (256,256): mean 207 ms / max 328 ms per chunk → keeps up with the mic; first partial on screen at ~0.5 s. Context (128,64) is 2× faster but text quality collapses (lost punctuation, "parakeet"→"parkeet") — rejected.
- Batch transcript near-perfect incl. punctuation, caps, fillers preserved; only miss: "Claude" → "cloud" (personal-dictionary case, Milestone 5).
- **Commit strategy decided: stream (256,256) for the live HUD; batch re-pass on release for the committed text.** Estimated commit latency without LLM polish: ~0.35 s (0.26 STT + paste).

## Risks & mitigations

- **Python 3.14 is the system default; MLX needs ≤3.13** → uv pins 3.12 inside the project, no system change.
- **Fn/globe key** (Wispr Flow's default) needs a flagsChanged event tap + setting system "Press 🌐 key" to Do Nothing → ship with Right ⌘ default, Fn as config option.
- **3B model occasionally rewrites meaning** → strict prompt + fixture tests; worst case `polish: off` still beats raw dictation (parakeet punctuates; regex strips fillers).
- **Permissions attach to the running binary** → during dev grant Terminal; the LaunchAgent runs the venv python and needs its own grant (documented).
- **Ollama not running** → detect, notify once, regex-only fallback.
- **parakeet-mlx needs float32 audio** — `get_logmel` views complex64 FFT output as input-dtype pairs, so bf16 input crashes with a matmul shape error (its `load_audio` ignores its dtype param and always yields float32). Feed float32 everywhere.
- **Streaming accuracy can trail batch** (limited context) → Milestone 1 measures the gap; if meaningful, the commit path re-transcribes the full buffer in batch (~0.2–0.5 s, still within budget) while the HUD keeps showing the streaming text.
- **HUD must never steal focus** → non-activating `NSPanel`; AppKit only touched from the main run loop (worker threads marshal UI updates over).
- **Project-boundary guard plugin** → all writes stay inside the project (config in-project; model downloads happen via the libraries' own cache at runtime, not via agent file writes).

## Verification

- Per-stage latency logged on every dictation; targets: instant mode ≤0.7 s, polish ≤1.8 s after key release for a 15 s utterance.
- Live preview: first partial visible ≤1.5 s after speech starts; updates ≤0.7 s apart while held.
- `uv run pytest` for cleanup/polish tests on real recorded fixtures.
- End-to-end: dictate a rambling 20 s prompt into Claude Code → clean text, fillers gone, meaning intact.
- Local-only proof: works with Wi-Fi off (after models downloaded).

## Status (2026-07-18) — v1 SHIPPED

- **M1–M4 done and user-verified**: hold Right ⌘ anywhere → live-preview pill streams partials → release → batch-accurate text pastes at the cursor in ~0.3–0.6 s. Runs in Terminal.app with mic / Input Monitoring / Accessibility granted there.
- **Final QA passed**: fixed a real tail-clipping bug (mic blocks from the last ~150 ms before key-release were dropped — now drained after stream stop); `scripts/qa_check.py` all green (imports, Quartz constants, clipboard round-trip, mic, stream→batch STT); `ruff check` clean; selftest green.
- **Quick launch**: double-click `FlowClone.command` (runs under Terminal.app so permissions carry over), or shell alias via `uv run --project`.
- **M5–M7 deferred by user choice**: cleanup engine (filler strip + personal dictionary — "Claude" still transcribes as "cloud"), Ollama polish pass, menu-bar app/launchd. Pick up here if resumed.

## References

- [Epicenter/Whispering](https://github.com/EpicenterHQ/epicenter) — reference for paste delivery + transformations UX
- [parakeet-mlx](https://github.com/senstella/parakeet-mlx) — STT engine
- [Handy](https://github.com/cjpais/Handy) — baseline app / fallback
- [VoiceInk](https://github.com/Beingpax/VoiceInk) — native-Swift reference for an eventual port
