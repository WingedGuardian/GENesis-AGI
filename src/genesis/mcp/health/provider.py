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

    # `summary()` reads the tracker's IN-MEMORY `_calls` map, which records only
    # what this process has observed. In the long-lived server that is the whole
    # system; in the standalone server an external client starts, the tracker is
    # freshly constructed per subprocess, so the first call returns [] and later
    # ones show only traffic that happened inside that subprocess — reporting a
    # healthy-looking void to a client asking about SYSTEM-WIDE provider health.
    # The DB fallback exists for exactly this and was simply never called here.
    rows = await _activity_tracker.summary_with_db_fallback()
    if not provider:
        return rows
    # `summary(provider)` returns a dict, so the filtered shape is preserved
    # rather than quietly changed to a list for one caller. An absent provider
    # returns the tracker's own empty-provider shape for consistency.
    for row in rows:
        if row.get("provider") == provider:
            return row
    return _activity_tracker.summary(provider)
