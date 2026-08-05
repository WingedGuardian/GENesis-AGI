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
    # BARE clock (no weekday) for a weekly cap stays day-ambiguous → None.
    kind, reset = parse_reset(raw_text="Weekly usage limit reached, resets 5pm", now=NOW)
    assert kind == WEEKLY
    assert reset is None


def test_prose_weekly_weekday_resolves_next_occurrence():
    """A weekly cap that NAMES a weekday is unambiguous — resolve the next
    occurrence instead of dropping to None (so the resume waits for the real
    reset rather than retrying on the cadence floor for days)."""
    now = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)  # Wednesday
    kind, reset = parse_reset(raw_text="You've hit your weekly limit · resets Monday 9am", now=now)
    assert kind == WEEKLY
    # Next Monday after Wed 2026-08-05 is 2026-08-10, 09:00 (no tz hint → UTC).
    assert reset == datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)


def test_prose_weekly_weekday_honors_tz_hint():
    """Weekday resolution is account-zone-aware when the CLI names the zone."""
    now = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)  # Wednesday
    _, reset = parse_reset(
        raw_text="hit your weekly limit · resets Monday 9am (America/Los_Angeles)",
        now=now,
    )
    # Monday 09:00 PDT (UTC-7) == 2026-08-10 16:00 UTC.
    assert reset == datetime(2026, 8, 10, 16, 0, 0, tzinfo=UTC)


def test_prose_weekday_same_day_already_passed_rolls_a_week():
    """Named day == today but the time already passed → next week, not today."""
    now = datetime(2026, 8, 5, 20, 0, 0, tzinfo=UTC)  # Wednesday 20:00
    _, reset = parse_reset(raw_text="weekly limit resets Wednesday 9am", now=now)
    assert reset == datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)


def test_wallclock_already_passed_rolls_to_tomorrow():
    kind, reset = parse_reset(raw_text="resets 9am", now=NOW)  # 9am < 2pm now
    assert reset == (NOW + timedelta(days=1)).replace(hour=9, minute=0)


def test_prose_weekday_future_this_week_no_wrap():
    """A weekday later this week resolves to this week (no spurious +7)."""
    wed = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)  # Wednesday
    _, reset = parse_reset(raw_text="weekly limit resets Friday 5pm", now=wed)
    assert reset == datetime(2026, 8, 7, 17, 0, 0, tzinfo=UTC)  # this Friday


def test_prose_weekday_full_name_and_comma():
    """Full weekday names and a comma separator ("Monday, 9am") both parse."""
    wed = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
    _, full = parse_reset(raw_text="weekly limit resets Monday, 9am", now=wed)
    _, abbrev = parse_reset(raw_text="weekly limit resets Mon 9am", now=wed)
    assert full == abbrev == datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)


def test_prose_weekday_prefix_word_is_not_a_weekday():
    """A word merely STARTING with a day prefix (monthly→mon, friendly→fri) must
    NOT be parsed as that weekday — it falls through to the bare-clock branch,
    which is day-ambiguous for a weekly cap → None (not a wrong Monday)."""
    wed = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
    assert parse_reset(raw_text="weekly limit resets monthly at 9am", now=wed) == (WEEKLY, None)
    assert parse_reset(raw_text="weekly limit, friendly note, resets 9am", now=wed) == (
        WEEKLY,
        None,
    )


def test_prose_wallclock_honors_tz_hint():
    """A trailing (IANA/Zone) hint is the account's local zone — resolve the
    wall-clock there, not in the server's UTC. Regression for the live-captured
    session-limit message that parsed ~hours off without this.
    """
    # 2026-08-03 09:00 UTC == 02:00 America/Los_Angeles (PDT, UTC-7). The message
    # says the session resets at 4:10am PT → 11:10 UTC the SAME day (~1h out in
    # wall terms), NOT the naive-UTC parse of 04:10 UTC which, being already past
    # 09:00, would roll to the next day (hours off).
    now = datetime(2026, 8, 3, 9, 0, 0, tzinfo=UTC)
    kind, reset = parse_reset(
        raw_text="You've hit your session limit · resets 4:10am (America/Los_Angeles)",
        now=now,
    )
    assert kind == SESSION
    assert reset == datetime(2026, 8, 3, 11, 10, 0, tzinfo=UTC)


def test_prose_wallclock_tz_hint_rolls_to_tomorrow_in_zone():
    """When the account-zone wall-clock has already passed, roll a day — the
    comparison happens in the account zone, not UTC."""
    # 2026-08-03 15:00 UTC == 08:00 PT. A 4:10am PT reset already passed today in
    # PT → next day 4:10am PT == 2026-08-04 11:10 UTC.
    now = datetime(2026, 8, 3, 15, 0, 0, tzinfo=UTC)
    _, reset = parse_reset(
        raw_text="hit your session limit · resets 4:10am (America/Los_Angeles)",
        now=now,
    )
    assert reset == datetime(2026, 8, 4, 11, 10, 0, tzinfo=UTC)


def test_prose_wallclock_no_tz_hint_unchanged():
    """No zone hint → parse in ``now``'s tz exactly as before (unchanged path)."""
    kind, reset = parse_reset(raw_text="Session limit — resets 5pm", now=NOW)
    assert kind == SESSION
    assert reset == NOW.replace(hour=17, minute=0)


def test_prose_wallclock_invalid_tz_hint_falls_back():
    """An unresolvable zone name must NOT raise — fall back to naive parsing."""
    kind, reset = parse_reset(raw_text="session limit resets 5pm (Not/AZone)", now=NOW)
    assert kind == SESSION
    assert reset == NOW.replace(hour=17, minute=0)


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
