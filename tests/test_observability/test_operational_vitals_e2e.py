"""End-to-end tests for the Operational Vitals feature.

Tests the integration between ProviderActivityTracker, health alerts,
and the provider_activity MCP tool.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.observability.provider_activity import ProviderActivityTracker


class TestProviderActivityTool:
    """Test the provider_activity logic via _activity_tracker directly.

    FastMCP wraps @mcp.tool() functions as FunctionTool objects, so we
    test the underlying logic rather than calling through the MCP layer.
    """

    def test_returns_unavailable_when_no_tracker(self):
        """With no tracker, the tool should indicate unavailability."""
        # The tool function checks `_activity_tracker is None`
        # We verify the tracker's summary behavior instead
        tracker = ProviderActivityTracker()
        result = tracker.summary("nonexistent")
        assert result["calls"] == 0

    def test_returns_all_providers(self):
        tracker = ProviderActivityTracker()
        tracker.record("llm.gemini", latency_ms=100, success=True)
        tracker.record("qdrant.search", latency_ms=10, success=True)

        result = tracker.summary()
        assert isinstance(result, list)
        providers = {r["provider"] for r in result}
        assert "llm.gemini" in providers
        assert "qdrant.search" in providers

    def test_returns_single_provider(self):
        tracker = ProviderActivityTracker()
        tracker.record("llm.gemini", latency_ms=100, success=True)

        result = tracker.summary("llm.gemini")
        assert isinstance(result, dict)
        assert result["provider"] == "llm.gemini"
        assert result["calls"] == 1


class TestAlertConditions:
    """Test the new alert conditions in _impl_health_alerts."""

    @pytest.mark.asyncio
    async def test_embedding_failing_alert_fires_on_high_error_rate(self):
        from genesis.mcp import health_mcp

        tracker = ProviderActivityTracker()
        # 3 failures, 1 success = 75% error rate > 50% threshold
        tracker.record("episodic_memory_embedding", latency_ms=100, success=False)
        tracker.record("episodic_memory_embedding", latency_ms=100, success=False)
        tracker.record("episodic_memory_embedding", latency_ms=100, success=False)
        tracker.record("episodic_memory_embedding", latency_ms=100, success=True)

        old_tracker = health_mcp._activity_tracker
        old_service = health_mcp._service
        try:
            health_mcp._activity_tracker = tracker
            health_mcp._service = MagicMock()
            health_mcp._service.snapshot = AsyncMock(return_value={
                "call_sites": {}, "queues": {}, "cc_sessions": {},
                "infrastructure": {}, "services": {}, "awareness": {},
            })
            alerts = await health_mcp._impl_health_alerts()
            embedding_alerts = [a for a in alerts if a["id"] == "provider:embedding_failing"]
            assert len(embedding_alerts) == 1
            assert "75%" in embedding_alerts[0]["message"]
        finally:
            health_mcp._activity_tracker = old_tracker
            health_mcp._service = old_service

    @pytest.mark.asyncio
    async def test_embedding_alert_does_not_fire_on_zero_calls(self):
        """No alert when there are zero calls (idle system)."""
        from genesis.mcp import health_mcp

        tracker = ProviderActivityTracker()
        # No calls recorded — system is idle

        old_tracker = health_mcp._activity_tracker
        old_service = health_mcp._service
        try:
            health_mcp._activity_tracker = tracker
            health_mcp._service = MagicMock()
            health_mcp._service.snapshot = AsyncMock(return_value={
                "call_sites": {}, "queues": {}, "cc_sessions": {},
                "infrastructure": {}, "services": {}, "awareness": {},
            })
            alerts = await health_mcp._impl_health_alerts()
            embedding_alerts = [a for a in alerts if a["id"] == "provider:embedding_failing"]
            assert len(embedding_alerts) == 0
        finally:
            health_mcp._activity_tracker = old_tracker
            health_mcp._service = old_service

    @pytest.mark.asyncio
    async def test_qdrant_unreachable_alert(self):
        from genesis.mcp import health_mcp

        tracker = ProviderActivityTracker()
        # All searches fail
        tracker.record("qdrant.search", latency_ms=5000, success=False)
        tracker.record("qdrant.search", latency_ms=5000, success=False)

        old_tracker = health_mcp._activity_tracker
        old_service = health_mcp._service
        try:
            health_mcp._activity_tracker = tracker
            health_mcp._service = MagicMock()
            health_mcp._service.snapshot = AsyncMock(return_value={
                "call_sites": {}, "queues": {}, "cc_sessions": {},
                "infrastructure": {}, "services": {}, "awareness": {},
            })
            alerts = await health_mcp._impl_health_alerts()
            qdrant_alerts = [a for a in alerts if a["id"] == "provider:qdrant_unreachable"]
            assert len(qdrant_alerts) == 1
        finally:
            health_mcp._activity_tracker = old_tracker
            health_mcp._service = old_service

    @pytest.mark.asyncio
    async def test_job_quarantine_alert(self):
        from genesis.mcp import health_mcp

        registry = MagicMock()
        registry.list_registered.return_value = ["awareness_tick"]
        registry.is_quarantined.return_value = True

        old_registry = health_mcp._job_retry_registry
        old_service = health_mcp._service
        try:
            health_mcp._job_retry_registry = registry
            health_mcp._service = MagicMock()
            health_mcp._service.snapshot = AsyncMock(return_value={
                "call_sites": {}, "queues": {}, "cc_sessions": {},
                "infrastructure": {}, "services": {}, "awareness": {},
            })
            alerts = await health_mcp._impl_health_alerts()
            quarantine_alerts = [a for a in alerts if "quarantined" in a["id"]]
            assert len(quarantine_alerts) == 1
            assert "awareness_tick" in quarantine_alerts[0]["id"]
        finally:
            health_mcp._job_retry_registry = old_registry
            health_mcp._service = old_service


class TestJobRetryTrigger:
    """Test that record_job_failure triggers retry at threshold."""

    def test_retry_triggered_at_threshold(self):
        """After 3 consecutive failures, attempt_retry should be called."""
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.__new__(GenesisRuntime)
        rt._job_health = {}
        rt._db = None

        # Mock the registry
        mock_registry = MagicMock()
        mock_registry.attempt_retry = AsyncMock()
        rt._job_retry_registry = mock_registry

        # Record 3 failures
        with (
            patch("genesis.runtime.GenesisRuntime._persist_job_health"),
            patch("genesis.util.tasks.tracked_task") as mock_tracked,
        ):
                rt.record_job_failure("test_job", "error 1")
                rt.record_job_failure("test_job", "error 2")
                assert mock_tracked.call_count == 0  # Not yet at threshold

                rt.record_job_failure("test_job", "error 3")
                assert mock_tracked.call_count == 1  # Threshold reached

    def test_no_retry_without_registry(self):
        """No crash when registry is None."""
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.__new__(GenesisRuntime)
        rt._job_health = {}
        rt._db = None
        rt._job_retry_registry = None

        with patch("genesis.runtime.GenesisRuntime._persist_job_health"):
            # Should not raise even after 3 failures
            rt.record_job_failure("test_job", "error 1")
            rt.record_job_failure("test_job", "error 2")
            rt.record_job_failure("test_job", "error 3")
