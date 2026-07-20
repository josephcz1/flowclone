"""Cleanup-engine unit tests against realistic sloppy-dictation fixtures.

Run: uv run pytest
"""

from flowclone.cleanup import clean
from flowclone.config import CleanupConfig, load

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


def test_default_config_loads():
    # The shipped config.toml must parse and seed the dictionary.
    cfg = load()
    assert isinstance(cfg, CleanupConfig)
    assert any(spoken == "cloud" for spoken, _ in cfg.dictionary)
