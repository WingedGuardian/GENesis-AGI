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
) -> None:
    """Record last run for any call site.

    Called from router, CC reflection bridge, conversation loop, embedding, etc.
    Uses INSERT OR REPLACE keyed on call_site_id (primary key).
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
    except Exception:
        logger.error(
            "Failed to record call_site_last_run for %s", call_site_id,
            exc_info=True,
        )
