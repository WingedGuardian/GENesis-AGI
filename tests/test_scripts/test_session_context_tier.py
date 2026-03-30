"""Tests for activity tier computation in the SessionStart hook."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Load the script as a module (not a package — use importlib)
_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_session_context.py"
_spec = importlib.util.spec_from_file_location("genesis_session_context", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_compute_activity_tier = _mod._compute_activity_tier


def _session_data(hours_ago: float) -> dict:
    """Helper: build last_session_data with ended_at N hours ago."""
    ended = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {"ended_at": ended, "session_id": "test123"}


def test_no_session_data_returns_away():
    assert _compute_activity_tier(None) == "away"


def test_empty_ended_at_returns_away():
    assert _compute_activity_tier({"ended_at": ""}) == "away"


def test_invalid_ended_at_returns_away():
    assert _compute_activity_tier({"ended_at": "not-a-date"}) == "away"


def test_recent_session_returns_active():
    """Session ended 30 minutes ago -> active."""
    assert _compute_activity_tier(_session_data(0.5)) == "active"


def test_two_hour_boundary_returns_active():
    """Session ended 1h59m ago -> still active."""
    assert _compute_activity_tier(_session_data(1.9)) == "active"


def test_beyond_two_hours_returns_returning():
    """Session ended 3 hours ago, <3 sessions -> returning."""
    assert _compute_activity_tier(_session_data(3), 1) == "returning"


def test_many_sessions_overrides_gap():
    """5-hour gap but 3+ sessions in 24h -> active (frequency signal)."""
    assert _compute_activity_tier(_session_data(5), 3) == "active"
    assert _compute_activity_tier(_session_data(5), 5) == "active"


def test_beyond_24_hours_no_sessions_returns_away():
    """Session ended 30 hours ago, no recent sessions -> away."""
    assert _compute_activity_tier(_session_data(30), 0) == "away"


def test_beyond_24_hours_with_many_sessions_returns_active():
    """30h gap but 3+ sessions in 24h -> active (frequency overrides gap).

    In practice this is rare: the DB query counts sessions in the last 24h,
    so old sessions age out. But if the user had 3+ sessions today and
    the last_foreground_session.json is stale, frequency wins.
    """
    assert _compute_activity_tier(_session_data(30), 3) == "active"


def test_exactly_24_hours_returns_away():
    """24h+ gap returns away."""
    assert _compute_activity_tier(_session_data(24.1)) == "away"
