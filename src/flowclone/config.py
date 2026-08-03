"""Load and validate config.toml (personal dictionary + cleanup options).

The file lives at the project root next to pyproject.toml and is optional — a
missing or malformed file falls back to the built-in defaults so the daemon
always starts. Parsed once at launch; edits require a restart.
"""

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import tomlkit

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


# Weight precision for the STT model. "8bit" is byte-for-byte identical to the
# full "none" (bf16) output on our benchmark while cutting resident RAM ~39%;
# "4bit" cuts ~60% with only cosmetic punctuation drift. See scripts/bench_quant.py.
VALID_QUANTIZATION: tuple[str, ...] = ("none", "8bit", "4bit")


@dataclass(frozen=True)
class ModelConfig:
    quantization: str = "8bit"


def load_model(path: Path = CONFIG_PATH) -> ModelConfig:
    """Read [model].quantization, falling back to the 8-bit default on any error."""
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (FileNotFoundError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return ModelConfig()
    quant = str(raw.get("model", {}).get("quantization", "8bit")).lower()
    if quant not in VALID_QUANTIZATION:
        quant = "8bit"
    return ModelConfig(quantization=quant)


@dataclass(frozen=True)
class HandsFreeConfig:
    # Quick-tap Right ⌘ to lock the mic on; stop by tapping again or by
    # falling silent for silence_stop_seconds. Holding is always push-to-talk.
    enabled: bool = True
    silence_stop_seconds: float = 1.5


def load_handsfree(path: Path = CONFIG_PATH) -> HandsFreeConfig:
    """Read [handsfree], falling back to defaults on any error."""
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (FileNotFoundError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return HandsFreeConfig()
    section = raw.get("handsfree", {})
    try:
        seconds = float(section.get("silence_stop_seconds", 1.5))
    except (TypeError, ValueError):
        seconds = 1.5
    seconds = min(10.0, max(0.5, seconds))
    return HandsFreeConfig(
        enabled=bool(section.get("enabled", True)),
        silence_stop_seconds=seconds,
    )


@dataclass(frozen=True)
class CleanupConfig:
    enabled: bool = True
    dedupe_stutters: bool = True
    add_trailing_space: bool = False
    # Read the text before the caret (Accessibility) to decide whether to
    # prepend a space and whether to capitalize. Silently inert in apps that
    # won't answer — see flowclone.context.
    context_aware: bool = True
    # Saying just "scratch that" deletes the previous dictation instead of
    # pasting the words "scratch that" — see cleanup.is_scratch_command.
    scratch_that: bool = True
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
        context_aware=bool(cleanup.get("context_aware", True)),
        scratch_that=bool(cleanup.get("scratch_that", True)),
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


@dataclass(frozen=True)
class PreferencesConfig:
    quantization: str
    handsfree_enabled: bool
    silence_stop_seconds: float
    cleanup_enabled: bool
    dedupe_stutters: bool
    context_aware: bool
    scratch_that: bool
    add_trailing_space: bool
    extra_fillers: tuple[str, ...]
    dictionary: tuple[tuple[str, str], ...]


def load_preferences(path: Path = CONFIG_PATH) -> PreferencesConfig:
    """All settings represented by the native Preferences window."""
    model_cfg = load_model(path)
    handsfree_cfg = load_handsfree(path)
    cleanup_cfg = load(path)
    extra_fillers: tuple[str, ...] = ()
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        cleanup_section = raw.get("cleanup", {})
        if isinstance(cleanup_section, dict):
            values = cleanup_section.get("extra_fillers", [])
            if isinstance(values, list):
                extra_fillers = tuple(str(value) for value in values)
    except (FileNotFoundError, tomllib.TOMLDecodeError, TypeError, ValueError):
        pass
    return PreferencesConfig(
        quantization=model_cfg.quantization,
        handsfree_enabled=handsfree_cfg.enabled,
        silence_stop_seconds=handsfree_cfg.silence_stop_seconds,
        cleanup_enabled=cleanup_cfg.enabled,
        dedupe_stutters=cleanup_cfg.dedupe_stutters,
        context_aware=cleanup_cfg.context_aware,
        scratch_that=cleanup_cfg.scratch_that,
        add_trailing_space=cleanup_cfg.add_trailing_space,
        extra_fillers=extra_fillers,
        dictionary=cleanup_cfg.dictionary,
    )


def save_preferences(settings: PreferencesConfig, path: Path = CONFIG_PATH) -> None:
    """Update known settings atomically while preserving TOML comments."""
    if settings.quantization not in VALID_QUANTIZATION:
        raise ValueError(f"Unsupported quantization: {settings.quantization}")
    seconds = min(10.0, max(0.5, float(settings.silence_stop_seconds)))
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, tomlkit.exceptions.ParseError):
        document = tomlkit.document()

    for name in ("model", "handsfree", "cleanup"):
        if name not in document or not isinstance(document[name], dict):
            document[name] = tomlkit.table()

    document["model"]["quantization"] = settings.quantization
    document["handsfree"]["enabled"] = bool(settings.handsfree_enabled)
    document["handsfree"]["silence_stop_seconds"] = seconds
    document["cleanup"]["enabled"] = bool(settings.cleanup_enabled)
    document["cleanup"]["dedupe_stutters"] = bool(settings.dedupe_stutters)
    document["cleanup"]["context_aware"] = bool(settings.context_aware)
    document["cleanup"]["scratch_that"] = bool(settings.scratch_that)
    document["cleanup"]["add_trailing_space"] = bool(settings.add_trailing_space)
    document["cleanup"]["extra_fillers"] = list(settings.extra_fillers)

    dictionary = tomlkit.table()
    seen_spoken: set[str] = set()
    for spoken, replacement in settings.dictionary:
        spoken = str(spoken).strip()
        replacement = str(replacement).strip()
        normalized = spoken.casefold()
        if not spoken or not replacement:
            raise ValueError("Dictionary entries cannot be blank.")
        if normalized in seen_spoken:
            raise ValueError(f"Duplicate dictionary phrase: {spoken}")
        seen_spoken.add(normalized)
        dictionary.add(spoken, replacement)
    document["dictionary"] = dictionary

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(tomlkit.dumps(document), encoding="utf-8")
    temp.replace(path)
