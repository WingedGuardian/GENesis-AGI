"""Dashboard internet probe (_internet_probe_entry) + infra:internet_down alert."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.resilience import network_config, network_state

# NOTE: the `snapshots` package __init__ re-exports the `infrastructure`
# FUNCTION, shadowing the submodule attribute — so `import ...infrastructure as
# x` would bind the function. import_module returns the real module from
# sys.modules, giving access to the module-level probe helpers.
infra_mod = importlib.import_module("genesis.observability.snapshots.infrastructure")


def _fresh(state: str, *, since_s: float = 120.0) -> dict:
    now = datetime.now(UTC)
    return {
        "state": state,
        "since": (now - timedelta(seconds=since_s)).isoformat(),
        "cause": "all_fail",
        "last_probe_at": (now - timedelta(seconds=5)).isoformat(),
        "window_open": state == "OFFLINE",
    }


@pytest.fixture()
def patched_store(monkeypatch):
    """Drive _internet_probe_entry from an in-memory snapshot, hermetically."""
    holder = {"snap": None}
    monkeypatch.setattr(network_state, "read_state", lambda path=None: holder["snap"])
    monkeypatch.setattr(
        network_config,
        "structural",
        lambda cfg=None: network_config.NetworkTuning(
            dns_tcp_anchors=("a",),
            ip_anchors=("b",),
            probe_port=443,
            probe_timeout_s=3,
            fast_cadence_s=20,
            steady_cadence_s=120,
            offline_all_fail_rounds=2,
            online_clean_rounds=3,
            stable_online_s=300,
            merge_gap_s=600,
        ),
    )
    return holder


def test_absent_store_omits_key(patched_store):
    patched_store["snap"] = None
    assert infra_mod._internet_probe_entry() is None


def test_normal_is_healthy(patched_store):
    patched_store["snap"] = _fresh("NORMAL")
    assert infra_mod._internet_probe_entry()["status"] == "healthy"


def test_offline_is_down_with_message(patched_store):
    patched_store["snap"] = _fresh("OFFLINE", since_s=1500)
    entry = infra_mod._internet_probe_entry()
    assert entry["status"] == "down"
    assert "offline" in entry["message"]
    assert "for" in entry["message"]  # duration rendered


def test_degraded_is_degraded(patched_store):
    patched_store["snap"] = _fresh("DEGRADED")
    assert infra_mod._internet_probe_entry()["status"] == "degraded"


def test_no_anchors_renders_not_configured(patched_store):
    # Misconfigured sentinel (zero anchors) surfaces explicitly, never a green.
    snap = _fresh("NORMAL")
    snap["cause"] = "no_anchors"
    patched_store["snap"] = snap
    entry = infra_mod._internet_probe_entry()
    assert entry["status"] == "unknown"
    assert "not configured" in entry["message"]


def test_stale_probe_is_unknown(patched_store):
    snap = _fresh("OFFLINE")
    snap["last_probe_at"] = (datetime.now(UTC) - timedelta(seconds=10_000)).isoformat()
    patched_store["snap"] = snap
    entry = infra_mod._internet_probe_entry()
    assert entry["status"] == "unknown"  # never asserts a false down/green when stale


def test_since_duration_formatting():
    now = datetime.now(UTC)
    assert infra_mod._internet_since_duration(None) is None
    assert infra_mod._internet_since_duration("garbage") is None
    m = infra_mod._internet_since_duration((now - timedelta(minutes=5)).isoformat())
    assert m == "for 5m"
    h = infra_mod._internet_since_duration((now - timedelta(hours=1, minutes=20)).isoformat())
    assert h == "for 1h 20m"


@pytest.mark.asyncio
async def test_internet_down_alert_fires_only_on_down():
    """infra:internet_down is WARNING and fires only for status 'down'."""
    import genesis.mcp.health_mcp as health_mcp_mod
    from genesis.mcp.health_mcp import _impl_health_alerts

    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {"internet": {"status": "down", "message": "offline for 20m"}},
        "queues": {},
        "awareness": {},
    }
    old, old_hist = health_mcp_mod._service, health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        hits = [a for a in alerts if a["id"] == "infra:internet_down"]
        assert len(hits) == 1
        assert hits[0]["severity"] == "WARNING"
        assert "offline for 20m" in hits[0]["message"]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_hist


@pytest.mark.asyncio
async def test_internet_degraded_does_not_alert():
    import genesis.mcp.health_mcp as health_mcp_mod
    from genesis.mcp.health_mcp import _impl_health_alerts

    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {"internet": {"status": "degraded", "message": "degraded for 2m"}},
        "queues": {},
        "awareness": {},
    }
    old, old_hist = health_mcp_mod._service, health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert not [a for a in alerts if a["id"] == "infra:internet_down"]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_hist
