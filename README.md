# FlowClone

A local, free, low-latency Wispr Flow-style dictation daemon for macOS. Hold
**Right ⌘** anywhere, speak, release — clean text appears at your cursor. Or
**quick-tap** Right ⌘ to go hands-free: speak as long as you like, and it
finalizes when you tap again or simply stop talking. STT is
[parakeet-mlx](https://github.com/senstella/parakeet-mlx) (≈84× realtime on an
M4 Pro); everything runs on-device, no cloud, no subscription.

See [PLAN.md](PLAN.md) for the full design and milestone history.

## Run

The easy way — build a real app, no terminal window:

```sh
uv sync
uv run python scripts/build_app.py --install   # → /Applications/FlowClone.app
```

Double-click **FlowClone.app**, then use **Launch at Login** in its menu to
start it automatically. It's a thin wrapper around this checkout, so permission grants
show up as "FlowClone" and `git pull` updates take effect on next launch —
rebuild only if you move the repo. Logs go to `~/Library/Logs/FlowClone.log`.

For development, run it from the checkout instead:

```sh
uv run flowclone            # menu-bar app (default)
uv run flowclone --terminal # headless, prints transcripts + latency to the terminal
uv run flowclone --selftest # check model, mic, and permissions, then exit
```

Or double-click **FlowClone.command** (runs under Terminal.app so its permission
grants carry over).

**Menu bar:** a monochrome mic outline (SF Symbols, matches light/dark menu
bars) — filled while recording, a waveform while finalizing, slashed while
paused.
*Pause dictation* immediately stops any active recording and disables the
hotkey without quitting. *Paste Last Dictation*, *Copy Last Dictation*, and the
recent-history submenu recover text when a paste is blocked or focus changes;
history stores only the latest 20 cleaned transcripts, never audio.
*Preferences…* opens the native settings window; cleanup, dictionary, and
hands-free changes apply immediately, while model precision applies after a
relaunch.

## Permissions

Grant these to whichever app launches FlowClone — **FlowClone** itself for the
app bundle, Terminal.app for the `.command` launcher: System Settings →
Privacy & Security →

- **Microphone** — to record.
- **Input Monitoring** — to detect the Right ⌘ hold.
- **Accessibility** — to paste (synthetic ⌘V) and to read the text before the
  caret for context-aware joins.

The **Launch at Login** menu toggle reopens the same app bundle, so it keeps the
same permission identity as a normal double-click launch.

## Cleanup & personal dictionary

The committed text (not the live preview) runs through a zero-latency cleanup
pass before pasting: personal-dictionary fixes, filler-word removal, stutter
dedupe, and a context-aware join that adds the missing space between
back-to-back dictations and lowercases mid-sentence continuations. Edit
Use **Preferences…** in the menu bar to tune it. Settings remain stored locally
in [config.toml](config.toml) so they are inspectable and portable:

- `[dictionary]` — words parakeet mishears → what you meant
  (ships with `cloud → Claude`, `cloud code → Claude Code`).
- `[cleanup].extra_fillers` — opt in to removing softer fillers
  (`like`, `you know`, `basically`, …). The built-in set (um, uh, er, hmm…) is
  always safe to strip.
- `[handsfree]` — quick-tap Right ⌘ to lock the mic on instead of holding;
  stop with another tap or by staying silent for `silence_stop_seconds`
  (speech detection adapts to your mic's noise floor; a tap where you never
  speak self-cancels after a few seconds).
- `[cleanup].scratch_that` — dictate just **"scratch that"** (or "delete
  that") to delete the previous dictation instead of pasting, as long as
  nothing was typed or clicked since it landed. Anything longer ("scratch that
  idea") pastes normally.
- `[cleanup].context_aware`, `add_trailing_space`, `dedupe_stutters`,
  `enabled` — toggles, each explained by its comment in the file.

## Develop

```sh
uv run --with pytest pytest   # unit tests: cleanup, context joins, hotkey tap
uv run python scripts/qa_check.py   # end-to-end subsystem checks
uvx ruff check src tests
```
