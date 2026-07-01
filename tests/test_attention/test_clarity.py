"""capture-clarity pure-function tests (runtime copy of the garble_features math)."""
import math

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
