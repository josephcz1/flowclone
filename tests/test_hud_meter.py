"""The RMS -> lit-bars mapping that drives the pill's level meter."""

from flowclone.hud import BAR_COUNT, RMS_CEIL, RMS_FLOOR, bars_for_rms


def test_silence_keeps_one_bar_lit():
    assert bars_for_rms(0.0) == 1
    assert bars_for_rms(RMS_FLOOR) == 1


def test_loud_speech_lights_everything():
    assert bars_for_rms(RMS_CEIL) == BAR_COUNT
    assert bars_for_rms(1.0) == BAR_COUNT


def test_monotone_and_in_range():
    prev = 0
    for rms in (0.001, 0.005, 0.01, 0.03, 0.07, 0.15, 0.3):
        n = bars_for_rms(rms)
        assert 1 <= n <= BAR_COUNT
        assert n >= prev
        prev = n
    assert prev == BAR_COUNT


def test_normal_speech_sits_midrange():
    assert 2 <= bars_for_rms(0.03) <= 4
