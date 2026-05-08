"""Tests for health MCP tool implementations."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.mcp import health_mcp
from genesis.mcp.health_mcp import (
    _impl_health_alerts,
    _impl_health_errors,
    _impl_health_status,
)


@pytest.fixture(autouse=True)
def _reset_service():
    """Reset global service between tests."""
    health_mcp._service = None
    health_mcp._alert_history = {}
    yield
    health_mcp._service = None
    health_mcp._alert_history = {}


_HEALTHY_DEFAULTS = {
    "services": {
        "bridge": {"active_state": "active", "sub_state": "running", "pid": 1234, "pid_alive": True},
        "watchdog_timer": {"active_state": "active", "sub_state": "waiting"},
        "watchdog": {"consecutive_failures": 0, "last_reason": None, "in_backoff": False},
    },
    "infrastructure": {
        "disk": {"free_pct": 80, "free_gb": 100, "total_gb": 140},
        "qdrant_collections": {"status": "healthy", "missing": []},
    },
}


def _mock_service(snapshot_data: dict):
    # Merge healthy defaults so new alert checks don't fire on missing keys
    merged = {**_HEALTHY_DEFAULTS, **snapshot_data}
    svc = AsyncMock()
    svc.snapshot = AsyncMock(return_value=merged)
    svc._dead_letter = None
    svc._breakers = None
    svc._routing_config = None
    svc._db = None
    return svc


class TestHealthStatus:
    @pytest.mark.asyncio
    async def test_unavailable_without_service(self):
        result = await _impl_health_status()
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_no_provider_summary(self):
        """provider_summary was removed — misleading call site counts."""
        svc = _mock_service({
            "call_sites": {
                "a": {"status": "healthy"},
                "b": {"status": "degraded"},
            },
            "cc_sessions": {},
            "infrastructure": {},
            "queues": {},
            "cost": {},
            "surplus": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_status()
        assert "provider_summary" not in result

    @pytest.mark.asyncio
    async def test_includes_all_sections(self):
        svc = _mock_service({
            "call_sites": {},
            "cc_sessions": {"fg": {}},
            "infrastructure": {"db": {"status": "healthy"}},
            "queues": {"deferred_work": 3},
            "cost": {"daily_usd": 0.1},
            "surplus": {"status": "idle"},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_status()
        assert "cc_sessions" in result
        assert "infrastructure" in result
        assert "queues" in result
        assert "cost" in result
        assert "surplus" in result


class TestHealthErrors:
    @pytest.mark.asyncio
    async def test_unavailable_without_service(self):
        result = await _impl_health_errors()
        assert result[0]["error"] == "HealthDataService not initialized"

    @pytest.mark.asyncio
    async def test_empty_when_no_errors(self):
        svc = _mock_service({})
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_errors()
        assert result == []


class TestHealthAlerts:
    @pytest.mark.asyncio
    async def test_unavailable_without_service(self):
        result = await _impl_health_alerts()
        assert result[0]["severity"] == "CRITICAL"

    @pytest.mark.asyncio
    async def test_down_call_site_generates_critical(self):
        # Must use a wired call site — health_alerts now skips groundwork
        # sites (meta.wired=False) to prevent ghost-down spam.
        svc = _mock_service({
            "call_sites": {"3_micro_reflection": {"status": "down"}},
            "queues": {},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        criticals = [a for a in result if a["severity"] == "CRITICAL"]
        assert len(criticals) == 1
        assert "3_micro_reflection" in criticals[0]["message"]

    @pytest.mark.asyncio
    async def test_degraded_call_site_generates_warning(self):
        svc = _mock_service({
            "call_sites": {"3_micro_reflection": {"status": "degraded", "active_provider": "b"}},
            "queues": {},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        warnings = [a for a in result if a["severity"] == "WARNING"]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_groundwork_call_site_alert_suppressed(self):
        """Regression for the call-site-11 spam loop.

        Sites with meta.wired=False (groundwork — config exists but no
        runtime invocation) MUST NOT emit alerts. Their "down" status is
        meaningless because nothing exercises them.
        """
        svc = _mock_service({
            "call_sites": {"2_triage": {"status": "down"}},  # wired=False
            "queues": {},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        site_alerts = [a for a in result if a.get("id", "").startswith("call_site:")]
        assert site_alerts == []

    @pytest.mark.asyncio
    async def test_disabled_call_site_alert_suppressed(self):
        """Sites with status='disabled' (no API key) MUST NOT emit alerts.

        This is the root-cause fix for the Sentinel spam loop: Anthropic
        providers without ANTHROPIC_API_KEY → call site marked disabled
        (config state, not outage) → no alert → no Sentinel wake.
        """
        svc = _mock_service({
            "call_sites": {
                "3_micro_reflection": {  # wired site
                    "status": "disabled",
                    "disabled_reason": "no_api_keys_configured",
                },
            },
            "queues": {},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        site_alerts = [a for a in result if a.get("id", "").startswith("call_site:")]
        assert site_alerts == []

    @pytest.mark.asyncio
    async def test_high_queue_depth_generates_warning(self):
        svc = _mock_service({
            "call_sites": {},
            "queues": {"deferred_work": 150},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        warnings = [a for a in result if a["severity"] == "WARNING"]
        assert any("deferred_work" in w["message"] for w in warnings)

    @pytest.mark.asyncio
    async def test_cc_throttled_generates_warning(self):
        svc = _mock_service({
            "call_sites": {},
            "queues": {},
            "cc_sessions": {
                "foreground": {"status": "idle"},
                "background": {"status": "throttled", "hourly_budget": "16/20"},
            },
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        warnings = [a for a in result if a["severity"] == "WARNING"]
        assert any("throttled" in w["message"] for w in warnings)

    @pytest.mark.asyncio
    async def test_no_alerts_when_all_healthy(self):
        svc = _mock_service({
            "call_sites": {"a": {"status": "healthy"}},
            "queues": {"deferred_work": 5},
            "cc_sessions": {"foreground": {"status": "idle"}, "background": {"status": "healthy"}},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        assert result == []

    @pytest.mark.asyncio
    async def test_resolved_alerts_with_active_only_false(self):
        svc = _mock_service({
            "call_sites": {"a": {"status": "down"}},
            "queues": {},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)

        # First call — creates alert
        await _impl_health_alerts()

        # Second call — alert resolved
        svc.snapshot = AsyncMock(return_value={
            "call_sites": {"a": {"status": "healthy"}},
            "queues": {},
            "cc_sessions": {},
        })
        result = await _impl_health_alerts(active_only=False)
        resolved = [a for a in result if a["severity"] == "RESOLVED"]
        assert len(resolved) == 1
