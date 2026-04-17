"""Essential Knowledge generator (L1b layer).

Generates a compressed summary of what Genesis knows, injected at foreground
session start. Contains: active context, recent decisions, wing index, key facts.

Two generation modes:
- Deterministic skeleton: always available, no external deps, fast
- LLM enrichment: periodic, best-effort, produces richer summaries

Output stored at ~/.genesis/essential_knowledge.md
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_OUTPUT_PATH = Path.home() / ".genesis" / "essential_knowledge.md"
_MAX_TOKENS = 300  # Target size in approximate tokens


async def generate_deterministic(db: aiosqlite.Connection) -> str:
    """Generate essential knowledge from DB queries alone (no LLM).

    Always available, fast, deterministic. The baseline that's always there.
    """
    now = datetime.now(UTC)
    parts: list[str] = []

    # Header with stats
    memory_count = await _count_memories(db)
    obs_count = await _count_observations(db)
    parts.append(
        f"## Essential Knowledge\n"
        f"Generated: {now.strftime('%Y-%m-%dT%H:%MZ')} | "
        f"Store: {memory_count} memories | {obs_count} observations"
    )

    # Active context: recent session topics (last 7 days)
    sessions = await _recent_session_topics(db, days=7)
    if sessions:
        parts.append("\n### Active Context")
        for topic in sessions[:5]:
            parts.append(f"- {topic}")

    # Recent decisions: observations of type 'decision' from last 7 days
    decisions = await _recent_decisions(db, days=7)
    if decisions:
        parts.append("\n### Recent Decisions (7d)")
        for decision in decisions[:5]:
            parts.append(f"- {decision}")

    # Wing index: which wings have content + top topics from actual data
    wing_stats = await _wing_stats(db)
    if wing_stats:
        wing_rooms = await _wing_top_rooms(db, top_n=4)
        parts.append("\n### Wings")
        for wing, count in sorted(wing_stats.items(), key=lambda x: -x[1]):
            if wing == "general":
                continue
            rooms = wing_rooms.get(wing, [])
            room_str = ", ".join(rooms) if rooms else "uncategorized"
            parts.append(f"- {wing} ({count}): {room_str}")

    return "\n".join(parts)


async def generate_and_write(db: aiosqlite.Connection) -> Path:
    """Generate essential knowledge and write to disk.

    Called after foreground sessions end (async, best-effort).
    Returns the path written.
    """
    content = await generate_deterministic(db)

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(content, encoding="utf-8")
    logger.info("Essential knowledge regenerated: %d chars", len(content))
    return _OUTPUT_PATH


# ---------------------------------------------------------------------------
# DB query helpers
# ---------------------------------------------------------------------------


async def _count_memories(db: aiosqlite.Connection) -> int:
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM memory_metadata")
        row = await cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


async def _count_observations(db: aiosqlite.Connection) -> int:
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE resolved = 0"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


async def _recent_session_topics(db: aiosqlite.Connection, days: int = 7) -> list[str]:
    """Get recent foreground session topics."""
    try:
        cursor = await db.execute(
            "SELECT topic FROM cc_sessions "
            "WHERE source_tag = 'foreground' "
            "AND topic IS NOT NULL AND topic != '' "
            "AND started_at > datetime('now', ?) "
            "ORDER BY started_at DESC LIMIT 10",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        return [row[0][:100] for row in rows if row[0]]
    except Exception:
        return []


async def _recent_decisions(db: aiosqlite.Connection, days: int = 7) -> list[str]:
    """Get recent decision observations."""
    try:
        cursor = await db.execute(
            "SELECT content FROM observations "
            "WHERE type IN ('decision', 'architecture_gap', 'learning') "
            "AND resolved = 0 "
            "AND created_at > datetime('now', ?) "
            "ORDER BY created_at DESC LIMIT 10",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        return [row[0][:120] for row in rows if row[0]]
    except Exception:
        return []


async def _wing_stats(db: aiosqlite.Connection) -> dict[str, int]:
    """Count memories per wing from memory_metadata."""
    try:
        cursor = await db.execute(
            "SELECT wing, COUNT(*) FROM memory_metadata "
            "WHERE wing IS NOT NULL "
            "GROUP BY wing ORDER BY COUNT(*) DESC"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception:
        return {}



async def _wing_top_rooms(
    db: aiosqlite.Connection, top_n: int = 4,
) -> dict[str, list[str]]:
    """Return top rooms per wing by memory count (data-driven).

    Uses a single query with window functions to get the top N rooms for
    each wing, ordered by frequency. Only includes rooms with >1 memory
    to filter noise.
    """
    try:
        cursor = await db.execute(
            "SELECT wing, room, cnt FROM ("
            "  SELECT wing, room, COUNT(*) AS cnt,"
            "    ROW_NUMBER() OVER (PARTITION BY wing ORDER BY COUNT(*) DESC) AS rn"
            "  FROM memory_metadata"
            "  WHERE wing IS NOT NULL AND room IS NOT NULL AND room != ''"
            "  GROUP BY wing, room"
            "  HAVING COUNT(*) > 1"
            ") WHERE rn <= ?",
            (top_n,),
        )
        rows = await cursor.fetchall()
        result: dict[str, list[str]] = {}
        for wing, room, _cnt in rows:
            result.setdefault(wing, []).append(room)
        return result
    except Exception:
        logger.debug("Failed to query wing top rooms", exc_info=True)
        return {}
