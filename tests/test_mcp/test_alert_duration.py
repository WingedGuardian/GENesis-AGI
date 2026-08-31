"""Alerts carry how LONG they have been firing, not just that they fire.

The 2026-08-28/29 provider outage was invisible for 3 days not because nothing
detected it but because every surface renders instantaneous state. `alert_events`
has carried `created_at`/`resolved_at` with 90-day retention the whole time and
had ZERO production readers (`list_recent()` is called only by tests).

Enriching the alert MESSAGE is deliberate: the dashboard banner, the morning
report and the Telegram path all render `message`, so one change gives every
surface a duration without adding a widget to any of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.mcp.health.errors import _apply_ongoing_duration, _ongoing_for

_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _ago(**kw) -> str:
    return (_NOW - timedelta(**kw)).isoformat()


class TestOngoingFor:
    def test_multi_day_outage_reads_in_days_and_hours(self):
        assert _ongoing_for(_ago(days=3, hours=4), _NOW) == "3d 4h"

    def test_sub_day_reads_in_hours(self):
        assert _ongoing_for(_ago(hours=5), _NOW) == "5h"

    def test_whole_days_omit_a_zero_hour(self):
        assert _ongoing_for(_ago(days=2), _NOW) == "2d"

    def test_young_alert_is_not_decorated(self):
        """Under the floor, a duration is noise — most alerts flap briefly."""
        assert _ongoing_for(_ago(minutes=20), _NOW) is None

    def test_unparseable_timestamp_is_not_decorated(self):
        """Never raise into the alert path; a bad row just gets no suffix."""
        assert _ongoing_for("not-a-timestamp", _NOW) is None
        assert _ongoing_for("", _NOW) is None
        assert _ongoing_for(None, _NOW) is None

    def test_future_timestamp_is_not_decorated(self):
        """Clock skew must not produce a negative or absurd duration."""
        assert _ongoing_for((_NOW + timedelta(hours=2)).isoformat(), _NOW) is None


class TestApplyOngoingDuration:
    def test_open_row_adds_duration_to_the_message(self):
        alerts = [{"id": "provider:x", "severity": "CRITICAL", "message": "Provider x is down"}]
        rows = {"provider:x": {"created_at": _ago(days=3, hours=4)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == "Provider x is down (ongoing for 3d 4h)"

    def test_alert_without_an_open_row_is_untouched(self):
        """First fire: the row is written AFTER this runs, so there is no age yet."""
        alerts = [{"id": "provider:x", "severity": "CRITICAL", "message": "Provider x is down"}]
        _apply_ongoing_duration(alerts, {}, now=_NOW)
        assert alerts[0]["message"] == "Provider x is down"

    def test_young_row_is_untouched(self):
        alerts = [{"id": "a", "severity": "WARNING", "message": "Blip"}]
        rows = {"a": {"created_at": _ago(minutes=5)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == "Blip"

    def test_is_idempotent(self):
        """_compute_alerts runs on every tick; a message must not accrete suffixes."""
        alerts = [{"id": "a", "severity": "CRITICAL", "message": "Down"}]
        rows = {"a": {"created_at": _ago(days=1)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"].count("ongoing for") == 1

    def test_a_broken_row_never_raises_into_the_alert_path(self):
        alerts = [{"id": "a", "severity": "CRITICAL", "message": "Down"}]
        _apply_ongoing_duration(alerts, {"a": {}}, now=_NOW)
        _apply_ongoing_duration(alerts, {"a": None}, now=_NOW)
        assert alerts[0]["message"] == "Down"
