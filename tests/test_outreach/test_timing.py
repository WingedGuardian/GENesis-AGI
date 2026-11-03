"""Tests for genesis.outreach.timing.resolve_preferred_timing.

``now`` and ``tz`` are injected explicitly so every case is deterministic —
no dependence on the host clock or install timezone.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta

import pytest

from genesis.outreach.timing import (
    TimingParseError,
    resolve_preferred_timing,
)

# A fixed mid-day UTC instant: 2026-03-14 15:00 UTC is 10:00 in America/Chicago
# (CDT, DST already started 2026-03-08).
NOW = datetime(2026, 3, 14, 15, 0, 0, tzinfo=UTC)
# The rework spec's anchor: 10:00 CDT on 2026-06-10.
SPEC_NOW = datetime(2026, 6, 10, 15, 0, 0, tzinfo=UTC)
# Spring-forward morning: 01:30 CST on 2026-03-08 (the 02:00→03:00 jump is ahead).
SPRING_FORWARD = datetime(2026, 3, 8, 7, 30, 0, tzinfo=UTC)
# Autumn rollback morning: 00:30 CDT on 2026-11-01 (the 02:00→01:00 fold is ahead).
FALL_BACK = datetime(2026, 11, 1, 5, 30, 0, tzinfo=UTC)

CHICAGO = "America/Chicago"
PHOENIX = "America/Phoenix"  # no DST — the non-DST zone required by the spec


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _local_wall(dt: datetime, tz: str) -> str:
    return dt.astimezone(zoneinfo.ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


# ── ISO-8601 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tz", ["UTC", CHICAGO])
def test_iso_aware_unchanged(tz):
    raw = "2026-03-20T18:30:00+00:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz=tz)) == raw


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        ("UTC", "2026-03-20T18:30:00+00:00"),
        # 18:30 naive Chicago (CDT, UTC-5) → 23:30 UTC.
        (CHICAGO, "2026-03-20T23:30:00+00:00"),
    ],
)
def test_iso_naive_uses_user_timezone(tz, expected):
    raw = "2026-03-20T18:30:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz=tz)) == expected


def test_iso_past_aware_unchanged():
    """A past ISO value keeps today's behaviour — not an error."""
    raw = "2020-01-01T12:00:00+00:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz="UTC")) == raw


def test_naive_iso_ambiguous_across_dst_raises():
    """A naive ISO local time that exists twice (rollback) cannot pick a side
    silently — it is refused like other unresolvable input."""
    now = datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC)
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(
            "2026-11-01T01:30:00", now=now, tz=CHICAGO
        )


def test_naive_iso_in_dst_gap_raises():
    """02:30 on the spring-forward day never exists in Chicago — refuse, never
    silently shift to 03:30."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(
            "2026-03-08T02:30:00", now=SPRING_FORWARD - timedelta(hours=12), tz=CHICAGO
        )


def test_iso_out_of_range_raises_timing_error():
    """datetime.max overflows the UTC conversion — a TimingParseError, never a
    bare OverflowError escaping the tool's error handling."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("9999-12-31T23:59:59", now=NOW, tz=CHICAGO)


# ── Clock time ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected_local"),
    [
        # From 10:00 CDT on 2026-06-10 — the spec's table.
        ("at 9", "2026-06-11 09:00"),   # next 09:00, not September
        ("9a", "2026-06-11 09:00"),     # next 09:00, not the year 2035
        ("9p", "2026-06-10 21:00"),     # next 21:00, not 9 minutes out
        ("9am", "2026-06-11 09:00"),
        ("9:30pm", "2026-06-10 21:30"),
        ("14:00", "2026-06-10 14:00"),
    ],
)
def test_clock_time_spec_table(raw, expected_local):
    got = resolve_preferred_timing(raw, now=SPEC_NOW, tz=CHICAGO)
    assert _local_wall(got, CHICAGO) == expected_local


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        (CHICAGO, "2026-03-15T14:00:00+00:00"),
        ("UTC", "2026-03-15T09:00:00+00:00"),
    ],
)
def test_bare_clock_time_prefers_future(tz, expected):
    assert _iso(resolve_preferred_timing("9am", now=NOW, tz=tz)) == expected


