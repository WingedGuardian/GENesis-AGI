"""Reflex snapshot — nerve ingestion state + signal-store aggregates.

Two data planes, degrading independently:

- ``ingestor``: live in-memory counters from the running ``ReflexIngestor``
  (queue depth, processed, dropped). Read via ``GenesisRuntime.peek()`` —
  the sanctioned no-construct read — so a process without a bootstrapped
  runtime (the standalone health MCP child, early bootstrap, tests) gets
  ``None`` here instead of a lazily-constructed blank singleton.
- ``counts`` / ``top_classes`` / ``total_signals``: DB aggregates from
  ``reflex_signals`` — portable to any process holding the shared DB.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


def _ingestor_stats() -> dict | None:
    """Live ingestor counters, or None when no runtime/ingestor exists."""
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.peek()
        ingestor = getattr(rt, "_reflex_ingestor", None) if rt is not None else None
        if ingestor is None:
            return None
        return dict(ingestor.stats)
    except Exception:
        logger.debug("Reflex ingestor stats read failed", exc_info=True)
        return None


async def reflex(db: aiosqlite.Connection | None) -> dict:
    """Reflex-arc section of the health snapshot (never raises)."""
    result: dict = {
        "ingestor": _ingestor_stats(),
        "counts": {},
        "top_classes": [],
        "total_signals": 0,
    }
    if db is None:
        return result
    try:
        from genesis.db.crud import reflex_signals as signals_crud

        counts = await signals_crud.count_by_status(db)
        result["counts"] = counts
        result["total_signals"] = sum(counts.values())
        result["top_classes"] = await signals_crud.top_class_keys(db, limit=8)
    except Exception:
        logger.debug("Reflex signal aggregates query failed", exc_info=True)
    return result
