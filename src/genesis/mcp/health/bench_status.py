"""bench_status MCP tool — Genesis-vs-bare A/B win-rate, readable in-session.

Surfaces the persisted `genesis eval bench` (WS-1 A3) results so Genesis can
read its own A/B benchmark without hand-written SQL. Shaping is shared with the
dashboard route (`eval/bench/surface.py`); aggregate-only, and every summary
carries `judge_calibrated` + the winrate `recommendation` so a provisional
pilot number is never read as significant.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


async def _impl_bench_status(limit: int = 12) -> dict:
    """Read recent bench A/B comparisons and shape them for display.

    Returns ``{"status": "unavailable", ...}`` if the health DB is not wired or
    the read fails — never silently-wrong (a broken read must not read as a
    healthy zero-result).
    """
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    from genesis.eval.bench.surface import build_bench_surface
    from genesis.eval.db import get_bench_comparisons

    try:
        runs = await get_bench_comparisons(_service._db, limit=limit)
    except Exception:
        logger.exception("bench_status: bench read failed")
        return {"status": "unavailable", "message": "bench read failed"}

    return {"status": "ok", **build_bench_surface(runs)}


@mcp.tool()
async def bench_status(limit: int = 12) -> dict:
    """Genesis-vs-bare bench A/B: latest win-rate + recent runs (uncalibrated)."""
    return await _impl_bench_status(limit)
