"""SessionStart procedure injection — inject CORE-tier procedures.

Queries the procedure store for CORE-tier (L1) procedures — the most-proven,
always-on set. Surfacing v2 narrowed this from L1/L2/L3 to CORE-only: blind
session-start injection runs before the session topic is known, so it should
carry only procedures proven enough to apply regardless of topic. Lower tiers
surface contextually instead (the proactive hook on the first message, the
tool advisor on tool use, and explicit recall). Renders as compact markdown
within a 200-word budget.

Called by genesis_session_context.py during the SessionStart hook.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Soft cap on procedures considered for injection. The actual rendered list
# is bounded by `_MAX_WORDS`, not this limit — `_MAX_PROCEDURES` is just the
# SQL LIMIT to keep the query cheap. Set above the seed-procedure count so
# that explicit-teach procedures (lower confidence than seeded ones, e.g.,
# 0.667 from `procedure_store`) still enter the candidate pool and get a
# fair shot at the word budget instead of being silently truncated by a
# tight LIMIT clause.
_MAX_PROCEDURES = 10
_MAX_WORDS = 200


async def load_active_procedures(db_path: str | Path) -> str | None:
    """Load CORE-tier procedures for session context injection.

    Returns formatted markdown string, or None if no CORE procedures found.
    Budget: 200 words max.
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
        # CORE-tier (L1) only — the always-on, most-proven set. v2 narrowed
        # blind session-start injection from L1/L2/L3 to CORE so we don't
        # inject mid-tier procedures of uncertain relevance before the session
        # topic is known. Lower tiers surface contextually elsewhere.
        rows = await db.execute(
            """SELECT task_type, principle, steps, activation_tier, confidence
               FROM procedural_memory
               WHERE activation_tier = 'L1'
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
