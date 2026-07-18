"""Canonical signal formatter — one format for every reflection depth."""

from genesis.awareness.signal_format import format_signal_line, format_signals
from genesis.awareness.types import SignalReading


def _sig(name="cpu_usage", value=0.3, source="system", **kw):
    return SignalReading(
        name=name, value=value, source=source,
        collected_at="2026-07-18T00:00:00+00:00", **kw,
    )


def test_line_basic():
    assert format_signal_line(_sig()) == "cpu_usage: 0.3 (source=system)"


def test_line_thresholds_and_status():
    line = format_signal_line(_sig(
        value=0.9, normal_max=0.5, warning_threshold=0.7, critical_threshold=0.85,
    ))
    assert "[CRITICAL; normal<=0.5, warn>=0.7, crit>=0.85]" in line


def test_line_baseline_note_and_persistence():
    line = format_signal_line(
        _sig(baseline_note="Baseline: 4.0/day. Recent: 27.0/day."),
        unchanged_ticks=24,
    )
    assert "-- baseline: Baseline: 4.0/day. Recent: 27.0/day." in line
    assert "(persistent ~2.0h)" in line


def test_format_signals_no_truncation():
    sigs = [_sig(name=f"signal_{i:02d}", value=0.5) for i in range(15)]
    text = format_signals(sigs)
    assert text.count("\n") == 14  # all 15 render — no silent [:10] cap


def test_format_signals_min_value_and_excluded():
    sigs = [
        _sig(name="keep_me", value=0.5),
        _sig(name="zero_bootstrap", value=0.0),
        _sig(name="excluded_one", value=0.9),
    ]
    text = format_signals(
        sigs, excluded_signals={"excluded_one"}, min_value=0.001,
    )
    assert "keep_me" in text
    assert "zero_bootstrap" not in text
    assert "excluded_one" not in text


def test_format_signals_empty_token():
    assert format_signals([], empty="none") == "none"
    assert format_signals([]) == ""
