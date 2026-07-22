"""The join rule: given the text before the caret, space and case decisions.

Every fixture here is a real string observed with scripts/ax_probe.py against
Terminal, Safari or Notes, or a case that probe run proved we had wrong.

Run: uv run pytest
"""

import pytest

from flowclone.context import Join, decide

# (text before caret, expected join, why it matters)
CASES = [
    # --- the reported bug -------------------------------------------------
    ("I am going to the store.", Join(space=True, capitalize=True),
     "the original complaint: period then immediately more speech"),
    ("I am going to the store. ", Join(space=False, capitalize=True),
     "same, but the app already left a space — don't double it"),
    # --- the other half of the same bug ------------------------------------
    ("I went to the", Join(space=True, capitalize=False),
     "paused mid-sentence: cleanup's leading capital is wrong here"),
    ("Not very", Join(space=True, capitalize=False),
     "from Joseph's live dictation, which produced 'veryHappy'"),
    ("I went to the ", Join(space=False, capitalize=False),
     "mid-sentence with the space already present"),
    # --- sentence boundaries behind closing punctuation ---------------------
    ('He said "hello."', Join(space=True, capitalize=True),
     "the quote is transparent; the period behind it still ends the sentence"),
    ("(a full aside.)", Join(space=True, capitalize=True), "same for brackets"),
    ("Done!", Join(space=True, capitalize=True), "! ends a sentence"),
    ("Really?", Join(space=True, capitalize=True), "? ends a sentence"),
    # --- prompts and markup: the regression the probe run caught ------------
    ("josephchen@Mac Whisprflow clone % ", Join(space=False, capitalize=True),
     "a shell prompt must not lowercase the first word of a dictation"),
    ("│ > ", Join(space=False, capitalize=True), "Claude Code's input box"),
    ("$ ", Join(space=False, capitalize=True), "sh prompt"),
    ("- ", Join(space=False, capitalize=True), "markdown bullet"),
    ("# ", Join(space=False, capitalize=True), "markdown heading or root prompt"),
    # --- newlines are sentence starts ---------------------------------------
    ("still get it fixed.\n", Join(space=False, capitalize=True),
     "observed in Notes"),
    ("def foo():\n    ", Join(space=False, capitalize=True),
     "indented new line in code"),
    ("some text\n\n", Join(space=False, capitalize=True), "blank line"),
    # --- continuation punctuation -------------------------------------------
    ("items: one, two,", Join(space=True, capitalize=False), "comma continues"),
    ("first clause;", Join(space=True, capitalize=False), "semicolon continues"),
    ("as follows:", Join(space=True, capitalize=False), "colon continues"),
    # --- speaking into an opening token -------------------------------------
    ("a note (", Join(space=False, capitalize=True), "no space after an open bracket"),
    ("https://example.com/", Join(space=False, capitalize=True), "URL path segment"),
    ("well-", Join(space=False, capitalize=True), "hyphenated compound"),
    ("email @", Join(space=False, capitalize=True), "handle"),
    # --- empty ---------------------------------------------------------------
    ("", Join(space=False, capitalize=True), "empty field"),
    ("   ", Join(space=False, capitalize=True), "whitespace-only field"),
]


@pytest.mark.parametrize("before,expected,why", CASES, ids=[c[2] for c in CASES])
def test_decide(before, expected, why):
    assert decide(before) == expected, why


def test_capitalize_is_the_safe_default():
    """Anything we don't understand must degrade to cleanup.py's old behavior.

    Lowercasing is the only genuinely destructive half of this feature, so it
    fires solely on positive evidence of a mid-sentence caret. If that ever
    stops being true, a novel character class starts silently lowercasing.
    """
    exotic = "…»¶§±≈✓🎤、。「" + "​" + "�"
    for ch in exotic:
        assert decide(ch).capitalize, f"{ch!r} should not have triggered lowercase"


def test_lowercase_requires_a_word_character_or_continuation():
    for ch in "abzAZ09":
        assert not decide(ch).capitalize
    for ch in ",;:":
        assert not decide(ch).capitalize


@pytest.fixture
def fresh():
    """context's paste memory, empty before and after."""
    import flowclone.context as ctx

    ctx.invalidate()
    yield ctx
    ctx.invalidate()


