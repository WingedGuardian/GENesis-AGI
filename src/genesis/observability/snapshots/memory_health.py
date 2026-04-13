"""Memory health metrics snapshot — calls genesis.memory.health functions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def memory_health(db: aiosqlite.Connection | None) -> dict:
    """Return algorithmic memory health stats for the dashboard.

    Calls the pure-SQL functions from genesis.memory.health.
    Qdrant duplicate check is skipped here (too slow for a 15s poll).
    """
    if db is None:
        return {"status": "unavailable"}

    try:
        from genesis.memory.health import (
            distribution_stats,
            growth_stats,
            orphan_stats,
        )

        orphans = await orphan_stats(db)
        distribution = await distribution_stats(db)
        growth = await growth_stats(db)

        # Derive a simple health status from the stats
        status = "healthy"
        if "error" in orphans or "error" in distribution or "error" in growth:
            status = "degraded"
        elif orphans.get("orphan_pct", 0) > 50:
            status = "warning"

        return {
            "status": status,
            "orphans": orphans,
            "distribution": distribution,
            "growth": growth,
        }
    except ImportError:
        logger.warning("genesis.memory.health not available", exc_info=True)
        return {"status": "unavailable"}
    except Exception:
        logger.error("Memory health snapshot failed", exc_info=True)
        return {"status": "error"}
