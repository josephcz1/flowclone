import json

from flowclone import history


def test_history_is_newest_first_and_persistent(tmp_path):
    path = tmp_path / "history.json"
    history.add("first", "app.one", "pasted", path)
    history.add("second", "app.two", "target_changed", path)

    entries = history.recent(path)
    assert [entry.text for entry in entries] == ["second", "first"]
    assert entries[0].app_id == "app.two"
    assert entries[0].outcome == "target_changed"


def test_history_is_bounded(tmp_path):
    path = tmp_path / "history.json"
    for i in range(history.MAX_ENTRIES + 5):
        history.add(f"entry {i}", "app", "pasted", path)

    entries = history.recent(path)
    assert len(entries) == history.MAX_ENTRIES
    assert entries[0].text == f"entry {history.MAX_ENTRIES + 4}"


def test_empty_and_damaged_history_are_safe(tmp_path):
    path = tmp_path / "history.json"
    assert history.add("", None, "empty", path) is None
    path.write_text("not json")
    assert history.recent(path) == []


def test_history_skips_invalid_entries(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps([{"bad": True}, {"text": "kept"}]))
    assert [entry.text for entry in history.recent(path)] == ["kept"]


def test_clear_history(tmp_path):
    path = tmp_path / "history.json"
    history.add("text", "app", "pasted", path)
    history.clear(path)
    assert history.recent(path) == []
