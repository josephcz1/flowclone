"""What is the text just before the caret? Answer it, so a dictation joins what
is already there.

Without this FlowClone is blind: `inject.paste_text` sets a clipboard and posts
⌘V into whatever holds focus, never looking at the destination. Two dictations in
a row therefore run together ("…to the store.Then I left"), and `cleanup._tidy`
puts a capital on every utterance even when you paused mid-sentence ("I went to
the" + "Store yesterday").

`decide` is the rule and is the same regardless of how the text was obtained.
There are two sources for it, tried in that order:

  recall()            what we pasted last, if nothing has happened since. Free,
                      needs no permission, and works in every app on earth
                      because it never asks the app anything.
  read_before_caret() the Accessibility API. ~1 ms, and the only source that can
                      see text FlowClone did not write — you clicked into the
                      middle of a paragraph and started speaking.

They cover disjoint cases, which is why both exist. Measured with
`scripts/ax_probe.py`, the AX one covers less than you would hope:

  Terminal, Safari, Notes    ✓  0.6–2.8 ms warm, 6–31 ms on first touch
  WezTerm                    ✗  focused element is the AXWindow, no caret
  Obsidian, ChatGPT          ✗  kAXErrorNoValue (Electron)

That list is why `recall` is first: the back-to-back dictation is both the
reported bug and the case AX misses in exactly the apps most used.

Blindness is still normal, not exceptional. Both sources return None for it and
callers keep their previous behavior rather than guessing — a missing space is a
smaller insult than a space that shouldn't be there.
"""

import threading

import ApplicationServices as AX
from AppKit import NSWorkspace

# Enough context to find a sentence boundary without hauling a whole document
# across the process boundary. The rule only ever inspects the tail.
LOOKBACK = 64
# A wedged app must never stall a dictation. The probe runs while you are still
# speaking, so this ceiling is generous and still invisible.
AX_TIMEOUT_SECONDS = 0.25

# Transparent when hunting for the sentence boundary: 'He left."' still ends a
# sentence, and the quote is not the thing that decides case.
_CLOSING = "\"'”’»›)]}」』"
_SKIP = " \t" + _CLOSING
# Seeing one of these proves we are mid-sentence, so the transcript's leading
# capital is cleanup's artifact rather than the speaker's intent.
_CONTINUES = ",;:"
# You are speaking *into* these, so a leading space would be wrong. Note '-' and
# '/' cover "well-" and "example.com/", where the next word continues a token.
_OPENING = "([{“‘«‹/@#-"


class Join:
    """How the incoming transcript should be attached to the text before it.

    `space` prepends one; `capitalize` decides whether the first letter is upper
    or lower. They are independent: "one, two," wants a space *and* lowercase,
    while "store. " wants neither.
    """

    __slots__ = ("space", "capitalize")

    def __init__(self, space: bool, capitalize: bool) -> None:
        self.space = space
        self.capitalize = capitalize

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, Join)
            and self.space == other.space
            and self.capitalize == other.capitalize
        )

    def __repr__(self) -> str:
        return f"Join(space={self.space}, capitalize={self.capitalize})"


def decide(before: str) -> Join:
    """Derive the join from the text preceding the caret.

    Walks back past horizontal whitespace and closing quotes/brackets to the
    first character that actually carries meaning. A newline, or running out of
    text, means we are starting a line and therefore a sentence.

    The capitalize default is deliberately inverted from the obvious reading:
    rather than "capitalize after .!?", it is "capitalize unless we can *see*
    we're mid-sentence". A shell prompt ends in "% " and Claude Code's input box
    in "> "; under the obvious rule both would have lowercased the first word of
    every dictation. Defaulting to capital means anything we fail to understand
    degrades to cleanup.py's existing behavior instead of a fresh bug.
    """
    boundary = None
    for ch in reversed(before):
        if ch in "\n\r":
            break  # start of a line is start of a sentence
        if ch in _SKIP:
            continue
        boundary = ch
        break

    mid_sentence = boundary is not None and (boundary.isalnum() or boundary in _CONTINUES)
    last = before[-1] if before else ""
    return Join(
        space=bool(last) and not last.isspace() and last not in _OPENING,
        capitalize=not mid_sentence,
    )


