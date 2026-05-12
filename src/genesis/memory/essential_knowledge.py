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
import re
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

    # System state: ego focus + active sessions
    ego_focus = await _ego_focus(db)
    active_sessions = await _active_session_count(db)
    state_lines: list[str] = []
    if ego_focus:
        state_lines.append(f"- Ego focus: {ego_focus}")
    if active_sessions > 0:
        state_lines.append(f"- Active sessions: {active_sessions}")
    if state_lines:
        parts.append("\n### System State")
        parts.extend(state_lines)

    # Active context: recent session topics (last 7 days)
    sessions = await _recent_session_topics(db, days=7)
    if sessions:
        parts.append("\n### Active Context")
        for topic in sessions[:5]:
            parts.append(f"- {topic}")

    # Key insights: meaningful observations from last 7 days
    decisions = await _recent_decisions(db, days=7)
    if decisions:
        parts.append("\n### Key Insights (7d)")
        for decision in decisions[:5]:
            parts.append(f"- {decision}")

    # Upcoming events: user-world events from the SVO calendar
    upcoming = await _upcoming_events(db, days=30)
    if upcoming:
        parts.append("\n### Upcoming Events")
        for evt in upcoming[:5]:
            parts.append(f"- {evt}")

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
        return [row[0][:200] for row in rows if row[0]]
    except Exception:
        return []


async def _recent_decisions(db: aiosqlite.Connection, days: int = 7) -> list[str]:
    """Get recent meaningful observations (exclusion-based to capture future types)."""
    # Exclude noisy/mechanical types; everything else surfaces.
    _EXCLUDED_TYPES = (
        # Mechanical / tick-level
        "awareness_tick",
        "genesis_version_change",
        "cc_version_available",
        "cc_version_baseline",
        "genesis_update_available",
        "genesis_version_baseline",
        "genesis_update_failed",
        "light_escalation_pending",
        "light_escalation_resolved",
        "memory_index",
        "bugfix_committed",
        "light_reflection",
        "micro_reflection",
        "reflection_output",
        "user_model_delta",
        "memory_operation_executed",
        "memory_operation",
        "cc_memory_file",
        # Raw JSON / non-human-readable content
        "reflection_summary",
        "finding",
        "triage_depth_3",
        "project_context",
        "feedback_rule",
        "reference_pointer",
        "quarantined_reflection",
        "self_assessment",
        "quality_calibration",
        "quality_drift",
        "code_audit",
        "version_change",
        "tech_debt",
    )
    placeholders = ",".join("?" * len(_EXCLUDED_TYPES))
    try:
        cursor = await db.execute(
            "SELECT content FROM observations "
            f"WHERE type NOT IN ({placeholders}) "
            "AND resolved = 0 "
            "AND created_at > datetime('now', ?) "
            "ORDER BY created_at DESC LIMIT 10",
            (*_EXCLUDED_TYPES, f"-{days} days"),
        )
        rows = await cursor.fetchall()
        return [row[0][:250] for row in rows if row[0]]
    except Exception:
        return []


# Behavioral focus patterns — ego should describe a topic, not a behavioral
# state.  Duplicated from ego.session (deliberate: avoids cross-layer import
# from memory → ego).  Keep in sync with ego.session._BEHAVIORAL_FOCUS_RE.
_BEHAVIORAL_FOCUS_RE = re.compile(
    r"(?i)"
    # Any focus starting with a self-referential behavioral verb.
    r"(?:^(?:holding|waiting|stepping|standing|lying|staying|backing|"
    r"keeping|pausing|going|hibernating|letting|until|not)\s"
    # Explicit non-action / dormancy phrasing
    r"|^observing\s+(?:only|quietly)"
    r"|^passive\s+(?:mode|watch)"
    r"|^minimal\s+(?:engagement|activity)"
    r"|^reduced\s+(?:activity|engagement)"
    r"|quiet\s+mode"
    r"|(?:self-|going\s+|entering\s+)dormant"
    r"|(?:going|entering)\s+fallow"
    r"|no\s+proposals?\s+(?:until|for\s+now))"
)


async def _ego_focus(db: aiosqlite.Connection) -> str | None:
    """Read ego focus summary from ego_state KV table.

    Validates that the focus describes a topic, not a behavioral state.
    Behavioral self-assignments (e.g., 'holding back') are omitted to
    prevent propagation to foreground sessions via essential_knowledge.
    """
    try:
        cursor = await db.execute(
            "SELECT value FROM ego_state WHERE key = 'ego_focus_summary'"
        )
        row = await cursor.fetchone()
        if row and row[0]:
            focus = row[0][:200]
            if _BEHAVIORAL_FOCUS_RE.search(focus):
                logger.warning(
                    "Ego focus contains behavioral self-assignment, "
                    "omitting from essential knowledge: %s",
                    focus[:80],
                )
                return None
            return focus
        return None
    except Exception:
        return None


async def _active_session_count(db: aiosqlite.Connection) -> int:
    """Count currently active CC sessions."""
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM cc_sessions WHERE status = 'active'"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


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



async def _upcoming_events(
    db: aiosqlite.Connection, days: int = 30,
) -> list[str]:
    """Get upcoming user-world events from the SVO event calendar.

    Reuses the memory_events CRUD for consistent filtering (excludes
    system events, caps at N days).
    """
    try:
        from genesis.db.crud import memory_events
        rows = await memory_events.upcoming_user_events(db, days=days, limit=10)
        results = []
        for row in rows:
            subj = row.get("subject", "?")
            verb = row.get("verb", "?")
            obj = row.get("object") or ""
            date = row.get("event_date") or ""
            date_short = date[:10] if date else "?"
            results.append(f"[{date_short}] {subj} {verb} {obj}".strip())
        return results
    except Exception:
        logger.debug("Failed to query upcoming events", exc_info=True)
        return []


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
