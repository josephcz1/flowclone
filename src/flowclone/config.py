"""Load and validate config.toml (personal dictionary + cleanup options).

The file lives at the project root next to pyproject.toml and is optional — a
missing or malformed file falls back to the built-in defaults so the daemon
always starts. Parsed once at launch; edits require a restart.
"""

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"

# Conservative by design: only sounds that are never meaningful words. Softer
# fillers ("like", "you know", "basically", "actually", "i mean", "sort of")
# are opt-in via [cleanup].extra_fillers so cleanup never eats real content.
DEFAULT_FILLERS: tuple[str, ...] = (
    "um",
    "umm",
    "uhm",
    "uh",
    "uhh",
    "er",
    "erm",
    "hmm",
    "mmm",
    "mhm",
)


@dataclass(frozen=True)
class CleanupConfig:
    enabled: bool = True
    dedupe_stutters: bool = True
    add_trailing_space: bool = False
    fillers: tuple[str, ...] = DEFAULT_FILLERS
    # (spoken, replacement) pairs, sorted longest-first so multi-word entries
    # ("cloud code" -> "Claude Code") win over the single-word rule.
    dictionary: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _coerce(raw: dict) -> CleanupConfig:
    cleanup = raw.get("cleanup", {})
    fillers = list(DEFAULT_FILLERS)
    fillers += [str(f) for f in cleanup.get("extra_fillers", [])]
    keep = {str(k).lower() for k in cleanup.get("keep", [])}
    fillers = tuple(dict.fromkeys(f for f in fillers if f.lower() not in keep))

    dictionary = raw.get("dictionary", {})
    pairs = sorted(
        ((str(k), str(v)) for k, v in dictionary.items()),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )

    return CleanupConfig(
        enabled=bool(cleanup.get("enabled", True)),
        dedupe_stutters=bool(cleanup.get("dedupe_stutters", True)),
        add_trailing_space=bool(cleanup.get("add_trailing_space", False)),
        fillers=fillers,
        dictionary=tuple(pairs),
    )


def load(path: Path = CONFIG_PATH) -> CleanupConfig:
    """Parse config.toml, or return defaults if it is missing/unreadable."""
    try:
        with open(path, "rb") as fh:
            return _coerce(tomllib.load(fh))
    except (FileNotFoundError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return replace(CleanupConfig())
