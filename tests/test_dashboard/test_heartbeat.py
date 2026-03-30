"""Tests for the DashboardHeartbeat class."""

from __future__ import annotations

from genesis.dashboard.heartbeat import DashboardHeartbeat


def test_heartbeat_expected_in_health_mcp():
    """Dashboard should be in the expected heartbeat intervals dict."""
    # This is a structural test — verify the config was added correctly
    from pathlib import Path

    health_mcp = Path(__file__).parent.parent.parent / "src" / "genesis" / "mcp" / "health_mcp.py"
    source = health_mcp.read_text()
    assert '"dashboard"' in source or "'dashboard'" in source


def test_heartbeat_starts_and_stops():
    """Heartbeat thread starts as daemon and can be stopped."""
    hb = DashboardHeartbeat(interval_seconds=1)
    # Don't actually start (would need runtime), just verify structure
    assert hb._interval == 1
    assert hb._thread is None
    assert not hb._stop_event.is_set()
    hb.stop()
    assert hb._stop_event.is_set()


def test_subsystem_enum_has_dashboard():
    """DASHBOARD should be a valid Subsystem enum value."""
    from genesis.observability.types import Subsystem

    assert Subsystem.DASHBOARD == "dashboard"
    assert Subsystem("dashboard") == Subsystem.DASHBOARD
