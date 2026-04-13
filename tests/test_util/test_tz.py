"""Tests for genesis.util.tz — timezone display helpers."""

from zoneinfo import ZoneInfo

import pytest

import genesis.util.tz as tz_module
from genesis.util.tz import fmt, fmt_short


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
