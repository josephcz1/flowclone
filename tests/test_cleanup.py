"""Cleanup-engine unit tests against realistic sloppy-dictation fixtures.

Run: uv run pytest
"""

from flowclone.cleanup import clean
from flowclone.config import CleanupConfig, load
from flowclone.context import Join, decide

DICT = (
    ("cloud code", "Claude Code"),
    ("cloud", "Claude"),
    ("wispr", "Wispr"),
)
CFG = CleanupConfig(dictionary=DICT)


def test_leading_filler_stripped_and_recapitalized():
    assert clean("Um, so basically it works.", CFG) == "So basically it works."


def test_mid_sentence_fillers_removed():
    assert clean("I want, uh, to refactor the API.", CFG) == "I want, to refactor the API."


def test_dictionary_single_word():
    assert clean("open cloud and ask it", CFG) == "Open Claude and ask it"


def test_dictionary_phrase_beats_single_word():
    # "cloud code" must win over the standalone "cloud" -> "Claude" rule.
    assert clean("run cloud code in the repo", CFG) == "Run Claude Code in the repo"


def test_dictionary_is_case_insensitive_and_whole_word():
    # "cloudy" must not be touched by the "cloud" entry.
    assert clean("a cloudy day with Cloud open", CFG) == "A cloudy day with Claude open"


def test_stutter_dedupe():
    assert clean("the the API endpoint", CFG) == "The API endpoint"


def test_stutter_dedupe_can_be_disabled():
    cfg = CleanupConfig(dictionary=DICT, dedupe_stutters=False)
    assert clean("the the API", cfg) == "The the API"


def test_hyphenated_word_survives_filler_strip():
    # "uh" inside "uh-huh" must not be stripped.
    assert clean("uh-huh that works", CFG) == "Uh-huh that works"


def test_disabled_still_applies_dictionary_but_not_fillers():
    cfg = CleanupConfig(dictionary=DICT, enabled=False)
    assert clean("um, open cloud", cfg) == "Um, open Claude"


def test_trailing_space_option():
    cfg = CleanupConfig(dictionary=DICT, add_trailing_space=True)
    assert clean("done", cfg) == "Done "


def test_empty_input():
    assert clean("", CFG) == ""
    assert clean("   ", CFG) == ""


def test_extra_fillers_opt_in():
    cfg = CleanupConfig(fillers=("um", "you know"))
    assert clean("it is, you know, fast", cfg) == "It is, fast"


def test_rambling_fixture():
    raw = "Um, so I I want to, uh, ask cloud to, um, parse the the JSON."
    assert clean(raw, CFG) == "So I want to, ask Claude to, parse the JSON."


def test_join_none_preserves_historical_behavior():
    # Every pre-existing test above passes join=None implicitly; make the
    # guarantee explicit so a future refactor can't quietly change it.
    assert clean("um, so it works", CFG, None) == "So it works"


def test_join_prepends_space_and_keeps_capital_after_a_period():
    join = decide("I am going to the store.")
    assert clean("Then I came back.", CFG, join) == " Then I came back."


def test_join_lowercases_a_mid_sentence_continuation():
    join = decide("I went to the")
    assert clean("Store yesterday.", CFG, join) == " store yesterday."


def test_join_adds_no_space_when_one_is_already_there():
    join = decide("I went to the ")
    assert clean("Store yesterday.", CFG, join) == "store yesterday."


def test_join_lowercase_survives_a_stripped_leading_filler():
    # The filler strip re-exposes "want" as the first word; it must end up
    # lowercase because the caret is mid-sentence, not capitalized by _tidy.
    join = decide("I really")
    assert clean("Um, want to refactor", CFG, join) == " want to refactor"


def test_join_never_emits_a_lone_space_for_an_empty_transcript():
    assert clean("", CFG, Join(space=True, capitalize=True)) == ""
    assert clean("   ", CFG, Join(space=True, capitalize=True)) == ""


def test_join_composes_with_trailing_space_option():
    cfg = CleanupConfig(dictionary=DICT, add_trailing_space=True)
    assert clean("done", cfg, Join(space=True, capitalize=True)) == " Done "


def test_join_applies_after_the_dictionary():
    # A dictionary replacement lands at the start; the mid-sentence rule must
    # still lowercase it rather than the dictionary's capital winning.
    join = decide("I opened")
    assert clean("cloud and asked", CFG, join) == " claude and asked"


def test_default_config_loads():
    # The shipped config.toml must parse and seed the dictionary.
    cfg = load()
    assert isinstance(cfg, CleanupConfig)
    assert any(spoken == "cloud" for spoken, _ in cfg.dictionary)
