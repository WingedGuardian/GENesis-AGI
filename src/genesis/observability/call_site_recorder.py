"""Shared call-site last-run recorder.

Records the most recent execution of each LLM call site (router-based,
CC-based, embedding, etc.) so the neural monitor can display last-run
times and full responses.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def record_last_run(
    db: aiosqlite.Connection,
    call_site_id: str,
    provider: str,
    model_id: str,
    response_text: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    success: bool = True,
) -> bool:
    """Record last run for any call site. Returns True iff the row landed.

    Called from router, CC reflection bridge, conversation loop, embedding, etc.
    Uses INSERT OR REPLACE keyed on call_site_id (primary key). Never raises —
    a failed write is logged and reported as False so callers that account for
    telemetry (the detached ambient worker) don't claim a row that isn't there.
    """
    now = datetime.now(UTC).isoformat()
    try:
        await db.execute(
            """INSERT OR REPLACE INTO call_site_last_run
               (call_site_id, last_run_at, provider_used, model_id,
                response_text, input_tokens, output_tokens, success, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                call_site_id, now, provider, model_id,
                response_text, input_tokens, output_tokens, int(success), now,
            ),
        )
        await db.commit()
        return True
    except Exception:
        logger.error(
            "Failed to record call_site_last_run for %s", call_site_id,
            exc_info=True,
        )
        return False


async def record_last_run_detached(
    db_path: str,
    call_site_id: str,
    provider: str,
    model_id: str,
    response_text: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    success: bool = True,
) -> bool:
    """``record_last_run`` for processes that don't hold a server DB handle.

    Opens its own short-lived connection via ``get_raw_db`` (WAL +
    busy_timeout) — the sanctioned seam for detached workers, hooks, and
    gate code (contribution version gate, ambient worker). Best-effort:
    never raises; True only when the row demonstrably landed.
    """
    try:
        from genesis.db.connection import get_raw_db

        async with get_raw_db(db_path) as db:
            return await record_last_run(
                db, call_site_id, provider, model_id, response_text,
                input_tokens=input_tokens, output_tokens=output_tokens,
                success=success,
            )
    except Exception:
        logger.debug(
            "Detached last-run record failed for %s", call_site_id,
            exc_info=True,
        )
        return False
