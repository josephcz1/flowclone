"""Small, local transcript history used to recover or retry dictations.

History is intentionally bounded and stored as plain JSON under Application
Support. It contains only the final cleaned text, destination app identifier,
and delivery outcome; audio is never retained.
"""

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

HISTORY_PATH = (
    Path.home() / "Library" / "Application Support" / "FlowClone" / "history.json"
)
MAX_ENTRIES = 20

_lock = threading.Lock()


@dataclass(frozen=True)
class HistoryEntry:
    text: str
    created_at: str
    app_id: str | None = None
    outcome: str = "pasted"


def _read(path: Path) -> list[HistoryEntry]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        entries = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            entries.append(
                HistoryEntry(
                    text=item["text"],
                    created_at=str(item.get("created_at", "")),
                    app_id=(
                        str(item["app_id"])
                        if item.get("app_id") is not None
                        else None
                    ),
                    outcome=str(item.get("outcome", "unknown")),
                )
            )
        return entries[:MAX_ENTRIES]
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []


def recent(path: Path = HISTORY_PATH) -> list[HistoryEntry]:
    """Newest-first history. A missing or damaged file behaves as empty."""
    with _lock:
        return _read(path)


def add(
    text: str,
    app_id: str | None,
    outcome: str,
    path: Path = HISTORY_PATH,
) -> HistoryEntry | None:
    """Persist a cleaned transcript and return it, or None for empty text."""
    if not text:
        return None
    entry = HistoryEntry(
        text=text,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        app_id=app_id,
        outcome=outcome,
    )
    with _lock:
        entries = [entry, *_read(path)][:MAX_ENTRIES]
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(
            json.dumps([asdict(item) for item in entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
    return entry


def clear(path: Path = HISTORY_PATH) -> None:
    with _lock:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
