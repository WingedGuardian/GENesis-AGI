"""capture-clarity pure-function tests (runtime copy of the garble_features math)."""
import math

import pytest

from genesis.attention.clarity import capture_clarity, frac_below, is_blip


def test_frac_below():
    assert frac_below([], -1.0) == 0.0
    assert frac_below([-2.0, -0.5, -1.5, 0.0], -1.0) == 0.5  # two of four < -1.0


def test_capture_clarity_loud_long_confident_is_max():
    assert capture_clarity(rms=0.2, duration_s=5.0, frac_lt_1=0.0, n_tokens=20) == 1.0


def test_capture_clarity_near_silence_blip_is_low():
    lo = capture_clarity(rms=0.0, duration_s=0.3, frac_lt_1=0.9, n_tokens=1)
    assert 0.0 <= lo <= 1.0
    assert lo < 0.15


def test_capture_clarity_monotonic_in_asr_confidence():
    hi_conf = capture_clarity(0.1, 2.0, 0.0, 8)
    lo_conf = capture_clarity(0.1, 2.0, 0.5, 8)
    assert hi_conf > lo_conf  # more < -1.0 tokens -> lower clarity


def test_capture_clarity_nan_safe():
    v = capture_clarity(rms=float("nan"), duration_s=2.0, frac_lt_1=0.1, n_tokens=8)
    assert 0.0 <= v <= 1.0 and not math.isnan(v)


def test_is_blip():
    assert is_blip(rms=0.01, duration_s=0.4, n_tokens=1) is True     # near-silence + short
    assert is_blip(rms=0.01, duration_s=5.0, n_tokens=20) is False   # quiet but long + rich
    assert is_blip(rms=0.2, duration_s=0.2, n_tokens=1) is False     # loud -> not a blip


# ── has_audio=False: the text-only path (OMI — no capture physics to judge) ─────────


def test_capture_clarity_no_audio_is_three_term_mean():
    # loudness dropped: mean of (confidence, length, richness) only.
    # confidence=1.0, length=2.0/4.0, richness=4/8 -> (1.0 + 0.5 + 0.5) / 3
    v = capture_clarity(rms=0.0, duration_s=2.0, frac_lt_1=0.0, n_tokens=4, has_audio=False)
    assert v == pytest.approx(2.0 / 3.0)


def test_capture_clarity_no_audio_ignores_rms():
    a = capture_clarity(rms=0.0, duration_s=2.0, frac_lt_1=0.1, n_tokens=4, has_audio=False)
    b = capture_clarity(rms=0.9, duration_s=2.0, frac_lt_1=0.1, n_tokens=4, has_audio=False)
    assert a == b


def test_capture_clarity_default_reproduces_four_term_mean():
    # regression pin: the default (audio) path must stay byte-identical to the
    # pre-has_audio math (also guards the mirrored edge-vendored copy's behaviour).
    conf, loud = 1.0 - 0.2, (0.095 - 0.02) / (0.17 - 0.02)
    length, rich = 2.0 / 4.0, 4 / 8.0
    expected = (conf + loud + length + rich) / 4.0
    assert capture_clarity(rms=0.095, duration_s=2.0, frac_lt_1=0.2, n_tokens=4) == pytest.approx(expected)


def test_is_blip_no_audio_only_empty_text():
    # no physics to judge: any tokens at all -> never a blip...
    assert is_blip(rms=0.0, duration_s=0.2, n_tokens=1, has_audio=False) is False
    # ...but a token-less row is still junk.
    assert is_blip(rms=0.0, duration_s=0.2, n_tokens=0, has_audio=False) is True
