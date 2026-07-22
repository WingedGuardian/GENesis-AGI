"""Tests for the rate-limit reset parser (pure, injected clock).

The parser is best-effort by design: it never raises, degrades to None on any
miss, and treats a day-ambiguous weekly wall-clock time as unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.cc.rate_limit_reset import (
    SESSION,
    UNKNOWN,
    WEEKLY,
    detect_limit_kind,
    parse_reset,
)

NOW = datetime(2026, 7, 22, 14, 0, 0, tzinfo=UTC)


def test_prose_relative_duration_is_unambiguous():
    kind, reset = parse_reset(raw_text="You've hit your limit · resets in 2h 30m", now=NOW)
    assert reset == NOW + timedelta(hours=2, minutes=30)


def test_prose_wallclock_session_next_occurrence():
    kind, reset = parse_reset(raw_text="Session limit — resets 5pm", now=NOW)
    assert kind == SESSION
    assert reset == NOW.replace(hour=17, minute=0)


def test_prose_wallclock_weekly_is_ambiguous_none():
    kind, reset = parse_reset(raw_text="Weekly usage limit reached, resets 5pm", now=NOW)
    assert kind == WEEKLY
    assert reset is None


def test_wallclock_already_passed_rolls_to_tomorrow():
    kind, reset = parse_reset(raw_text="resets 9am", now=NOW)  # 9am < 2pm now
    assert reset == (NOW + timedelta(days=1)).replace(hour=9, minute=0)


def test_event_epoch_seconds():
    ts = int((NOW + timedelta(hours=3)).timestamp())
    _, reset = parse_reset(raw_event={"type": "rate_limit_event", "resetsAt": ts}, now=NOW)
    assert reset == NOW + timedelta(hours=3)


def test_event_epoch_milliseconds():
    ts_ms = int((NOW + timedelta(hours=1)).timestamp() * 1000)
    _, reset = parse_reset(raw_event={"resetAt": ts_ms}, now=NOW)
    assert reset == NOW + timedelta(hours=1)


def test_event_retry_after_duration():
    _, reset = parse_reset(raw_event={"retryAfter": 900}, now=NOW)
    assert reset == NOW + timedelta(seconds=900)


def test_event_nested_payload_and_weekly_keyword():
    ts = int((NOW + timedelta(hours=2)).timestamp())
    kind, reset = parse_reset(
        raw_event={"error": {"rate_limit": {"reset": ts}}, "note": "weekly limit"}, now=NOW
    )
    assert kind == WEEKLY
    assert reset == NOW + timedelta(hours=2)


def test_event_iso_string():
    iso = (NOW + timedelta(hours=4)).isoformat()
    _, reset = parse_reset(raw_event={"resetsAt": iso}, now=NOW)
    assert reset == NOW + timedelta(hours=4)


def test_empty_signal_is_unknown_none():
    assert parse_reset(now=NOW) == (UNKNOWN, None)


def test_absurd_future_epoch_is_clamped_to_none():
    ts = int((NOW + timedelta(days=40)).timestamp())
    _, reset = parse_reset(raw_event={"resetsAt": ts}, now=NOW)
    assert reset is None


def test_detect_limit_kind_variants():
    assert detect_limit_kind(None, "5-hour session limit") == SESSION
    assert detect_limit_kind(None, "weekly cap reached") == WEEKLY
    assert detect_limit_kind({"scope": "five hour"}, None) == SESSION
    assert detect_limit_kind(None, "some other error") == UNKNOWN


def test_event_weekly_string_value_is_ambiguous_none():
    # A STRUCTURED weekly payload with a wall-clock reset string must not guess a
    # concrete time (the day-ambiguity guard applies to raw_event strings too —
    # without the fix, _reset_from_event would parse "resets 5pm" to a concrete
    # time despite the weekly kind).
    kind, reset = parse_reset(raw_event={"limit_type": "weekly", "reset": "resets 5pm"}, now=NOW)
    assert kind == WEEKLY
    assert reset is None


def test_event_session_string_value_resolves():
    kind, reset = parse_reset(raw_event={"limit_type": "session", "reset": "resets 5pm"}, now=NOW)
    assert kind == SESSION
    assert reset == NOW.replace(hour=17, minute=0)


def test_event_preferred_over_prose_when_both_present():
    # Structured event wins; prose weekly-ambiguity does not suppress it.
    ts = int((NOW + timedelta(hours=1)).timestamp())
    _, reset = parse_reset(raw_event={"resetsAt": ts}, raw_text="weekly limit resets 5pm", now=NOW)
    assert reset == NOW + timedelta(hours=1)


def test_never_raises_on_garbage():
    # Bad types must degrade, not crash.
    kind, reset = parse_reset(raw_event={"resetsAt": object()}, raw_text=None, now=NOW)
    assert reset is None
