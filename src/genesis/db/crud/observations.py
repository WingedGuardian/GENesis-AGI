"""CRUD operations for observations table."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)

# Types that should NEVER expire — the observation IS the authoritative record.
_PERMANENT_TYPES: frozenset[str] = frozenset({
    "feedback_rule",             # Learned behavioral rules
    "genesis_version_baseline",  # Single reference point, replaced on next version
    "cc_version_baseline",       # Single reference point, replaced on next version
})

# Default TTL for types not explicitly listed. Any new type that appears without
# an entry in _TTL_BY_TYPE gets this default + a warning log so we notice and
# categorize it properly.
_DEFAULT_TTL = timedelta(days=14)

# TTL per observation type. Canonical source — observation_writer.py imports from here.
_TTL_BY_TYPE: dict[str, timedelta] = {
    # ── 3-day (ephemeral) ──────────────────────────────────────────────
    "awareness_tick": timedelta(days=3),
    "light_reflection": timedelta(days=3),
    "reflection_summary": timedelta(days=3),
    "reflection_output": timedelta(days=3),
    "surplus_candidate": timedelta(days=3),
    "memory_operation_executed": timedelta(days=3),
    "cc_version_available": timedelta(days=3),
    "version_change": timedelta(days=3),
    "dead_letter_replay": timedelta(days=3),
    "light_reflection_candidate": timedelta(days=3),
    "process_reaper_kill": timedelta(days=3),
    # ── 1-day (transient) ──────────────────────────────────────────────
    "light_escalation_resolved": timedelta(days=1),
    "light_escalation_pending": timedelta(days=1),
    "task_detected": timedelta(days=1),
    "model_downgrade": timedelta(days=1),
    # ── 7-day (version tracking, operational) ──────────────────────────
    "genesis_version_change": timedelta(days=7),
    "memory_index": timedelta(days=7),
    "operational_alert": timedelta(days=7),
    "db_maintenance": timedelta(days=7),
    "backup_verification": timedelta(days=7),
    "scheduled_review": timedelta(days=7),
    "strategic_reflection": timedelta(days=7),
    "micro_reflection": timedelta(days=7),
    "deep_reflection": timedelta(days=7),
    "reflection_observation": timedelta(days=7),
    "version_current": timedelta(days=7),
    "cc_memory_file": timedelta(days=7),
    "contradiction": timedelta(days=7),
    "pending_question": timedelta(days=7),
    "question_response": timedelta(days=7),
    "init_degradation": timedelta(days=7),
    "procedure_quarantined": timedelta(days=7),
    # ── 14-day (learning artifacts & assessments — also the DEFAULT) ───
    "build_state": timedelta(days=14),
    "project_context": timedelta(days=14),
    "learning": timedelta(days=14),
    "learning_regression": timedelta(days=14),
    "skill_evolution": timedelta(days=14),
    "skill_proposal": timedelta(days=14),
    "scope_clarification": timedelta(days=14),
    "interpretation_correction": timedelta(days=14),
    "merged_observation": timedelta(days=14),
    "self_assessment": timedelta(days=14),
    "quality_drift": timedelta(days=14),
    "quality_calibration": timedelta(days=14),
    "user_model_delta": timedelta(days=14),
    "capability_improvement": timedelta(days=14),
    "escalation_to_user_ego": timedelta(days=14),
    "strategic_analysis": timedelta(days=14),
    # ── 30-day (intake signals, need processing time) ──────────────────
    "finding": timedelta(days=30),
    "bugfix_committed": timedelta(days=30),
    "sentinel_escalated": timedelta(days=30),
    "user_signal": timedelta(days=30),
    "user_model_gap": timedelta(days=30),
    "reference_pointer": timedelta(days=30),
    "user_profile": timedelta(days=30),
    "test_isolation_gap": timedelta(days=30),
    "operational_gap": timedelta(days=30),
    "interaction_theme": timedelta(days=30),
    "guardian_diagnosis": timedelta(days=30),
    # ── 60-day (action-required, real issues) ──────────────────────────
    "bug_identified": timedelta(days=60),
    "tech_debt": timedelta(days=60),
    "architecture_risk": timedelta(days=60),
    "concurrency_risk": timedelta(days=60),
    # ── Special: genesis update tracking ───────────────────────────────
    "genesis_update_available": timedelta(days=30),
    "genesis_update_failed": timedelta(days=30),
    # ── Memory operations ──────────────────────────────────────────────
    "memory_operation": timedelta(days=3),
    "quarantined_reflection": timedelta(days=14),
    "code_audit": timedelta(days=14),
}
_TTL_PREFIX: list[tuple[str, timedelta]] = [
    ("triage_depth_", timedelta(days=30)),
]


def _compute_ttl(obs_type: str) -> timedelta | None:
    """Look up TTL for an observation type.

    Returns None only for types in _PERMANENT_TYPES. All other unknown
    types get _DEFAULT_TTL (14 days) with a warning log.
    """
    if obs_type in _PERMANENT_TYPES:
        return None

    ttl = _TTL_BY_TYPE.get(obs_type)
    if ttl is not None:
        return ttl

    for prefix, prefix_ttl in _TTL_PREFIX:
        if obs_type.startswith(prefix):
            return prefix_ttl

    logger.warning(
        "Unknown observation type %r — assigning default TTL of %d days. "
        "Add it to _TTL_BY_TYPE for explicit categorization.",
        obs_type, _DEFAULT_TTL.days,
    )
    return _DEFAULT_TTL


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    source: str,
    type: str,
    content: str,
    priority: str,
    created_at: str,
    person_id: str | None = None,
    category: str | None = None,
    speculative: int = 0,
    expires_at: str | None = None,
    content_hash: str | None = None,
    skip_if_duplicate: bool = False,
) -> str | None:
    # Auto-compute content_hash if not provided
    if content_hash is None and content and content.strip():
        content_hash = hashlib.sha256(content.encode()).hexdigest()

    # Dedup gate: skip if identical unresolved observation exists
    if (
        skip_if_duplicate
        and content_hash is not None
        and await exists_by_hash(db, source=source, content_hash=content_hash, unresolved_only=True)
    ):
        logger.debug("Observation dedup: skipping duplicate (source=%s, hash=%s)", source, content_hash[:12])
        return None

    # Auto-TTL: compute expires_at if not explicitly provided
    if expires_at is None:
        ttl = _compute_ttl(type)
        if ttl:
            try:
                created_dt = datetime.fromisoformat(created_at)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=UTC)
                expires_at = (created_dt + ttl).isoformat()
                logger.debug("Auto-TTL: type=%s, expires_at=%s", type, expires_at)
            except (ValueError, TypeError):
                pass  # Invalid created_at — skip TTL, don't fail the write

    await db.execute(
        """INSERT INTO observations
           (id, person_id, source, type, category, content, priority,
            speculative, created_at, expires_at, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, person_id, source, type, category, content, priority,
         speculative, created_at, expires_at, content_hash),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    source: str,
    type: str,
    content: str,
    priority: str,
    created_at: str,
    person_id: str | None = None,
    category: str | None = None,
    speculative: int = 0,
    expires_at: str | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO observations
           (id, person_id, source, type, category, content, priority,
            speculative, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             person_id = excluded.person_id,
             source = excluded.source, type = excluded.type, category = excluded.category,
             content = excluded.content, priority = excluded.priority,
             speculative = excluded.speculative, expires_at = excluded.expires_at""",
        (id, person_id, source, type, category, content, priority,
         speculative, created_at, expires_at),
    )
    await db.commit()
    return id


async def exists_by_hash(
    db: aiosqlite.Connection,
    *,
    source: str,
    content_hash: str,
    unresolved_only: bool = False,
) -> bool:
    """Check if an observation with this source + content_hash already exists.

    When *unresolved_only* is ``True``, only checks unresolved observations so
    that recurring conditions (e.g., a CPU spike that resolves then recurs) can
    be re-observed.  Default ``False`` checks all observations (permanent dedup).
    """
    sql = "SELECT 1 FROM observations WHERE source = ? AND content_hash = ?"
    if unresolved_only:
        sql += " AND resolved = 0"
    sql += " LIMIT 1"
    cursor = await db.execute(sql, (source, content_hash))
    return (await cursor.fetchone()) is not None


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM observations WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query(
    db: aiosqlite.Connection,
    *,
    person_id: str | None = None,
    source: str | None = None,
    source_in: list[str] | None = None,
    type: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    resolved: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    if source and source_in:
        raise ValueError("Cannot specify both 'source' and 'source_in'")
    sql = "SELECT * FROM observations WHERE 1=1"
    params: list = []
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if source_in:
        placeholders = ",".join("?" for _ in source_in)
        sql += f" AND source IN ({placeholders})"
        params.extend(source_in)
    if type:
        sql += " AND type = ?"
        params.append(type)
    if priority:
        sql += " AND priority = ?"
        params.append(priority)
    if category:
        sql += " AND category = ?"
        params.append(category)
    if resolved is not None:
        sql += " AND resolved = ?"
        params.append(int(resolved))
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def resolve(
    db: aiosqlite.Connection, id: str, *, resolved_at: str, resolution_notes: str
) -> bool:
    cursor = await db.execute(
        "UPDATE observations SET resolved = 1, resolved_at = ?, resolution_notes = ? WHERE id = ?",
        (resolved_at, resolution_notes, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def resolve_batch(
    db: aiosqlite.Connection,
    ids: list[str],
    *,
    resolved_at: str,
    resolution_notes: str,
) -> int:
    """Resolve multiple observations in one statement. Returns count resolved."""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE observations SET resolved = 1, resolved_at = ?, resolution_notes = ? "
        f"WHERE id IN ({placeholders}) AND resolved = 0",
        [resolved_at, resolution_notes, *ids],
    )
    await db.commit()
    return cursor.rowcount


async def increment_retrieved(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "UPDATE observations SET retrieved_count = retrieved_count + 1 WHERE id = ?",
        (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def increment_retrieved_batch(db: aiosqlite.Connection, ids: list[str]) -> int:
    """Increment retrieved_count for multiple observations. Returns count updated."""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE observations SET retrieved_count = retrieved_count + 1 "
        f"WHERE id IN ({placeholders})",
        ids,
    )
    await db.commit()
    return cursor.rowcount


# GROUNDWORK(observation-feedback-loop): called when autonomy/reflection acts on an observation
async def mark_influenced(db: aiosqlite.Connection, id: str) -> bool:
    """Mark an observation as having influenced an action."""
    cursor = await db.execute(
        "UPDATE observations SET influenced_action = 1 WHERE id = ?",
        (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_influenced_batch(db: aiosqlite.Connection, ids: list[str]) -> int:
    """Mark multiple observations as having influenced an action. Returns count updated."""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE observations SET influenced_action = 1 WHERE id IN ({placeholders})",
        ids,
    )
    await db.commit()
    return cursor.rowcount


async def resolve_expired(db: aiosqlite.Connection) -> int:
    """Resolve all unresolved observations past their expires_at.

    Returns the number of observations resolved.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE observations SET resolved = 1, resolved_at = ?, "
        "resolution_notes = 'auto-expired (TTL)' "
        "WHERE resolved = 0 AND expires_at IS NOT NULL AND expires_at < ?",
        (now, now),
    )
    await db.commit()
    return cursor.rowcount


