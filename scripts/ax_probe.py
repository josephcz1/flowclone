"""Diagnostic: can we read the text before the caret in the focused field?

The context-aware spacing/capitalization design hinges on one question that only
a real machine can answer: does the macOS Accessibility API expose the caret and
surrounding text in the apps Joseph actually dictates into? Terminals, Electron
and canvas editors are the known-doubtful cases.

    uv run --with pyobjc-framework-applicationservices \
        python scripts/ax_probe.py

Needs Accessibility permission for the *hosting terminal app* (System Settings →
Privacy & Security → Accessibility) — the same grant the daemon already needs to
post ⌘V.

Three strategies are tried per sample, because no single one covers the field:

  1. system-wide  — AXUIElementCreateSystemWide → kAXFocusedUIElement. Cheapest,
                    and what production should try first.
  2. by-pid       — AXUIElementCreateApplication(frontmost pid) → same attribute.
                    Sometimes answers when the system-wide element won't.
  3. manual-ax    — set the undocumented-but-universal AXManualAccessibility flag
                    on the app element, then retry (2). Chrome and Electron ship
                    their accessibility tree switched off until a client asks;
                    this is the ask.
"""

import sys
import time

import ApplicationServices as AX
from AppKit import NSWorkspace
from Foundation import NSDate, NSRunLoop

SAMPLE_SECONDS = 1.0
DEFAULT_DURATION = 120.0
# How much text before the caret to fetch. The rule only reads the last
# non-whitespace char; a window lets us eyeball whether the API is truthful.
LOOKBACK = 40
# Never let a wedged app block the probe (or, later, the dictation path).
AX_TIMEOUT_SECONDS = 0.25

AX_ERRORS = {
    0: "success",
    -25200: "kAXErrorFailure",
    -25201: "kAXErrorIllegalArgument",
    -25202: "kAXErrorInvalidUIElement",
    -25204: "kAXErrorCannotComplete",
    -25205: "kAXErrorAttributeUnsupported",
    -25212: "kAXErrorNoValue",
    -25213: "kAXErrorParameterizedAttributeUnsupported",
    -25211: "kAXErrorAPIDisabled",
    -25208: "kAXErrorNotImplemented",
}

_CLOSING = "\"'”’»›)]}」』"
_OPENING = "([{“‘«‹/@#-"


def _err(code) -> str:
    return AX_ERRORS.get(code, f"err {code}")


def _attr(element, name):
    """(value, error_name). value is None whenever the read failed."""
    if element is None:
        return None, "no element"
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return (value if err == 0 else None), _err(err)


def _focused_element():
    """Try all three strategies; return (element, strategy_name, notes)."""
    notes = []

    system = AX.AXUIElementCreateSystemWide()
    AX.AXUIElementSetMessagingTimeout(system, AX_TIMEOUT_SECONDS)
    element, err = _attr(system, AX.kAXFocusedUIElementAttribute)
    if element is not None:
        return element, "system-wide", notes
    notes.append(f"system-wide: {err}")

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        notes.append("by-pid: no frontmost app")
        return None, None, notes
    app_element = AX.AXUIElementCreateApplication(app.processIdentifier())
    AX.AXUIElementSetMessagingTimeout(app_element, AX_TIMEOUT_SECONDS)
    element, err = _attr(app_element, AX.kAXFocusedUIElementAttribute)
    if element is not None:
        return element, "by-pid", notes
    notes.append(f"by-pid: {err}")

    # Chrome/Electron keep their AX tree off until a client sets this.
    AX.AXUIElementSetAttributeValue(app_element, "AXManualAccessibility", True)
    element, err = _attr(app_element, AX.kAXFocusedUIElementAttribute)
    if element is not None:
        return element, "manual-ax", notes
    notes.append(f"manual-ax: {err}")
    return None, None, notes


def _range_value(ax_value):
    if ax_value is None:
        return None
    ok, rng = AX.AXValueGetValue(ax_value, AX.kAXValueTypeCFRange, None)
    if not ok:
        return None
    # pyobjc returns CFRange as a plain (location, length) tuple in some builds
    # and as a struct with .location/.length in others. Accept both.
    if hasattr(rng, "location"):
        return int(rng.location), int(rng.length)
    location, length = rng
    return int(location), int(length)


def _string_for_range(element, location, length):
    """Parameterized read: fetch only the slice we want.

    The only viable path in an editor holding a large document, since kAXValue
    would copy the entire thing across the process boundary on every dictation.
    """
    rng = AX.AXValueCreate(AX.kAXValueTypeCFRange, (location, length))
    if rng is None:
        return None, "AXValueCreate failed"
    err, value = AX.AXUIElementCopyParameterizedAttributeValue(
        element, AX.kAXStringForRangeParameterizedAttribute, rng, None
    )
    return (value if err == 0 else None), _err(err)


