"""update_history_recent tool — read recent Genesis self-update attempts.

Closes the loop on the update infrastructure: the ``update_history`` table
is written by ``scripts/update.sh`` on every update attempt (success,
failure, or rollback), but nothing read from it until this tool existed.

This is a pure read-only tool. Returns the most recent N entries plus a
computed success rate over that window. If the table hasn't been created
yet (migration not run, or on a fresh install before the first update),
returns a clear ``note`` explaining the state rather than a silent
empty object.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from genesis.db.connection import BUSY_TIMEOUT_MS
from genesis.mcp.health import mcp  # noqa: E402

logger = logging.getLogger(__name__)

_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 100


async def _impl_update_history_recent(limit: int = _DEFAULT_LIMIT) -> dict:
    """Return recent update_history entries and success rate.

    Args:
        limit: number of most-recent entries to return (1-100). Values
            outside that range are clamped and the effective value is
            reported back in ``effective_limit`` so callers can't do
            silently-wrong math against their requested limit.
    """
    requested_limit = limit
    if limit < 1:
        limit = 1
    elif limit > _MAX_LIMIT:
        limit = _MAX_LIMIT

    base_meta: dict = {"effective_limit": limit}
    if requested_limit != limit:
        base_meta["requested_limit"] = requested_limit
        base_meta["limit_clamped"] = True

    if not _DB_PATH.exists():
        return {
            "count": 0,
            "success_rate": None,
            "entries": [],
            "note": f"Genesis database not found at {_DB_PATH}",
            **base_meta,
        }

    try:
        async with aiosqlite.connect(str(_DB_PATH)) as db:
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

            # Detect missing table explicitly — don't hide absence behind
            # a generic empty response.
            cursor = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='update_history'"
            )
            if await cursor.fetchone() is None:
                return {
                    "count": 0,
                    "success_rate": None,
                    "entries": [],
                    "note": (
                        "update_history table not yet created — run "
                        "`python -m genesis.db.migrations --apply` or "
                        "wait for bootstrap to run migrations"
                    ),
                    **base_meta,
                }

            cursor = await db.execute(
                "SELECT id, old_tag, new_tag, old_commit, new_commit, "
                "       status, rollback_tag, failure_reason, "
                "       degraded_subsystems, started_at, completed_at "
                "FROM update_history "
                "ORDER BY started_at DESC "
                "LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
    except aiosqlite.Error:
        logger.error(
            "update_history_recent query failed", exc_info=True,
        )
        raise

    entries = [
        {
            "id": row[0],
            "old_tag": row[1],
            "new_tag": row[2],
            "old_commit": row[3],
            "new_commit": row[4],
            "status": row[5],
            "rollback_tag": row[6],
            "failure_reason": row[7],
            "degraded_subsystems": row[8],
            "started_at": row[9],
            "completed_at": row[10],
        }
        for row in rows
    ]

    if entries:
        successes = sum(1 for e in entries if e["status"] == "success")
        success_rate = round(successes / len(entries), 3)
    else:
        success_rate = None

    return {
        "count": len(entries),
        "success_rate": success_rate,
        "entries": entries,
        **base_meta,
    }


@mcp.tool()
async def update_history_recent(limit: int = _DEFAULT_LIMIT) -> dict:
    """Recent Genesis self-update attempts + success rate over the window.

    Reads the update_history table written by scripts/update.sh. Each
    entry records an update attempt — success, failure, or rolled_back
    — with before/after versions, timing, and failure context when
    applicable. Use this to diagnose update issues or verify recent
    update history.

    Args:
        limit: number of entries to return (1-100, default 10).
    """
    return await _impl_update_history_recent(limit)
