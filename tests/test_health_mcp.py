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
        "tmpfs": {"free_pct": 90, "free_mb": 460, "total_mb": 512},
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
    async def test_returns_provider_summary(self):
        svc = _mock_service({
            "call_sites": {
                "a": {"status": "healthy"},
                "b": {"status": "degraded"},
                "c": {"status": "down"},
            },
            "cc_sessions": {},
            "infrastructure": {},
            "queues": {},
            "cost": {},
            "surplus": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_status()
        assert result["provider_summary"] == "1/3 call sites ok, 1 degraded, 1 down"

    @pytest.mark.asyncio
    async def test_provider_summary_standalone_statuses(self):
        """Standalone MCP uses active/idle/stale instead of healthy/degraded/down."""
        svc = _mock_service({
            "call_sites": {
                "a": {"status": "active"},
                "b": {"status": "idle"},
                "c": {"status": "stale"},
            },
            "cc_sessions": {},
            "infrastructure": {},
            "queues": {},
            "cost": {},
            "surplus": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_status()
        assert result["provider_summary"] == "1/3 call sites ok, 1 idle, 1 stale"

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
        svc = _mock_service({
            "call_sites": {"2_triage": {"status": "down"}},
            "queues": {},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        criticals = [a for a in result if a["severity"] == "CRITICAL"]
        assert len(criticals) == 1
        assert "2_triage" in criticals[0]["message"]

    @pytest.mark.asyncio
    async def test_degraded_call_site_generates_warning(self):
        svc = _mock_service({
            "call_sites": {"2_triage": {"status": "degraded", "active_provider": "b"}},
            "queues": {},
            "cc_sessions": {},
        })
        health_mcp.init_health_mcp(svc)
        result = await _impl_health_alerts()
        warnings = [a for a in result if a["severity"] == "WARNING"]
        assert len(warnings) == 1

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