async def resolve_stale_persistent(
    db: aiosqlite.Connection,
    *,
    max_age_days: int = 60,
) -> int:
    """Resolve unresolved persistent observations older than *max_age_days*.

    Only targets low/medium priority.  High/critical persist until manually
    resolved so they remain visible for human review.
    """
    now = datetime.now(UTC).isoformat()
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    cursor = await db.execute(
        "UPDATE observations SET resolved = 1, resolved_at = ?, "
        "resolution_notes = 'auto-resolved (stale persistent)' "
        "WHERE resolved = 0 AND expires_at IS NULL "
        "AND created_at < ? AND priority IN ('low', 'medium')",
        (now, cutoff),
    )
    await db.commit()
    return cursor.rowcount


async def exists_recent_by_type(
    db: aiosqlite.Connection,
    *,
    source: str,
    type: str,
    window_minutes: int = 30,
) -> bool:
    """Check if an unresolved observation of this source+type was created recently.

    Used as a cooldown gate to prevent near-duplicate observations from
    LLM reflections that produce different wording for the same system state.

    Uses Python-side ISO cutoff (not SQLite ``datetime('now')``) so the
    comparison works correctly with ISO 8601 timestamps stored in created_at.
    """
    cutoff = (datetime.now(UTC) - timedelta(minutes=window_minutes)).isoformat()
    cursor = await db.execute(
        "SELECT 1 FROM observations "
        "WHERE source = ? AND type = ? AND resolved = 0 "
        "AND created_at > ? "
        "LIMIT 1",
        (source, type, cutoff),
    )
    return (await cursor.fetchone()) is not None


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM observations WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
