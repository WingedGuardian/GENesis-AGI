"""health_status tool."""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp  # noqa: E402

logger = logging.getLogger(__name__)


async def _impl_health_status() -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    _activity_tracker = health_mcp_mod._activity_tracker

    if _service is None:
        return {"status": "unavailable", "message": "HealthDataService not initialized"}

    snap = await _service.snapshot()
    call_sites = snap.get("call_sites", {})
    total = len(call_sites)
    healthy = sum(1 for s in call_sites.values() if s.get("status") == "healthy")

    provider_activity = []
    if _activity_tracker and hasattr(_activity_tracker, "summary"):
        try:
            provider_activity = _activity_tracker.summary()
        except Exception:
            logger.warning("Failed to get provider activity summary", exc_info=True)

    return {
        "provider_summary": f"{healthy}/{total} call sites healthy",
        "cc_sessions": snap.get("cc_sessions", {}),
        "infrastructure": snap.get("infrastructure", {}),
        "queues": snap.get("queues", {}),
        "cost": snap.get("cost", {}),
        "surplus": snap.get("surplus", {}),
        "awareness": snap.get("awareness", {}),
        "outreach_stats": snap.get("outreach_stats", {}),
        "services": snap.get("services", {}),
        "provider_activity": provider_activity,
    }


@mcp.tool()
async def health_status() -> dict:
    """Current system health: provider availability, resilience state, infrastructure, queues, cost."""
    return await _impl_health_status()
