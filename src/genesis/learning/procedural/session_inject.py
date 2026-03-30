"""SessionStart procedure injection — load relevant procedures for session context.

Queries the procedure store for L3+ tier procedures relevant to the current
session context. Renders as compact markdown within a 200-word budget.

Called by genesis_session_context.py during the SessionStart hook.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_MAX_PROCEDURES = 5
_MAX_WORDS = 200


async def load_active_procedures(db_path: str | Path) -> str | None:
    """Load relevant procedures for session context injection.

    Returns formatted markdown string, or None if no procedures found.
    Budget: 200 words max, top 5 procedures.
    """
    from genesis.db.connection import BUSY_TIMEOUT_MS

    try:
        db = await aiosqlite.connect(str(db_path))
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except Exception:
        logger.debug("Could not open DB for procedure injection", exc_info=True)
        return None

    try:
        # Get L3+ procedures, ordered by confidence
        rows = await db.execute(
            """SELECT task_type, principle, steps, activation_tier, confidence
               FROM procedural_memory
               WHERE activation_tier IN ('L1', 'L2', 'L3')
                 AND deprecated = 0 AND quarantined = 0
               ORDER BY confidence DESC
               LIMIT ?""",
            (_MAX_PROCEDURES,),
        )
        results = await rows.fetchall()

        if not results:
            return None

        lines = []
        word_count = 0
        for row in results:
            entry = f"- **{row['task_type']}** ({row['confidence']:.0%}): {row['principle']}"
            entry_words = len(entry.split())
            if word_count + entry_words > _MAX_WORDS and lines:
                break
            lines.append(entry)
            word_count += entry_words

        return "\n".join(lines) if lines else None
    except Exception:
        logger.debug("Procedure injection query failed", exc_info=True)
        return None
    finally:
        await db.close()
