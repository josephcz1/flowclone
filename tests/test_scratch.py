"""The scratch-that detector: strict by design, because a false positive
deletes text while a false negative merely pastes visible words."""

from dataclasses import replace

from flowclone import inject
from flowclone.cleanup import is_scratch_command
from flowclone.config import CleanupConfig

CFG = CleanupConfig()


def test_bare_command():
    assert is_scratch_command("scratch that", CFG)


def test_transcript_punctuation_and_case():
    assert is_scratch_command("Scratch that.", CFG)
    assert is_scratch_command("Scratch that!", CFG)


def test_delete_that_variant():
    assert is_scratch_command("Delete that.", CFG)


def test_leading_filler_is_ignored():
    assert is_scratch_command("Um, scratch that.", CFG)
    assert is_scratch_command("Uh... scratch that", CFG)


def test_command_inside_a_sentence_is_content():
    assert not is_scratch_command("I want to scratch that.", CFG)
    assert not is_scratch_command("Scratch that idea.", CFG)
    assert not is_scratch_command("Scratch that, let's start over.", CFG)


def test_empty_and_unrelated():
    assert not is_scratch_command("", CFG)
    assert not is_scratch_command("Hello world.", CFG)


def test_config_toggle_off():
    off = replace(CFG, scratch_that=False)
    assert not is_scratch_command("Scratch that.", off)


def test_extra_fillers_from_config_also_ignored():
    cfg = replace(CFG, fillers=CFG.fillers + ("like",))
    assert is_scratch_command("Like, scratch that.", cfg)


def test_undo_is_one_command_key_event_pair(monkeypatch):
    calls = []
    monkeypatch.setattr(inject, "secure_input_active", lambda: False)
    monkeypatch.setattr(
        inject, "_post_key", lambda keycode, flags=0: calls.append((keycode, flags))
    )

    assert inject.undo()
    assert calls == [(inject.Z_KEYCODE, inject.Quartz.kCGEventFlagMaskCommand)]


def test_undo_refuses_secure_input(monkeypatch):
    monkeypatch.setattr(inject, "secure_input_active", lambda: True)
    monkeypatch.setattr(inject, "NSBeep", lambda: None)
    assert not inject.undo()
