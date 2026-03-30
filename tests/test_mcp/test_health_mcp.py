"""Tests for health-mcp server — verify all tools are registered."""

from unittest.mock import AsyncMock, patch

import genesis.mcp.health_mcp as health_mcp_mod
from genesis.mcp.health_mcp import (
    _impl_health_alerts,
    _impl_health_status,
    _impl_session_set_effort,
    _impl_session_set_model,
    mcp,
)


async def test_all_tools_registered():
    tools = await mcp.get_tools()
    for name in [
        "health_status", "health_errors", "health_alerts",
        "session_set_model", "session_set_effort",
    ]:
        assert name in tools, f"Missing tool: {name}"


async def test_health_status_returns_dict_without_service():
    """Without a service wired, tools return graceful unavailable response."""
    tools = await mcp.get_tools()
    result = await tools["health_status"].fn()
    assert result["status"] == "unavailable"


async def test_health_status_includes_awareness():
    """health_status should include awareness section from snapshot."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {},
        "infrastructure": {},
        "queues": {},
        "cost": {},
        "surplus": {},
        "awareness": {"status": "healthy", "ticks_24h": 42},
        "outreach_stats": {"window": "7d"},
    }
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        result = await _impl_health_status()
        assert "awareness" in result
        assert result["awareness"]["ticks_24h"] == 42
        assert "outreach_stats" in result
    finally:
        health_mcp_mod._service = old


async def test_health_status_includes_provider_activity():
    """When activity tracker is wired, health_status includes provider_activity."""
    from genesis.observability.provider_activity import ProviderActivityTracker

    tracker = ProviderActivityTracker()
    tracker.record("ollama_embedding", latency_ms=50, success=True)

    mock_service = AsyncMock()
    mock_service.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {},
        "infrastructure": {},
        "queues": {},
        "cost": {},
        "surplus": {},
    }

    old_service = health_mcp_mod._service
    old_tracker = health_mcp_mod._activity_tracker
    try:
        health_mcp_mod._service = mock_service
        health_mcp_mod._activity_tracker = tracker

        result = await _impl_health_status()
        assert "provider_activity" in result
        assert isinstance(result["provider_activity"], list)
        assert len(result["provider_activity"]) == 1
        assert result["provider_activity"][0]["provider"] == "ollama_embedding"
        assert result["provider_activity"][0]["calls"] == 1
    finally:
        health_mcp_mod._service = old_service
        health_mcp_mod._activity_tracker = old_tracker


async def test_alert_fires_on_tick_overdue():
    """Awareness tick overdue >360s should fire CRITICAL alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {"time_since_last_tick_seconds": 900},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        tick_alerts = [a for a in alerts if a["id"] == "awareness:tick_overdue"]
        assert len(tick_alerts) == 1
        assert tick_alerts[0]["severity"] == "CRITICAL"
        assert "overdue" in tick_alerts[0]["message"].lower()
        assert ">360s" in tick_alerts[0]["message"]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_fires_on_stale_dead_letters():
    """Dead letters older than 1h should fire alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {"dead_letter_oldest_age_seconds": 7200},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        dl_alerts = [a for a in alerts if a["id"] == "queue:stale_dead_letters"]
        assert len(dl_alerts) == 1
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_fires_on_disk_low():
    """Disk <10% free should fire alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {"disk": {"free_pct": 7.2, "free_gb": 10.1}},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        disk_alerts = [a for a in alerts if a["id"] == "infra:disk_low"]
        assert len(disk_alerts) == 1
        assert disk_alerts[0]["severity"] == "CRITICAL"
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


# ── session_set_model / session_set_effort tests ─────────────────────────


async def test_session_set_model_invalid():
    result = await _impl_session_set_model("sess-1", "gpt4")
    assert "error" in result
    assert "Invalid model" in result["error"]


async def test_session_set_effort_invalid():
    result = await _impl_session_set_effort("sess-1", "turbo")
    assert "error" in result
    assert "Invalid effort" in result["error"]


async def test_session_set_model_no_service():
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = None
        result = await _impl_session_set_model("sess-1", "opus")
        assert "error" in result
        assert "not available" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_success():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=True) as mock_update:
            result = await _impl_session_set_model("sess-1", "opus")
            assert result["success"] is True
            assert result["model"] == "opus"
            mock_update.assert_awaited_once_with(
                mock_svc._db, "sess-1", model="opus",
            )
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_not_found():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=False):
            result = await _impl_session_set_model("bad-id", "opus")
            assert "error" in result
            assert "not found" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_effort_success():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=True) as mock_update:
            result = await _impl_session_set_effort("sess-1", "high")
            assert result["success"] is True
            assert result["effort"] == "high"
            mock_update.assert_awaited_once_with(
                mock_svc._db, "sess-1", effort="high",
            )
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_case_insensitive():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=True):
            result = await _impl_session_set_model("sess-1", "OPUS")
            assert result["success"] is True
            assert result["model"] == "opus"
    finally:
        health_mcp_mod._service = old


async def test_session_set_effort_no_service():
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = None
        result = await _impl_session_set_effort("sess-1", "high")
        assert "error" in result
        assert "not available" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_effort_not_found():
    mock_svc = AsyncMock()
    mock_svc._db = AsyncMock()
    old = health_mcp_mod._service
    try:
        health_mcp_mod._service = mock_svc
        with patch("genesis.db.crud.cc_sessions.update_model_effort", new_callable=AsyncMock, return_value=False):
            result = await _impl_session_set_effort("bad-id", "high")
            assert "error" in result
            assert "not found" in result["error"]
    finally:
        health_mcp_mod._service = old


async def test_session_set_model_empty_session_id():
    result = await _impl_session_set_model("", "opus")
    assert "error" in result
    assert "required" in result["error"].lower()


async def test_session_set_effort_empty_session_id():
    result = await _impl_session_set_effort("  ", "high")
    assert "error" in result
    assert "required" in result["error"].lower()
