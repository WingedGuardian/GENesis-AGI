"""Tests for genesis.util.tz — timezone display helpers."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import genesis.util.tz as tz_module
from genesis.util.tz import fmt, fmt_short, local_day_boundary, parse_utc_iso


@pytest.fixture()
def eastern_tz(monkeypatch):
    """Pin _USER_TZ to America/New_York for tests that check EST/EDT output."""
    monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("America/New_York"))


class TestFmt:
    def test_utc_to_est(self, eastern_tz):
        # 17:00 UTC = 12:00 EST (or 13:00 EDT)
        result = fmt("2026-01-15T17:00:00+00:00")
        assert "12:00" in result
        assert "EST" in result

    def test_utc_to_edt(self, eastern_tz):
        # 17:00 UTC in July = 13:00 EDT
        result = fmt("2026-07-15T17:00:00+00:00")
        assert "13:00" in result
        assert "EDT" in result

    def test_naive_assumed_utc(self, eastern_tz):
        result = fmt("2026-01-15T17:00:00")
        assert "12:00" in result
        assert "EST" in result

    def test_invalid_string_returns_original(self):
        assert fmt("not-a-date") == "not-a-date"

    def test_none_returns_unknown(self):
        assert fmt(None) == "unknown"  # type: ignore[arg-type]

    def test_empty_string_returns_unknown(self):
        assert fmt("") == "unknown"

    def test_custom_format(self, eastern_tz):
        result = fmt("2026-01-15T17:00:00+00:00", "%H:%M %Z")
        assert result == "12:00 EST"


class TestFmtShort:
    def test_short_format(self, eastern_tz):
        result = fmt_short("2026-01-15T17:00:00+00:00")
        assert result == "12:00 EST"


class TestParseUtcIso:
    def test_aware_string_preserved(self):
        dt = parse_utc_iso("2026-06-20T17:00:00+00:00")
        assert dt == datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
        assert dt.tzinfo is not None

    def test_naive_string_assumed_utc(self):
        dt = parse_utc_iso("2026-06-20T17:00:00")
        assert dt == datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
        assert dt.tzinfo is UTC

    def test_naive_result_is_aware_and_subtractable(self):
        # The core bug this guards against: naive value subtracted from aware now.
        dt = parse_utc_iso("2026-06-20T17:00:00")
        delta = datetime.now(UTC) - dt  # must not raise
        assert delta.total_seconds() >= 0

    def test_none_returns_none(self):
        assert parse_utc_iso(None) is None

    def test_empty_returns_none(self):
        assert parse_utc_iso("") is None

    def test_invalid_returns_none(self):
        assert parse_utc_iso("not-a-timestamp") is None


class TestLocalDayBoundary:
    """The day boundary must be LOCAL midnight (hour), not UTC midnight.

    Regression guard for the CC-session daily-reset bug: sessions reset at
    ~20:00 ET instead of local midnight because the boundary was computed in
    UTC. The boundary must also be returned UTC-aware so a string ``<``
    comparison against stored ``+00:00`` timestamps stays lexicographically
    correct (the query_stale trap).
    """

    def test_boundary_is_local_midnight_not_utc(self, eastern_tz):
        # now = 2026-08-24 03:00 UTC = 2026-08-23 23:00 EDT (still Aug 23 local).
        # Most recent LOCAL midnight = Aug 23 00:00 EDT = 2026-08-23 04:00 UTC.
        # The buggy UTC-midnight logic would give 2026-08-24 00:00 UTC.
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 8, 23, 4, 0, tzinfo=UTC)

    def test_boundary_returned_utc_aware_plus_zero_offset(self, eastern_tz):
        # The ISO-string trap: boundary must serialize with +00:00, never a
        # local offset, or the query_stale string comparison breaks.
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        boundary = local_day_boundary(0, now=now)
        assert boundary.utcoffset() == timedelta(0)
        assert boundary.isoformat().endswith("+00:00")

    def test_boundary_dst_winter_est(self, eastern_tz):
        # January: EST is UTC-5. now = 2026-01-15 04:00 UTC = 2026-01-14 23:00 EST.
        # Local midnight Jan 14 00:00 EST = 2026-01-14 05:00 UTC.
        now = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 1, 14, 5, 0, tzinfo=UTC)

    def test_boundary_just_after_local_midnight_is_today(self, eastern_tz):
        # now = 2026-08-24 05:00 UTC = 2026-08-24 01:00 EDT (just past local midnight).
        # Boundary = Aug 24 00:00 EDT = 2026-08-24 04:00 UTC (today's, no rollback).
        now = datetime(2026, 8, 24, 5, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 8, 24, 4, 0, tzinfo=UTC)

    def test_boundary_nonzero_hour(self, eastern_tz):
        # hour=6 (06:00 local). now = 2026-08-24 03:00 UTC = 2026-08-23 23:00 EDT.
        # Most recent 06:00 EDT = Aug 23 06:00 EDT = 2026-08-23 10:00 UTC.
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        assert local_day_boundary(6, now=now) == datetime(2026, 8, 23, 10, 0, tzinfo=UTC)

    def test_utc_install_unaffected(self, monkeypatch):
        # A UTC install: boundary IS UTC midnight (no behavior change).
        import genesis.util.tz as tz_module

        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("UTC"))
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 8, 24, 0, 0, tzinfo=UTC)

    def test_default_now_returns_recent_past_utc(self, eastern_tz):
        # Smoke test with real now: boundary is UTC-aware and in the past.
        boundary = local_day_boundary(0)
        assert boundary.tzinfo is not None
        assert boundary <= datetime.now(UTC)