@pytest.mark.parametrize("bad", ["25:00", "9:99", "13am", "0:45pm"])
def test_invalid_clock_raises(bad):
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=NOW, tz="UTC")


def test_bare_clock_dst_rollback_picks_second_occurrence():
    """On the autumn rollback morning, 01:30 exists twice in Chicago. From the
    first 01:45 (06:45 UTC), "1:30am" resolves to the still-future SECOND
    occurrence (07:30 UTC, 45 min away) — not next day's."""
    now = datetime(2026, 11, 1, 6, 45, 0, tzinfo=UTC)
    assert _iso(
        resolve_preferred_timing("1:30am", now=now, tz=CHICAGO)
    ) == "2026-11-01T07:30:00+00:00"


def test_bare_clock_in_dst_gap_skips_to_next_day():
    """02:30 never exists on the 2026-03-08 spring-forward in Chicago, so a
    "2:30am" asked that morning resolves to the next day's occurrence."""
    now = datetime(2026, 3, 8, 9, 0, 0, tzinfo=UTC)  # 03:00 CST, post-gap
    assert _iso(
        resolve_preferred_timing("2:30am", now=now, tz=CHICAGO)
    ) == "2026-03-09T07:30:00+00:00"


def test_bare_clock_asked_evening_before_gap_finds_day_after():
    """"2:30am" asked on the evening before the spring-forward: today is past,
    tomorrow is the gap — the three-day search lands on the day after."""
    now = datetime(2026, 3, 8, 5, 0, 0, tzinfo=UTC)  # 23:00 CST Mar 7
    assert _iso(
        resolve_preferred_timing("2:30am", now=now, tz=CHICAGO)
    ) == "2026-03-09T07:30:00+00:00"


# ── Elapsed time (added to the UTC clock) ────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "2h", "in 2h", "2 hrs", "2 hours", "after 2 hours", "2 hours later",
        "in 2 hours.", "in 120m", "2 hours from now",
    ],
)
@pytest.mark.parametrize(
    ("now", "tz"),
    [(SPRING_FORWARD, CHICAGO), (FALL_BACK, CHICAGO), (SPRING_FORWARD, PHOENIX)],
)
def test_elapsed_two_hours_is_utc_delta(raw, now, tz):
    """+2h on the clock, on both sides of a clock change — never the local
    wall-clock arithmetic that lands an hour off."""
    assert _iso(resolve_preferred_timing(raw, now=now, tz=tz)) == _iso(
        now + timedelta(hours=2)
    )


@pytest.mark.parametrize(
    ("now", "expected_delta"),
    [
        (SPRING_FORWARD, timedelta(minutes=90)),
        (FALL_BACK, timedelta(minutes=90)),
    ],
)
def test_elapsed_1h30m_is_utc_delta(now, expected_delta):
    assert _iso(
        resolve_preferred_timing("in 1h30m", now=now, tz=CHICAGO)
    ) == _iso(now + expected_delta)


def test_elapsed_an_hour():
    assert _iso(
        resolve_preferred_timing("in an hour", now=NOW, tz="UTC")
    ) == _iso(NOW + timedelta(hours=1))


def test_elapsed_decimal_hours():
    assert _iso(
        resolve_preferred_timing("1.5 hours", now=NOW, tz="UTC")
    ) == _iso(NOW + timedelta(hours=1.5))


