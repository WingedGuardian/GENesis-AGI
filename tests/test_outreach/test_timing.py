"""Tests for genesis.outreach.timing.resolve_preferred_timing.

``now`` and ``tz`` are injected explicitly so every case is deterministic —
no dependence on the host clock or install timezone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.outreach.timing import (
    TimingParseError,
    resolve_preferred_timing,
)

# A fixed mid-day UTC instant: 2026-03-14 15:00 UTC is 10:00 in America/Chicago
# (CDT, DST already started 2026-03-08).
NOW = datetime(2026, 3, 14, 15, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


@pytest.mark.parametrize("tz", ["UTC", "America/Chicago"])
def test_iso_aware_unchanged(tz):
    raw = "2026-03-20T18:30:00+00:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz=tz)) == raw


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        # 18:30 naive UTC → 18:30 UTC.
        ("UTC", "2026-03-20T18:30:00+00:00"),
        # 18:30 naive Chicago (CDT, UTC-5) → 23:30 UTC.
        ("America/Chicago", "2026-03-20T23:30:00+00:00"),
    ],
)
def test_iso_naive_uses_user_timezone(tz, expected):
    raw = "2026-03-20T18:30:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz=tz)) == expected


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        # Next local day 09:00: Chicago is CDT (UTC-5) → 14:00 UTC.
        ("America/Chicago", "2026-03-15T14:00:00+00:00"),
        ("UTC", "2026-03-15T09:00:00+00:00"),
    ],
)
def test_tomorrow_9am(tz, expected):
    assert _iso(resolve_preferred_timing("tomorrow 9am", now=NOW, tz=tz)) == expected


def test_tomorrow_9am_across_dst_boundary():
    """A 'tomorrow 9am' crossing a DST transition resolves in local time.

    2026-11-01 02:00 ends DST in America/Chicago (CDT -5 → CST -6). From
    2026-10-31 15:00 UTC, 'tomorrow 9am' is 9am CST = 15:00 UTC Nov 1.
    """
    now = datetime(2026, 10, 31, 15, 0, 0, tzinfo=UTC)
    assert _iso(
        resolve_preferred_timing("tomorrow 9am", now=now, tz="America/Chicago")
    ) == "2026-11-01T15:00:00+00:00"


@pytest.mark.parametrize("tz", ["UTC", "America/Chicago"])
def test_in_two_hours(tz):
    assert _iso(resolve_preferred_timing("in 2 hours", now=NOW, tz=tz)) == _iso(
        NOW + timedelta(hours=2)
    )


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        # NOW is 10:00 local in Chicago; bare "9am" prefers future → next day
        # 09:00 CDT = 14:00 UTC.
        ("America/Chicago", "2026-03-15T14:00:00+00:00"),
        # NOW is 15:00 UTC; bare "9am" → next day 09:00 UTC.
        ("UTC", "2026-03-15T09:00:00+00:00"),
    ],
)
def test_bare_clock_time_prefers_future(tz, expected):
    assert _iso(resolve_preferred_timing("9am", now=NOW, tz=tz)) == expected


@pytest.mark.parametrize("bad", ["", "   ", "soonish", "banana"])
def test_unparseable_raises(bad):
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=NOW, tz="UTC")


def test_result_is_utc_iso_and_lexicographically_comparable():
    later = resolve_preferred_timing("tomorrow 9am", now=NOW, tz="UTC")
    earlier = resolve_preferred_timing("in 2 hours", now=NOW, tz="UTC")
    a, b = _iso(earlier), _iso(later)
    assert a.endswith("+00:00") and b.endswith("+00:00")
    assert a < b  # lexicographic order == chronological order for UTC ISO


@pytest.mark.parametrize("bad", ["25:00", "9:99", "13am", "0:45pm"])
def test_invalid_clock_raises(bad):
    """Out-of-range clock fields raise TimingParseError, never a bare ValueError
    that would escape outreach_send's error handling."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=NOW, tz="UTC")


def test_bare_clock_dst_rollback_picks_second_occurrence():
    """On the autumn rollback morning, 01:30 exists twice in Chicago. From the
    first 01:45 (06:45 UTC), "1:30am" resolves to the still-future SECOND
    occurrence (07:30 UTC, 45 min away) — not next day's."""
    now = datetime(2026, 11, 1, 6, 45, 0, tzinfo=UTC)
    assert _iso(
        resolve_preferred_timing("1:30am", now=now, tz="America/Chicago")
    ) == "2026-11-01T07:30:00+00:00"


def test_naive_iso_ambiguous_across_dst_raises():
    """A naive ISO local time that exists twice (rollback) cannot pick a side
    silently — it is refused like other unresolvable input."""
    now = datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC)
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(
            "2026-11-01T01:30:00", now=now, tz="America/Chicago"
        )


def test_bare_clock_in_dst_gap_skips_to_next_day():
    """02:30 never exists on the 2026-03-08 spring-forward in Chicago, so a
    "2:30am" asked that morning resolves to the next day's occurrence
    (07:30 UTC, CDT -5)."""
    now = datetime(2026, 3, 8, 9, 0, 0, tzinfo=UTC)  # 03:00 CST, post-gap
    assert _iso(
        resolve_preferred_timing("2:30am", now=now, tz="America/Chicago")
    ) == "2026-03-09T07:30:00+00:00"
