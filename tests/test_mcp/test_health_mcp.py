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


async def test_call_site_alert_fires_on_down():
    """A wired call site in DOWN status should emit a WARNING alert.

    DOWN = all provider circuit breakers open (transient, self-resolving).
    Severity is WARNING (Tier 3 / reflexes only), not CRITICAL (Tier 2).
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {
            "3_micro_reflection": {  # wired in _call_site_meta
                "status": "down",
                "active_provider": None,
            },
        },
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        down = [a for a in alerts if a["id"] == "call_site:3_micro_reflection"]
        assert len(down) == 1
        assert down[0]["severity"] == "WARNING"
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_call_site_alert_suppressed_for_disabled_status():
    """A call site in 'disabled' status (no API keys) MUST NOT emit an alert.

    Regression test for the call-site-11 spam loop: providers with no API key
    caused ghost-down status → CRITICAL alert → Sentinel wake every 5 min.
    Disabled is a config state, not an infrastructure alert.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {
            "11_user_model_synthesis": {
                "status": "disabled",
                "disabled_reason": "no_api_keys_configured",
            },
        },
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        call_site_alerts = [a for a in alerts if a["id"].startswith("call_site:")]
        assert call_site_alerts == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_call_site_alert_suppressed_for_unwired_groundwork():
    """A groundwork call site (meta.wired=False) MUST NOT emit an alert.

    Call sites with config but no runtime invocation are placeholders, not
    infrastructure. Alerting on them would be noise — the "call site" isn't
    actually part of any active code path.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {
            "10_cognitive_state": {  # meta.wired=False (planned for V4)
                "status": "down",
                "active_provider": None,
            },
        },
        "cc_sessions": {"background": {}},
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"].startswith("call_site:")] == []
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


async def test_alert_fires_on_guardian_heartbeat_stale():
    """Guardian probe status=down must emit guardian:heartbeat_stale CRITICAL.

    Part 8: the Guardian is the host-side safety net. Its heartbeat going
    stale means the container has lost external visibility on itself,
    which is a Tier 1 defense mechanism failure.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "down", "staleness_s": 612.5},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        guardian_alerts = [a for a in alerts if a["id"] == "guardian:heartbeat_stale"]
        assert len(guardian_alerts) == 1
        assert guardian_alerts[0]["severity"] == "CRITICAL"
        # Message surfaces staleness for context
        assert "612" in guardian_alerts[0]["message"]
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_skips_guardian_when_healthy():
    """Healthy Guardian must NOT emit any alert."""
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "healthy", "staleness_s": 5.2},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"] == "guardian:heartbeat_stale"] == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_skips_guardian_when_paused():
    """Guardian paused → probe returns 'degraded', NOT an alert.

    Genesis paused by the user is a legitimate quiet state, not a failure.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "degraded", "message": "Guardian paused"},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"] == "guardian:heartbeat_stale"] == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_alert_handles_missing_staleness_gracefully():
    """If the Guardian probe details don't include staleness_s, still fire
    the alert with a best-effort message.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {"background": {}},
        "infrastructure": {
            "guardian": {"status": "down", "message": "signal_dropped"},
        },
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        guardian_alerts = [a for a in alerts if a["id"] == "guardian:heartbeat_stale"]
        assert len(guardian_alerts) == 1
        # Still emits; doesn't crash on missing staleness_s
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


# ── CC quota / rate-limit cross-check tests (Part 9b) ───────────────────


async def test_cc_quota_exhausted_suppressed_when_bg_healthy():
    """If the resilience state machine says RATE_LIMITED but the
    background budget tracker reports healthy, suppress the alert.
    The state machine is stale — the budget tracker is source of truth.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {
            "realtime_status": "RATE_LIMITED",
            "background": {"status": "healthy", "active": 0, "hourly_budget": "1/20"},
        },
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        assert [a for a in alerts if a["id"] == "cc:quota_exhausted"] == []
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_cc_quota_exhausted_fires_when_bg_also_throttled():
    """If BOTH the state machine and the budget tracker agree CC is
    degraded, the alert fires (at WARNING, not CRITICAL).
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {
            "realtime_status": "RATE_LIMITED",
            "background": {"status": "throttled", "active": 0, "hourly_budget": "20/20"},
        },
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        quota = [a for a in alerts if a["id"] == "cc:quota_exhausted"]
        assert len(quota) == 1
        assert quota[0]["severity"] == "WARNING"
    finally:
        health_mcp_mod._service = old
        health_mcp_mod._alert_history = old_history


async def test_cc_quota_exhausted_is_warning_not_critical():
    """Lock in the severity downgrade. Emission MUST be WARNING.
    Part 9c classifier routing assumes this — if this flips back to
    CRITICAL, the blanket rule will promote the alert to Tier 2 and
    the self-defeating Sentinel wake path is back.
    """
    mock_svc = AsyncMock()
    mock_svc.snapshot.return_value = {
        "call_sites": {},
        "cc_sessions": {
            "realtime_status": "UNAVAILABLE",
            "background": {"status": "rate_limited", "active": 0, "hourly_budget": "20/20"},
        },
        "infrastructure": {},
        "queues": {},
        "awareness": {},
    }
    old = health_mcp_mod._service
    old_history = health_mcp_mod._alert_history.copy()
    try:
        health_mcp_mod._service = mock_svc
        health_mcp_mod._alert_history = {}
        alerts = await _impl_health_alerts()
        quota = [a for a in alerts if a["id"] == "cc:quota_exhausted"]
        assert len(quota) == 1
        assert quota[0]["severity"] == "WARNING"
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
