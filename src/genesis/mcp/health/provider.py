"""provider_activity tool."""

from __future__ import annotations

from genesis.mcp.health import mcp


@mcp.tool()
async def provider_activity(provider: str = "") -> dict | list[dict]:
    """Per-provider call metrics: counts, error rates, latency percentiles, cache hits.

    Call with no args to see all providers. Pass a provider name (e.g.,
    "qdrant.search", "llm.gemini_flash", "mcp.memory.memory_recall")
    to see one provider's metrics over the rolling 1-hour window.
    """
    import genesis.mcp.health_mcp as health_mcp_mod  # late import to avoid circular
    _activity_tracker = health_mcp_mod._activity_tracker
    if _activity_tracker is None:
        return {"status": "unavailable", "message": "ProviderActivityTracker not initialized"}
    return _activity_tracker.summary(provider or None)
