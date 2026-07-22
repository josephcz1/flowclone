# FlowClone

A local, free, low-latency Wispr Flow-style dictation daemon for macOS. Hold
**Right ⌘** anywhere, speak, release — clean text appears at your cursor. STT is
[parakeet-mlx](https://github.com/senstella/parakeet-mlx) (≈84× realtime on an
M4 Pro); everything runs on-device, no cloud, no subscription.

See [PLAN.md](PLAN.md) for the full design and milestone history.

## Run

```sh
uv run flowclone            # menu-bar app (default)
uv run flowclone --terminal # headless, prints transcripts + latency to the terminal
uv run flowclone --selftest # check model, mic, and permissions, then exit
```

Or double-click **FlowClone.command** (runs under Terminal.app so its permission
grants carry over).

**Menu bar:** the 🎤 icon turns 🔴 while recording and ✨ while finalizing.
*Pause dictation* disables the hotkey without quitting; *Edit config…* opens
`config.toml`.

## Permissions

Grant these to whichever app launches FlowClone (Terminal.app for the launcher):
System Settings → Privacy & Security →

- **Microphone** — to record.
- **Input Monitoring** — to detect the Right ⌘ hold.
- **Accessibility** — to paste (synthetic ⌘V) and to read the text before the
  caret for context-aware joins.

Grants attach to the launching binary, so if you later autostart via launchd
you'll re-grant them for `uv`/python once (see
[launchd/com.flowclone.agent.plist](launchd/com.flowclone.agent.plist)).

## Cleanup & personal dictionary (`config.toml`)

The committed text (not the live preview) runs through a zero-latency cleanup
pass before pasting: personal-dictionary fixes, filler-word removal, stutter
dedupe, and a context-aware join that adds the missing space between
back-to-back dictations and lowercases mid-sentence continuations. Edit
[config.toml](config.toml) and restart to tune it:

- `[dictionary]` — words parakeet mishears → what you meant
  (ships with `cloud → Claude`, `cloud code → Claude Code`).
- `[cleanup].extra_fillers` — opt in to removing softer fillers
  (`like`, `you know`, `basically`, …). The built-in set (um, uh, er, hmm…) is
  always safe to strip.
- `[cleanup].context_aware`, `add_trailing_space`, `dedupe_stutters`,
  `enabled` — toggles, each explained by its comment in the file.

## Develop

```sh
uv run --with pytest pytest   # unit tests: cleanup, context joins, hotkey tap
uv run python scripts/qa_check.py   # end-to-end subsystem checks
uvx ruff check src tests
```
