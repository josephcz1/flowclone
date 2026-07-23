"""Deterministic, zero-latency transcript cleanup (Milestone 5).

Runs on the batch-accurate text before it is pasted. Four passes, in order:

  1. personal dictionary  — fix words parakeet reliably mishears ("cloud" ->
     "Claude"); whole-word, case-insensitive, longest phrase wins.
  2. filler strip         — drop "um"/"uh"/… (see config.DEFAULT_FILLERS).
  3. stutter dedupe       — collapse immediate word repeats ("the the" -> "the").
  4. tidy                 — repair the whitespace/punctuation the strips leave
                            behind, and set leading capitalization to match the
                            text already at the caret (see flowclone.context).

The live HUD keeps showing the raw streaming partials; cleanup only shapes the
committed text — the same split Wispr Flow makes. Everything here is regex on a
short string, so it adds no measurable latency to the paste path.
"""

import re

from flowclone.config import CleanupConfig

# A run of one or more consecutive identical words, e.g. "the the the".
_STUTTER = re.compile(r"\b(\w+)(?:[ \t]+\1\b)+", re.IGNORECASE)
# Space(s) sitting in front of closing punctuation, left when a filler before
# the punctuation is removed ("so , basically" -> "so, basically").
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")
# Two or more punctuation/whitespace clusters collapsed to the first mark.
_REPEAT_PUNCT = re.compile(r"([,.!?;:])(?:[ \t]*[,.!?;:])+")
_MULTISPACE = re.compile(r"[ \t]{2,}")


def _apply_dictionary(text: str, dictionary) -> str:
    for spoken, replacement in dictionary:
        # \b won't anchor around phrases that start/end with non-word chars, but
        # every practical dictionary entry is word-ish, so \b is the right guard.
        pattern = re.compile(rf"\b{re.escape(spoken)}\b", re.IGNORECASE)
        text = pattern.sub(lambda _m, r=replacement: r, text)
    return text


def _strip_fillers(text: str, fillers) -> str:
    if not fillers:
        return text
    alternation = "|".join(re.escape(f) for f in sorted(fillers, key=len, reverse=True))
    # (?<![\w-]) / (?![\w-]) is a word boundary that also refuses to fire inside
    # hyphenated words, so "uh-huh" and "um-brella"-like tokens survive.
    filler_re = re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])", re.IGNORECASE)
    return filler_re.sub("", text)


def _tidy(text: str, capitalize: bool = True) -> str:
    text = _REPEAT_PUNCT.sub(r"\1", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _MULTISPACE.sub(" ", text)
    # Leading junk left by a stripped opening filler ("  , so" -> "so").
    text = re.sub(r"^[\s,]+", "", text)
    text = text.strip()
    if not text:
        return text
    # Removing a leading "Um," can leave the next word lowercased — recapitalize.
    # But when the caret sits mid-sentence, the capital is this function's own
    # artifact rather than the speaker's, so context.decide asks us to drop it.
    if capitalize:
        return text[0].upper() + text[1:] if text[0].islower() else text
    return text[0].lower() + text[1:] if text[0].isupper() else text


def clean(text: str, cfg: CleanupConfig, join=None) -> str:
    """Return the cleaned transcript. Dictionary always applies; the filler and
    stutter passes are skipped when cleanup is disabled.

    `join` is a context.Join describing the text already before the caret, or
    None when the focused app wouldn't tell us. None preserves the historical
    behavior exactly: always capitalize, never prepend.
    """
    if not text:
        return text
    text = _apply_dictionary(text, cfg.dictionary)
    if cfg.enabled:
        text = _strip_fillers(text, cfg.fillers)
        if cfg.dedupe_stutters:
            text = _STUTTER.sub(r"\1", text)
    text = _tidy(text, capitalize=join.capitalize if join else True)
    # After _tidy, which strips: a leading space it would have eaten.
    if join is not None and join.space and text:
        text = " " + text
    if cfg.add_trailing_space and text:
        text += " "
    return text
