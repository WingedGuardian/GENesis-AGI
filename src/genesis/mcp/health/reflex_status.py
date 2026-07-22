"""reflex_status MCP tool — the reflex-arc nerve surface.

One read-only view of what the nerve has ingested: signal counts by
lifecycle status, the loudest error classes, and the most recent signals.

Process-portable by design: the standalone health MCP child has no
bootstrapped runtime, so ``enabled`` comes from the config loader (not the
live ingestor) and everything else from the shared DB. Live in-memory
counters (queue depth, processed, dropped) belong to the in-server health
snapshot / status.json, not this tool.

Read-only. Reuses existing CRUD; no new schema; does NOT change behaviour.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


async def _impl_reflex_status() -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db
    from genesis.db.crud import reflex_signals as signals_crud
    from genesis.reflex.config import load_reflex_config

    try:
        enabled = load_reflex_config().ingest_enabled
    except Exception:
        logger.warning("Reflex config load failed in reflex_status", exc_info=True)
        enabled = None

    counts = await signals_crud.count_by_status(db)
    top_classes = await signals_crud.top_class_keys(db, limit=8)
    recent = await signals_crud.list_recent(db, limit=10)

    return {
        "status": "ok",
        "ingest_enabled": enabled,
        "total_signals": sum(counts.values()),
        "counts_by_status": counts,
        "top_classes": top_classes,
        "recent_signals": [
            {
                "fingerprint": r.get("fingerprint"),
                "class_key": r.get("class_key"),
                "task_name": r.get("task_name"),
                "error_type": r.get("error_type"),
                "status": r.get("status"),
                "occurrence_count": r.get("occurrence_count"),
                "first_seen_at": r.get("first_seen_at"),
                "last_seen_at": r.get("last_seen_at"),
                "last_error_message": r.get("last_error_message"),
            }
            for r in recent
        ],
        "note": (
            "Reflex arc afferent nerve. Signals are fingerprint-deduped "
            "task.failed exceptions (a burst of N identical crashes = one "
            "signal, occurrence_count N). ingest_enabled reflects the config "
            "loader in THIS process (shipped default is false; installs "
            "enable via ~/.genesis/config/reflex.local.yaml); None = config "
            "unreadable. Live queue/drop counters live in health_status "
            ".reflex.ingestor. Read-only — does NOT change behaviour."
        ),
    }


@mcp.tool()
async def reflex_status() -> dict:
    """What has the reflex nerve ingested — Genesis's fingerprint-deduped
    record of its own background-task crashes?

    Signal counts by lifecycle status, the loudest error classes, and the
    most recent signals. Read-only; does NOT change behaviour.
    """
    return await _impl_reflex_status()