@pytest.mark.parametrize(
    "bad",
    ["in 5", "in two hours", "in -2 hours", "1430", "in 99999999999 hours"],
)
def test_elapsed_garbage_refused(bad):
    """Bare counts, number words, negatives, ambiguous HHMM, and overflow all
    refuse — dateparser must never guess at them."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=SPEC_NOW, tz=CHICAGO)


# ── Calendar phrases (dateparser, gated) ─────────────────────────────────


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        (CHICAGO, "2026-03-15T14:00:00+00:00"),
        ("UTC", "2026-03-15T09:00:00+00:00"),
    ],
)
def test_tomorrow_9am(tz, expected):
    assert _iso(resolve_preferred_timing("tomorrow 9am", now=NOW, tz=tz)) == expected


def test_tomorrow_9am_across_dst_boundary():
    """'tomorrow 9am' crossing the rollback resolves in local time: 9am CST."""
    now = datetime(2026, 10, 31, 15, 0, 0, tzinfo=UTC)
    assert _iso(
        resolve_preferred_timing("tomorrow 9am", now=now, tz=CHICAGO)
    ) == "2026-11-01T15:00:00+00:00"


def test_in_2_days_keeps_local_clock_time():
    """`in 2 days` is calendar, not elapsed: it lands at the same local clock
    time two days out (CDT → +49h UTC across the spring forward)."""
    now = datetime(2026, 3, 7, 7, 30, 0, tzinfo=UTC)  # 01:30 CST Mar 7
    got = resolve_preferred_timing("in 2 days", now=now, tz=CHICAGO)
    assert _local_wall(got, CHICAGO) == "2026-03-09 01:30"


def test_next_friday_3pm():
    """A weekday phrase is calendar-shaped and supported — decided question in
    the spec; see the PR body for the reasoning."""
    got = resolve_preferred_timing("next friday 3pm", now=SPEC_NOW, tz=CHICAGO)
    assert got > SPEC_NOW
    local = got.astimezone(zoneinfo.ZoneInfo(CHICAGO))
    assert local.weekday() == 4  # Friday
    assert (local.hour, local.minute) == (15, 0)


def test_tomorrow_clock_in_dst_gap_refused():
    """'tomorrow 2:30am' names a wall time that does not exist — refused, not
    silently shifted an hour."""
    now = datetime(2026, 3, 8, 5, 0, 0, tzinfo=UTC)  # evening before, CST
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("tomorrow 2:30am", now=now, tz=CHICAGO)


def test_tomorrow_clock_in_dst_fold_refused():
    """'tomorrow 1:30am' across the rollback exists twice — refused, not a
    silently picked side."""
    now = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)  # 22:00 CDT Oct 31
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("tomorrow 1:30am", now=now, tz=CHICAGO)


def test_calendar_result_beyond_horizon_refused():
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(
            "december 25 2030", now=SPEC_NOW, tz=CHICAGO
        )


# ── The refusal contract ─────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "soonish", "banana", "yesterday"])
def test_unparseable_raises(bad):
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=NOW, tz="UTC")


def test_result_is_utc_iso_and_lexicographically_comparable():
    later = resolve_preferred_timing("tomorrow 9am", now=NOW, tz="UTC")
    earlier = resolve_preferred_timing("in 2 hours", now=NOW, tz="UTC")
    a, b = _iso(earlier), _iso(later)
    assert a.endswith("+00:00") and b.endswith("+00:00")
    assert a < b  # lexicographic order == chronological order for UTC ISO


def test_dangling_and_refused():
    """'in 2 hours and' is an unfinished conjunction — a queued receipt for a
    truncated duration is exactly the guess this contract refuses."""
    for bad in ("in 2 hours and", "and 2 hours", "in and 2 hours"):
        with pytest.raises(TimingParseError):
            resolve_preferred_timing(bad, now=NOW, tz="UTC")
    # The connector is still valid between two durations.
    assert resolve_preferred_timing(
        "in 2 hours and 30 minutes", now=NOW, tz="UTC"
    ) == NOW + timedelta(hours=2, minutes=30)


def test_named_zone_phrase_resolves_in_that_zone():
    """'next friday 3pm UTC' from a Chicago user must land at 15:00 UTC —
    the named zone governs; the local fold check does not second-guess it."""
    got = resolve_preferred_timing("next friday 3pm UTC", now=SPEC_NOW, tz=CHICAGO)
    assert got == datetime(2026, 6, 19, 15, 0, tzinfo=UTC)
    got = resolve_preferred_timing("friday 3pm +0530", now=SPEC_NOW, tz=CHICAGO)
    assert got == datetime(2026, 6, 12, 9, 30, tzinfo=UTC)


def test_abbreviated_zone_refused():
    """CST is four different offsets worldwide — refuse rather than guess."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("tomorrow 9am CST", now=SPEC_NOW, tz=CHICAGO)
