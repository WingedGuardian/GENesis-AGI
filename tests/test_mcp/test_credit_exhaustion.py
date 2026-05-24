"""Tests for credit exhaustion detection in health alerts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_service(*, rows_recent=None, rows_baseline=None):
    """Build a mock HealthDataService with a mock DB."""
    svc = MagicMock()
    svc._db = AsyncMock()
    svc._breakers = None
    svc._routing_config = None
    svc._provider_health = None

    # Mock the snapshot to return minimal data (no call sites, no infra, etc.)
    svc.snapshot = AsyncMock(return_value={
        "call_sites": {},
        "infrastructure": {},
        "cc_sessions": {},
        "resilience": {"level": "L0"},
        "queues": {},
    })

    # Set up DB execute responses for the credit exhaustion queries
    recent_cursor = AsyncMock()
    recent_cursor.fetchall = AsyncMock(return_value=rows_recent or [])

    baseline_cursor = AsyncMock()
    baseline_cursor.fetchone = AsyncMock(return_value=rows_baseline)

    # The DB gets called multiple times — we need to handle the
    # update-check queries too (they come after credit exhaustion)
    update_cursor = AsyncMock()
    update_cursor.fetchone = AsyncMock(return_value=None)

    call_count = 0

    async def mock_execute(query, params=None):
        nonlocal call_count
        call_count += 1
        if "FROM activity_log" in query and "GROUP BY provider" in query:
            return recent_cursor
        if "FROM activity_log" in query and "provider = ?" in query:
            return baseline_cursor
        return update_cursor

    svc._db.execute = mock_execute
    return svc


@pytest.mark.asyncio
async def test_credit_exhaustion_detected():
    """Alert fires when a previously healthy provider starts failing."""
    # Provider had 100 calls, 2 errors over 7d (98% success)
    # Now has 20 calls, 15 errors in last hour (75% error rate)
    svc = _make_mock_service(
        rows_recent=[("episodic_memory_embedding", 20, 15)],
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 1
    alert = credit_alerts[0]
    assert alert["severity"] == "CRITICAL"
    assert "episodic_memory_embedding" in alert["id"]
    assert "credit" in alert["message"].lower() or "exhaustion" in alert["message"].lower()


@pytest.mark.asyncio
async def test_no_alert_when_baseline_was_unhealthy():
    """No alert if the provider was already failing in the 7-day baseline."""
    # Provider had 100 calls, 30 errors over 7d (70% success — already bad)
    svc = _make_mock_service(
        rows_recent=[("episodic_memory_embedding", 20, 15)],
        rows_baseline=(100, 30),  # >5% baseline error rate
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_no_alert_for_info_tier_provider():
    """INFO-tier providers don't trigger credit exhaustion alerts."""
    svc = _make_mock_service(
        rows_recent=[("some_random_provider", 20, 15)],
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_warning_severity_for_warning_tier():
    """WARNING-tier providers get WARNING severity, not CRITICAL."""
    svc = _make_mock_service(
        rows_recent=[("web_search", 20, 15)],
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 1
    assert credit_alerts[0]["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_no_alert_when_recent_rate_is_low():
    """No alert if recent error rate is below 50%."""
    svc = _make_mock_service(
        rows_recent=[("episodic_memory_embedding", 20, 5)],  # 25% error rate
        rows_baseline=(100, 2),
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0


@pytest.mark.asyncio
async def test_no_alert_with_insufficient_baseline():
    """No alert if baseline has fewer than 10 calls."""
    svc = _make_mock_service(
        rows_recent=[("episodic_memory_embedding", 20, 15)],
        rows_baseline=(5, 0),  # Only 5 baseline calls
    )

    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):

        from genesis.mcp.health.errors import _impl_health_alerts
        alerts = await _impl_health_alerts(active_only=True)

    credit_alerts = [a for a in alerts if "credit_exhaustion" in a.get("id", "")]
    assert len(credit_alerts) == 0
