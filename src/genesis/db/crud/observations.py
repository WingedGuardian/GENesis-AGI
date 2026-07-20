"""CRUD operations for observations table."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)

# Types that should NEVER expire — the observation IS the authoritative record.
_PERMANENT_TYPES: frozenset[str] = frozenset(
    {
        "feedback_rule",  # Learned behavioral rules
        "genesis_version_baseline",  # Single reference point, replaced on next version
        "cc_version_baseline",  # Single reference point, replaced on next version
        "execution_challenge",  # Task failure post-mortem — resolved manually
    }
)

# Observation types that are Genesis-internal telemetry and should NOT surface
# to the user in morning reports or the dashboard observations panel.
# Everything else surfaces by default — new types are user-visible unless
# explicitly excluded here.  Canonical source — imported by morning_report.py
# and dashboard routes.
INTERNAL_OBS_TYPES: frozenset[str] = frozenset(
    {
        # Reflection / awareness lifecycle
        "awareness_tick",
        "micro_reflection",
        "light_reflection",
        "deep_reflection",
        "reflection_observation",
        "reflection_summary",
        "reflection_output",
        "light_escalation_pending",
        "light_escalation_resolved",
        "light_reflection_candidate",
        # Session/conversation telemetry — per-session pivots; consumed internally
        # by L1 essential-knowledge (queried by type directly), never user-facing.
        "conversation_pivot",
        # Memory internals
        "memory_operation_executed",
        "memory_operation",
        "memory_index",
        "cc_memory_file",
        "merged_observation",
        # Version tracking internals
        "version_current",
        "version_change",
        "genesis_version_change",
        "cc_version_baseline",
        "cc_version_available",
        "genesis_version_baseline",
        "genesis_update_available",
        "genesis_update_failed",
        # Build / project state
        "build_state",
        "project_context",
        "model_downgrade",
        # Triage telemetry
        "triage_depth_3",
        "triage_depth_4",
        # Development internals
        "bugfix_committed",
        "interpretation_correction",
        "scope_clarification",
        "feedback_rule",
        # CC silent-cap detection — per-empty telemetry rows. Internal: only the
        # aggregate infrastructure_alert (raised by the awareness cap detector when
        # a run of these accumulates) surfaces to the user.
        "cc_cap_empty_event",
    }
)

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
    "operational_alert": timedelta(days=3),
    "infrastructure_alert": timedelta(days=3),
    "cc_cap_empty_event": timedelta(days=3),
    "strategic_reflection": timedelta(days=3),
    # ── 1-day (transient) ──────────────────────────────────────────────
    "light_escalation_resolved": timedelta(days=1),
    "light_escalation_pending": timedelta(days=1),
    "task_detected": timedelta(days=1),
    "model_downgrade": timedelta(days=1),
    # ── 7-day (version tracking, operational) ──────────────────────────
    "conversation_pivot": timedelta(days=7),
    "genesis_version_change": timedelta(days=7),
    "memory_index": timedelta(days=7),
    "db_maintenance": timedelta(days=7),
    "backup_verification": timedelta(days=7),
    "scheduled_review": timedelta(days=7),
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
    "escalation_to_user_ego": timedelta(days=7),
    "sentinel_escalated": timedelta(days=7),
    "guardian_diagnosis": timedelta(days=7),
    "infrastructure_drift": timedelta(days=7),
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
    "strategic_analysis": timedelta(days=14),
    # process_reaper dry-run audit trail — the WOULD-KILL evidence an operator
    # reviews before arming the reaper (set_operator_armed). Kept 14d (vs the 3d
    # process_reaper_kill above) so a multi-day audit window survives, and made
    # explicit here so it no longer logs the unknown-type warning every tick.
    "process_reaper_would_kill": timedelta(days=14),
    # ── 30-day (intake signals, need processing time) ──────────────────
    "finding": timedelta(days=30),
    "bugfix_committed": timedelta(days=30),
    "user_signal": timedelta(days=30),
    "user_model_gap": timedelta(days=30),
    "reference_pointer": timedelta(days=30),
    "user_profile": timedelta(days=30),
    "test_isolation_gap": timedelta(days=30),
    "operational_gap": timedelta(days=30),
    "interaction_theme": timedelta(days=30),
    # cognitive self-mod rollback audit (operator-visible correction event)
    "self_mod_rollback": timedelta(days=30),
    # skill-edit Critic shadow verdicts (WS1) — kept 30d (vs 14d for the
    # skill_evolution/skill_proposal events) so a multi-week shadow-bake
    # adjudication window survives. NOT in INTERNAL_OBS_TYPES: flagged
    # (high-priority) verdicts stay visible during the bake.
    "skill_edit_critic": timedelta(days=30),
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
    "cc_memory_staleness": timedelta(days=14),
    # provider_failure resolves on breaker recovery (ProviderEscalation); the
    # explicit TTL is only a backstop for a provider that never comes back
    # (= the previous implicit default, made explicit to silence the warning).
    "provider_failure": timedelta(days=14),
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
        obs_type,
        _DEFAULT_TTL.days,
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
    origin_class: str | None = None,
) -> str | None:
    # Auto-compute content_hash if not provided
    if content_hash is None and content and content.strip():
        content_hash = hashlib.sha256(content.encode()).hexdigest()

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

    params = (
        id,
        person_id,
        source,
        type,
        category,
        content,
        priority,
        speculative,
        created_at,
        expires_at,
        content_hash,
        origin_class,
    )

    if skip_if_duplicate and content_hash is not None:
        # Atomic dedup: one INSERT … WHERE NOT EXISTS statement. A separate
        # SELECT-then-INSERT is NOT a cross-process guard — two writers can
        # both pass the check before either commits. SQLite serializes
        # writers, so a single statement is race-free without needing a
        # schema-level unique index (which would change semantics for every
        # other observation writer).
        cursor = await db.execute(
            """INSERT INTO observations
               (id, person_id, source, type, category, content, priority,
                speculative, created_at, expires_at, content_hash, origin_class)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               WHERE NOT EXISTS (
                   SELECT 1 FROM observations
                   WHERE source = ? AND content_hash = ? AND resolved = 0
               )""",
            (*params, source, content_hash),
        )
        await db.commit()
        if cursor.rowcount == 0:
            logger.debug(
                "Observation dedup: skipping duplicate (source=%s, hash=%s)",
                source,
                content_hash[:12],
            )
            return None
        return id

    await db.execute(
        """INSERT INTO observations
           (id, person_id, source, type, category, content, priority,
            speculative, created_at, expires_at, content_hash, origin_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        params,
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
    origin_class: str | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO observations
           (id, person_id, source, type, category, content, priority,
            speculative, created_at, expires_at, origin_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             person_id = excluded.person_id,
             source = excluded.source, type = excluded.type, category = excluded.category,
             content = excluded.content, priority = excluded.priority,
             speculative = excluded.speculative, expires_at = excluded.expires_at,
             origin_class = excluded.origin_class""",
        (
            id,
            person_id,
            source,
            type,
            category,
            content,
            priority,
            speculative,
            created_at,
            expires_at,
            origin_class,
        ),
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
    rows = await db.execute_fetchall(sql, (source, content_hash))
    return len(rows) > 0


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM observations WHERE id = ?", (id,))
    row = rows[0] if rows else None
    return dict(row) if row else None


async def query(
    db: aiosqlite.Connection,
    *,
    person_id: str | None = None,
    source: str | None = None,
    source_in: list[str] | None = None,
    source_prefix: str | None = None,
    type: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    resolved: bool | None = None,
    exclude_types: tuple[str, ...] | frozenset[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    if sum(map(bool, (source, source_in, source_prefix))) > 1:
        raise ValueError("Specify at most one of 'source', 'source_in', 'source_prefix'")
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
    if source_prefix:
        # Callers pass fixed literals (e.g. "session:"), never user-supplied
        # patterns, so no LIKE-wildcard escaping is needed.
        sql += " AND source LIKE ? || '%'"
        params.append(source_prefix)
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
    if exclude_types:
        type_placeholders = ",".join("?" for _ in exclude_types)
        sql += f" AND type NOT IN ({type_placeholders})"
        params.extend(exclude_types)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = await db.execute_fetchall(sql, params)
    return [dict(r) for r in rows]


async def distinct_unresolved_types(db: aiosqlite.Connection) -> list[str]:
    """Distinct ``type`` values among unresolved observations (dropdown feed)."""
    rows = await db.execute_fetchall(
        "SELECT DISTINCT type FROM observations WHERE resolved = 0 ORDER BY type"
    )
    return [row[0] for row in rows]


async def distinct_unresolved_sources(
    db: aiosqlite.Connection,
    *,
    exclude_types: tuple[str, ...] | frozenset[str] | None = None,
) -> list[str]:
    """Distinct ``source`` values among unresolved observations (dropdown feed).

    ``exclude_types`` drops observations of those types before deriving sources,
    so a source whose unresolved rows are ALL internal types (e.g. a
    ``session:<uuid>`` source with only ``conversation_pivot`` rows) never
    appears as a filter option the list endpoint would then show zero rows for.
    """
    sql = "SELECT DISTINCT source FROM observations WHERE resolved = 0"
    params: list = []
    if exclude_types:
        placeholders = ",".join("?" for _ in exclude_types)
        sql += f" AND type NOT IN ({placeholders})"
        params.extend(exclude_types)
    sql += " ORDER BY source"
    rows = await db.execute_fetchall(sql, params)
    return [row[0] for row in rows]


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
        "WHERE resolved = 0 AND expires_at IS NOT NULL AND datetime(expires_at) < datetime(?)",
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
    category_like: str | None = None,
    category_not_like: str | None = None,
) -> bool:
    """Check if an unresolved observation of this source+type was created recently.

    Used as a cooldown gate to prevent near-duplicate observations from
    LLM reflections that produce different wording for the same system state.
    ``category_like`` / ``category_not_like`` scope the check to categories
    matching (or not matching) a SQL LIKE pattern (e.g. ``"%:user"``) so
    cooldowns can mirror an ego-visibility partition — a reflection visible to
    one ego does not suppress one visible to a different ego.

    Uses Python-side ISO cutoff (not SQLite ``datetime('now')``) so the
    comparison works correctly with ISO 8601 timestamps stored in created_at.
    """
    cutoff = (datetime.now(UTC) - timedelta(minutes=window_minutes)).isoformat()
    query = (
        "SELECT 1 FROM observations "
        "WHERE source = ? AND type = ? AND resolved = 0 "
        "AND created_at > ? "
    )
    params: list = [source, type, cutoff]
    if category_like is not None:
        query += "AND category LIKE ? "
        params.append(category_like)
    if category_not_like is not None:
        # NULL categories are treated as matching (i.e. NOT the excluded
        # pattern) — this mirrors GenesisEgoContextBuilder, which counts a
        # NULL-category observation as Genesis-visible via `category IS NULL`.
        query += "AND (category IS NULL OR category NOT LIKE ?) "
        params.append(category_not_like)
    query += "LIMIT 1"
    rows = await db.execute_fetchall(query, tuple(params))
    return len(rows) > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM observations WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0


async def delete_by_source_and_type(
    db: aiosqlite.Connection,
    *,
    source: str,
    type: str,
) -> int:
    """Delete all observations matching a source + type pair.

    Returns the number of rows deleted.
    """
    cursor = await db.execute(
        "DELETE FROM observations WHERE source = ? AND type = ?",
        (source, type),
    )
    await db.commit()
    return cursor.rowcount


async def resolve_by_source_and_type(
    db: aiosqlite.Connection,
    *,
    source: str,
    type: str,
    resolved_at: str,
    resolution_notes: str,
    category: str | None = None,
) -> int:
    """Resolve all unresolved observations matching a source + type pair.

    ``category`` optionally narrows the resolve to rows with that exact
    category (rows with a NULL/other category are left open). Used for
    slot-scoped self-healing — e.g. a passing cheap git probe clears only
    ``git_cheap`` alerts, never a deep content-corruption alert. Omit it to
    clear every matching row regardless of category (including legacy
    NULL-category rows).

    Returns the number of rows resolved.
    """
    sql = (
        "UPDATE observations SET resolved = 1, resolved_at = ?, "
        "resolution_notes = ? "
        "WHERE source = ? AND type = ? AND resolved = 0"
    )
    params: list[str] = [resolved_at, resolution_notes, source, type]
    if category is not None:
        sql += " AND category = ?"
        params.append(category)
    cursor = await db.execute(sql, params)
    await db.commit()
    return cursor.rowcount


async def resolve_by_content_hash(
    db: aiosqlite.Connection,
    *,
    source: str,
    content_hash: str,
    resolved_at: str,
    resolution_notes: str,
) -> int:
    """Resolve all unresolved observations matching a source + content_hash pair.

    Used for condition-recheck resolution where the writer derives a stable,
    subject-specific content_hash (e.g. one per provider): the recovery signal
    resolves exactly that subject's row and nothing else. Idempotent — a cheap
    no-op when no unresolved row matches.

    Returns the number of rows resolved.
    """
    cursor = await db.execute(
        "UPDATE observations SET resolved = 1, resolved_at = ?, "
        "resolution_notes = ? "
        "WHERE source = ? AND content_hash = ? AND resolved = 0",
        (resolved_at, resolution_notes, source, content_hash),
    )
    await db.commit()
    return cursor.rowcount


async def supersede_except_hash(
    db: aiosqlite.Connection,
    *,
    source: str,
    type: str,
    keep_content_hash: str,
    resolved_at: str,
    resolution_notes: str,
) -> int:
    """Resolve every unresolved source+type row EXCEPT the given content_hash.

    The "exactly one active alert = the current state" pattern (embedding
    backlog, deploy staleness): the caller is about to create/keep the
    current-state row and retires any other-state siblings so a state
    transition never leaves a stale peak-severity row standing.

    Returns the number of rows superseded.
    """
    cursor = await db.execute(
        "UPDATE observations SET resolved = 1, resolved_at = ?, "
        "resolution_notes = ? "
        "WHERE source = ? AND type = ? AND resolved = 0 AND content_hash != ?",
        (resolved_at, resolution_notes, source, type, keep_content_hash),
    )
    await db.commit()
    return cursor.rowcount


async def oldest_created_at(
    db: aiosqlite.Connection,
    *,
    source: str,
    content_like: str,
    resolution_notes: str,
) -> str | None:
    """MIN(created_at) over rows of a source whose content matches
    ``content_like`` (SQL LIKE pattern) and that are either unresolved or
    carry exactly ``resolution_notes``.

    The deploy-staleness >24h escalation anchor: superseded-by-state-change
    rows keep anchoring (so escalating can't reset its own clock) while
    genuinely-recovered rows (different notes) never do.
    """
    cursor = await db.execute(
        "SELECT MIN(created_at) FROM observations "
        "WHERE source = ? AND content LIKE ? "
        "AND (resolved = 0 OR resolution_notes = ?)",
        (source, content_like, resolution_notes),
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def rewrite_resolution_notes(
    db: aiosqlite.Connection,
    *,
    source: str,
    from_notes: str,
    to_notes: str,
    content_like: str | None = None,
) -> int:
    """Rewrite ``resolution_notes`` on a source's rows (optionally content-
    filtered) — retiring rows from note-keyed roles such as the escalation
    anchor above. Returns the number of rows rewritten.
    """
    if content_like is None:
        cursor = await db.execute(
            "UPDATE observations SET resolution_notes = ? "
            "WHERE source = ? AND resolution_notes = ?",
            (to_notes, source, from_notes),
        )
    else:
        cursor = await db.execute(
            "UPDATE observations SET resolution_notes = ? "
            "WHERE source = ? AND resolution_notes = ? AND content LIKE ?",
            (to_notes, source, from_notes, content_like),
        )
    await db.commit()
    return cursor.rowcount


# -- Surfacing ----------------------------------------------------------------


async def get_unsurfaced(
    db: aiosqlite.Connection,
    *,
    priority_filter: tuple[str, ...] = ("critical", "high", "medium"),
    exclude_types: tuple[str, ...] | frozenset[str] = (),
    limit: int = 10,
) -> list[dict]:
    """Return unsurfaced, unresolved observations for user delivery.

    Results are ordered by priority weight (critical > high > medium)
    then by creation time descending (newest first).
    """
    if not priority_filter:
        return []
    prio_placeholders = ",".join("?" for _ in priority_filter)
    sql = (
        "SELECT id, source, type, category, content, priority, created_at "
        "FROM observations "
        f"WHERE surfaced_at IS NULL AND resolved = 0 AND priority IN ({prio_placeholders})"
    )
    params: list = list(priority_filter)

    if exclude_types:
        type_placeholders = ",".join("?" for _ in exclude_types)
        sql += f" AND type NOT IN ({type_placeholders})"
        params.extend(exclude_types)

    sql += (
        " ORDER BY CASE priority "
        "   WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "   WHEN 'medium' THEN 2 ELSE 3 END, "
        " created_at DESC "
        f" LIMIT {limit}"
    )
    async with db.execute(sql, params) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r, strict=False)) for r in rows]


async def count_unsurfaced(
    db: aiosqlite.Connection,
    *,
    priority_filter: tuple[str, ...] = ("critical", "high", "medium"),
    exclude_types: tuple[str, ...] | frozenset[str] = (),
) -> int:
    """COUNT mirror of :func:`get_unsurfaced` (same WHERE, no rows fetched).

    The dashboard badge polls this every 15s — a COUNT keeps that O(1) rows
    instead of pulling up to ``limit`` full rows (with content) to ``len()``
    them, and doesn't silently cap at the fetch limit.
    """
    if not priority_filter:
        return 0
    prio_placeholders = ",".join("?" for _ in priority_filter)
    sql = (
        "SELECT COUNT(*) FROM observations "
        f"WHERE surfaced_at IS NULL AND resolved = 0 AND priority IN ({prio_placeholders})"
    )
    params: list = list(priority_filter)
    if exclude_types:
        type_placeholders = ",".join("?" for _ in exclude_types)
        sql += f" AND type NOT IN ({type_placeholders})"
        params.extend(exclude_types)
    rows = await db.execute_fetchall(sql, params)
    return int(rows[0][0]) if rows else 0


async def mark_surfaced(
    db: aiosqlite.Connection,
    ids: list[str],
    surfaced_at: str,
) -> int:
    """Mark observations as surfaced and increment surfaced_count.

    Uses COALESCE to preserve the original surfaced_at timestamp on
    re-surfacing while always incrementing the count.
    """
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE observations SET surfaced_at = COALESCE(surfaced_at, ?), "
        f"surfaced_count = surfaced_count + 1 "
        f"WHERE id IN ({placeholders})",
        [surfaced_at, *ids],
    )
    await db.commit()
    return cursor.rowcount


async def get_standing(
    db: aiosqlite.Connection,
    *,
    priority_filter: tuple[str, ...] = ("critical", "high", "medium"),
    exclude_types: tuple[str, ...] | frozenset[str] = (),
    threshold: int = 3,
    limit: int = 5,
) -> list[dict]:
    """Return observations surfaced >= threshold times but still unresolved.

    These are "standing items" — known conditions that have been brought
    to attention multiple times without being resolved.
    """
    prio_placeholders = ",".join("?" for _ in priority_filter)
    sql = (
        "SELECT id, source, type, category, content, priority, "
        "created_at, surfaced_at, surfaced_count "
        f"FROM observations WHERE surfaced_count >= ? AND resolved = 0 "
        f"AND priority IN ({prio_placeholders})"
    )
    params: list = [threshold, *priority_filter]
    if exclude_types:
        type_placeholders = ",".join("?" for _ in exclude_types)
        sql += f" AND type NOT IN ({type_placeholders})"
        params.extend(exclude_types)
    sql += (
        " ORDER BY CASE priority "
        "  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "  WHEN 'medium' THEN 2 ELSE 3 END, "
        "surfaced_count DESC, created_at DESC "
        "LIMIT ?"
    )
    params.append(limit)
    rows = await db.execute_fetchall(sql, params)
    return [
        {
            "id": r[0],
            "source": r[1],
            "type": r[2],
            "category": r[3],
            "content": r[4],
            "priority": r[5],
            "created_at": r[6],
            "surfaced_at": r[7],
            "surfaced_count": r[8],
        }
        for r in rows
    ]


async def unsurfaced_counts_by_priority(db: aiosqlite.Connection) -> dict[str, int]:
    """Count unsurfaced, unresolved observations grouped by priority."""
    rows = await db.execute_fetchall(
        "SELECT priority, COUNT(*) FROM observations "
        "WHERE surfaced_at IS NULL AND resolved = 0 "
        "GROUP BY priority"
    )
    return {row[0]: row[1] for row in rows}


async def count_unresolved(
    db: aiosqlite.Connection,
    *,
    exclude_types: tuple[str, ...] | frozenset[str] = (),
) -> int:
    """Count unresolved observations, optionally excluding internal types."""
    sql = "SELECT COUNT(*) FROM observations WHERE resolved = 0"
    params: list = []
    if exclude_types:
        placeholders = ",".join("?" for _ in exclude_types)
        sql += f" AND type NOT IN ({placeholders})"
        params.extend(exclude_types)
    rows = await db.execute_fetchall(sql, params)
    row = rows[0] if rows else None
    return row[0] if row else 0


async def count_unresolved_by_types(
    db: aiosqlite.Connection,
    *,
    types: tuple[str, ...] | frozenset[str],
) -> int:
    """Count unresolved observations matching a set of types."""
    if not types:
        return 0
    placeholders = ",".join("?" for _ in types)
    rows = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM observations WHERE resolved = 0 AND type IN ({placeholders})",
        tuple(types),
    )
    row = rows[0] if rows else None
    return row[0] if row else 0


async def count_external_by_ids(
    db: aiosqlite.Connection,
    ids: list[str],
) -> int:
    """Count observations among ``ids`` stored with external provenance.

    Used by the gate-2 (identity) shadow emit to aggregate the origin of the
    just-accepted user-model deltas: external iff ANY contributing delta row
    carries ``origin_class='external_untrusted'``. NULL/legacy rows count as
    first-party by omission — pre-substrate rows must not manufacture signal.
    """
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM observations "
        f"WHERE id IN ({marks}) "  # noqa: S608 -- placeholders bound
        "AND origin_class = 'external_untrusted'",
        ids,
    )
    row = rows[0] if rows else None
    return row[0] if row else 0


async def count_recent_unresolved_by_type_and_source(
    db: aiosqlite.Connection,
    *,
    type: str,
    source: str,
    since: str,
) -> int:
    """Count unresolved observations of a type+source created after ``since`` (ISO).

    Used by the awareness silent-cap detector to count recent
    ``cc_cap_empty_event`` telemetry rows without embedding raw SQL in the loop.
    """
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM observations "
        "WHERE type = ? AND source = ? AND created_at > ? AND resolved = 0",
        (type, source, since),
    )
    row = rows[0] if rows else None
    return row[0] if row else 0