# --- source 1: our own last paste -------------------------------------------
#
# Invalidated from the event tap thread, read from the dictation thread.
_lock = threading.Lock()
_last: tuple[str, str] | None = None  # (pasted text, app it went to)


def remember(text: str, app: str | None) -> None:
    """Record a paste that landed, as the presumed text before the caret."""
    global _last
    if not text or not app:
        return
    with _lock:
        _last = (text, app)


def invalidate() -> None:
    """Forget it — the user typed or clicked, so the caret is no longer ours.

    Called on every real key and mouse press (see hotkey.HoldToTalk). Cheap on
    purpose: this runs on the event tap's thread, in front of your keystrokes.
    """
    global _last
    with _lock:
        _last = None


def recall(app: str | None) -> str | None:
    """Text before the caret per our own memory, or None if we can't be sure.

    Sure means: we pasted something, it went to this same app, and no keystroke
    or click has arrived since. Anything else — you clicked elsewhere, switched
    apps, typed a character — clears it, so this can report stale text only if
    the caret moved without any input event at all.
    """
    with _lock:
        if _last is None or app is None or _last[1] != app:
            return None
        return _last[0]


def frontmost_app_id() -> str | None:
    """Bundle id of the app that will receive the paste. Never raises."""
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return app.bundleIdentifier() or app.localizedName()
    except Exception:
        return None


# --- source 2: the Accessibility API ----------------------------------------


def _attr(element, name):
    """AXUIElementCopyAttributeValue -> value, or None on any AX error."""
    if element is None:
        return None
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def _caret_location(element) -> int | None:
    """Offset of the caret, or of the start of the selection it will replace."""
    ax_value = _attr(element, AX.kAXSelectedTextRangeAttribute)
    if ax_value is None:
        return None
    ok, rng = AX.AXValueGetValue(ax_value, AX.kAXValueTypeCFRange, None)
    if not ok:
        return None
    # pyobjc hands back CFRange as a plain tuple in some builds and as a struct
    # with .location in others.
    return int(rng.location) if hasattr(rng, "location") else int(rng[0])


def _focused_element():
    """The focused UI element, via the frontmost app's own AX element.

    Deliberately *not* AXUIElementCreateSystemWide first: on this machine the
    system-wide element returned kAXErrorCannotComplete for every app tested,
    while the per-pid element answered immediately. It stays as a fallback.
    """
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is not None:
        element = AX.AXUIElementCreateApplication(app.processIdentifier())
        AX.AXUIElementSetMessagingTimeout(element, AX_TIMEOUT_SECONDS)
        focused = _attr(element, AX.kAXFocusedUIElementAttribute)
        if focused is not None:
            return focused

    system = AX.AXUIElementCreateSystemWide()
    AX.AXUIElementSetMessagingTimeout(system, AX_TIMEOUT_SECONDS)
    return _attr(system, AX.kAXFocusedUIElementAttribute)


def _read(lookback: int) -> str | None:
    element = _focused_element()
    if element is None:
        return None
    location = _caret_location(element)
    if location is None:
        return None

    start = max(0, location - lookback)
    length = location - start
    # Ask for just the slice. The whole-value fallback exists for fields that
    # don't implement the parameterized attribute, but it copies the entire
    # document, so it must never be the first choice in a large editor.
    rng = AX.AXValueCreate(AX.kAXValueTypeCFRange, (start, length))
    if rng is not None:
        err, value = AX.AXUIElementCopyParameterizedAttributeValue(
            element, AX.kAXStringForRangeParameterizedAttribute, rng, None
        )
        if err == 0 and value is not None:
            return str(value)

    value = _attr(element, AX.kAXValueAttribute)
    if value is None:
        return None
    # AX offsets are UTF-16 code units; slicing a Python str by them is wrong
    # past the BMP. Only the fallback path is affected, and only when an emoji
    # sits within LOOKBACK of the caret, so a wrong space is the worst outcome.
    return str(value)[start:location]


def read_before_caret(lookback: int = LOOKBACK) -> str | None:
    """Text immediately before the caret, or None when the app won't say.

    Never raises. A dictation that reaches the paste path must always paste,
    even if this returns garbage or the focused app is mid-crash.
    """
    try:
        return _read(lookback)
    except Exception:
        return None
