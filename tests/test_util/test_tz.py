"""Tests for genesis.util.tz — timezone display helpers."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

import genesis.util.tz as tz_module
from genesis.util.tz import fmt, fmt_short, parse_utc_iso


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