def test_recall_returns_what_we_pasted(fresh):
    """The reported bug: two dictations in a row, nothing in between."""
    fresh.remember("This is my first sentence.", "com.apple.Terminal")
    before = fresh.recall("com.apple.Terminal")
    assert before == "This is my first sentence."
    assert decide(before) == Join(space=True, capitalize=True)


def test_recall_is_empty_until_something_is_pasted(fresh):
    assert fresh.recall("com.apple.Terminal") is None


def test_typing_invalidates(fresh):
    """A keystroke moved the caret, so our memory says nothing about it."""
    fresh.remember("some text", "com.apple.Terminal")
    fresh.invalidate()
    assert fresh.recall("com.apple.Terminal") is None


def test_a_different_app_does_not_recall(fresh):
    """⌘-Tab away and dictate: that app's caret is not where we pasted."""
    fresh.remember("some text", "com.apple.Terminal")
    assert fresh.recall("md.obsidian") is None


def test_unknown_app_does_not_recall(fresh):
    """frontmost_app_id failed. Blind is the safe answer, not the last paste."""
    fresh.remember("some text", "com.apple.Terminal")
    assert fresh.recall(None) is None


def test_empty_paste_is_not_remembered(fresh):
    """Nothing was inserted, so the caret is wherever it already was."""
    fresh.remember("", "com.apple.Terminal")
    assert fresh.recall("com.apple.Terminal") is None


def test_only_the_last_paste_is_kept(fresh):
    """decide only ever inspects the tail, so one paste of history is enough."""
    fresh.remember("first.", "com.apple.Terminal")
    fresh.remember("second,", "com.apple.Terminal")
    assert fresh.recall("com.apple.Terminal") == "second,"


def test_recall_round_trips_the_string_cleanup_actually_pasted(fresh):
    """What we store is the final pasted text, leading space and all."""
    fresh.remember(" and then I left.", "com.apple.Terminal")
    assert decide(fresh.recall("com.apple.Terminal")) == Join(
        space=True, capitalize=True
    )


def test_back_to_back_dictations_do_not_run_together(fresh):
    """The whole point, end to end through the real cleanup path.

    Two presses with nothing in between, in an app AX cannot see into. Before
    the paste memory existed this produced "sentence.And".
    """
    from flowclone import cleanup, config

    cfg = config.CleanupConfig()
    app = "com.github.wez.wezterm"

    first = cleanup.clean("This is my first sentence.", cfg, None)
    fresh.remember(first, app)

    join = decide(fresh.recall(app))
    second = cleanup.clean("And this is my second sentence.", cfg, join)

    assert first + second == "This is my first sentence. And this is my second sentence."


def test_a_dictation_resumed_mid_sentence_stays_lowercase(fresh):
    """The other half: you paused for breath rather than finishing a thought."""
    from flowclone import cleanup, config

    cfg = config.CleanupConfig()
    app = "com.github.wez.wezterm"

    first = cleanup.clean("I went to the", cfg, None)
    fresh.remember(first, app)

    join = decide(fresh.recall(app))
    second = cleanup.clean("store yesterday.", cfg, join)

    assert first + second == "I went to the store yesterday."


def test_typing_between_dictations_falls_back_to_blind(fresh):
    """Invalidation must reach cleanup as None, i.e. the old behavior."""
    from flowclone import cleanup, config

    cfg = config.CleanupConfig()
    app = "com.github.wez.wezterm"

    fresh.remember(cleanup.clean("First.", cfg, None), app)
    fresh.invalidate()  # the event tap saw a keystroke

    before = fresh.recall(app)
    join = decide(before) if before is not None else None
    assert join is None
    assert cleanup.clean("second thing.", cfg, join) == "Second thing."


def test_frontmost_app_id_never_raises(monkeypatch):
    import flowclone.context as ctx

    class Boom:
        def sharedWorkspace(self):
            raise RuntimeError("WindowServer said no")

    monkeypatch.setattr(ctx, "NSWorkspace", Boom())
    assert ctx.frontmost_app_id() is None


def test_read_before_caret_never_raises(monkeypatch):
    """The paste path must survive a hostile or crashing focused app."""
    import flowclone.context as ctx

    def boom(*_args, **_kwargs):
        raise RuntimeError("the focused app went away mid-read")

    monkeypatch.setattr(ctx, "_read", boom)
    assert ctx.read_before_caret() is None