def probe():
    """One sample. Returns (info_dict, elapsed_ms)."""
    t0 = time.perf_counter()
    info = {"strategy": None, "role": None, "before": None, "source": None, "why": []}

    element, strategy, notes = _focused_element()
    info["strategy"], info["why"] = strategy, notes
    if element is None:
        return info, (time.perf_counter() - t0) * 1000

    info["role"] = _attr(element, AX.kAXRoleAttribute)[0]
    info["attrs"] = AX.AXUIElementCopyAttributeNames(element, None)[1]

    sel, err = _attr(element, AX.kAXSelectedTextRangeAttribute)
    caret = _range_value(sel)
    if caret is None:
        info["why"].append(f"caret: {err}")
        return info, (time.perf_counter() - t0) * 1000
    location = caret[0]
    info["caret"] = location

    start = max(0, location - LOOKBACK)
    before, err = _string_for_range(element, start, location - start)
    source = "kAXStringForRange"
    if before is None:
        info["why"].append(f"kAXStringForRange: {err}")
        value, err = _attr(element, AX.kAXValueAttribute)
        if value is None:
            info["why"].append(f"kAXValue: {err}")
            return info, (time.perf_counter() - t0) * 1000
        before, source = str(value)[start:location], "kAXValue"

    info["before"], info["source"] = str(before), source
    return info, (time.perf_counter() - t0) * 1000


def _verdict(before: str) -> str:
    """What the joint rule would decide, given the text before the caret.

    Two independent decisions off one string, matching what Wispr Flow describes
    and FluidVoice implements:

      case  — walk back past horizontal whitespace and closing wrappers to the
              first real character. A newline, or running out of text, means we
              are starting a sentence. So does '.', '!' or '?'. Anything else
              means we are mid-sentence and the transcript's leading capital is
              an artifact of cleanup.py, not the speaker.
      space — purely about the final character: insert one unless something
              already separates us, or we are speaking into an open bracket.
    """
    boundary = None
    for ch in reversed(before):
        if ch in "\n\r":
            break  # start of a line is start of a sentence
        if ch in " \t" or ch in _CLOSING:
            continue
        boundary = ch
        break

    capitalize = boundary is None or boundary in ".!?"
    last = before[-1] if before else ""
    needs_space = bool(last) and not last.isspace() and last not in _OPENING
    return ("SPACE + " if needs_space else "no space, ") + (
        "capitalize" if capitalize else "LOWERCASE first word"
    )


def main() -> int:
    if not AX.AXIsProcessTrusted():
        print(
            "✗ this process is NOT trusted for Accessibility.\n"
            "  Grant it to your terminal app (System Settings → Privacy & Security\n"
            "  → Accessibility) and rerun.",
            file=sys.stderr,
        )
        return 1
    print("✓ Accessibility granted\n")

    duration = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DURATION
    print(
        f"Sampling every {SAMPLE_SECONDS}s for {duration:.0f}s. Ctrl-C to stop early.\n"
        "Click into a text field in each app you dictate into and type a sentence\n"
        "ending in a period, then a few words with no period.\n"
        "Every app is reported once; a '·' means nothing changed.\n"
    )

    deadline = time.time() + duration
    seen_apps = set()
    last_key = None
    try:
        while time.time() < deadline:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            name = app.localizedName() if app else "?"
            # One uncooperative app must never kill the run — and in production
            # it must never kill a dictation. Same rule, enforced here first.
            try:
                info, elapsed = probe()
            except Exception as exc:
                info = {
                    "strategy": "crashed", "role": None, "before": None,
                    "source": None, "why": [f"{type(exc).__name__}: {exc}"],
                }
                elapsed = 0.0

            key = (name, info["strategy"], info.get("before"), tuple(info["why"]))
            if key == last_key:
                sys.stdout.write("·")
                sys.stdout.flush()
            else:
                last_key = key
                print()
                if info["before"] is not None:
                    print(
                        f"  ✓ {name:<16} {info['role']} caret@{info.get('caret')} "
                        f"via {info['strategy']}/{info['source']} [{elapsed:.1f}ms]\n"
                        f"      before caret: {info['before'][-LOOKBACK:]!r}\n"
                        f"      would do:     {_verdict(info['before'])}"
                    )
                else:
                    print(
                        f"  ✗ {name:<16} role={info['role']} "
                        f"strategy={info['strategy']} [{elapsed:.1f}ms]\n"
                        f"      {' | '.join(info['why'])}"
                    )
                    # First time we fail on an app, dump what it DOES expose —
                    # that tells us whether a different attribute would work.
                    if name not in seen_apps and info.get("attrs"):
                        attrs = [a for a in info["attrs"] if "Text" in a or "Value" in a
                                 or "Selected" in a or "Insertion" in a]
                        print(f"      text-ish attrs: {attrs or 'none'}")
                seen_apps.add(name)

            # runUntilDate pumps the run loop, so NSWorkspace actually notices
            # you switching apps. time.sleep() would freeze it on one app.
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(SAMPLE_SECONDS)
            )
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
