"""CRUD operations for procedural_memory table."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.learning.procedural.credential_scrub import WITHHELD, scrub_value


def _json_or_none(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value)


def _source_session(source: str | None) -> str | None:
    """The session ``source`` (JSON) names, for ``created_session_id``.

    Written only by the INSERT statements below, never by an update, so the
    lesson cap's count of a session's creations survives a refine that rewrites
    ``source``. None when ``source`` is absent, not JSON, or names no session.
    """
    try:
        parsed = json.loads(source) if source else None
    except (ValueError, TypeError):
        return None
    sid = parsed.get("session_id") if isinstance(parsed, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _live_clause(live_only: bool) -> str:
    """SQL that leaves out held lessons (external origin, not reviewed)."""
    if not live_only:
        return ""
    from genesis.learning.procedural.lesson_shape import LIVE_LESSON_SQL

    return f" AND ({LIVE_LESSON_SQL})"


def _kind_clause(lessons: bool | None) -> str:
    """SQL keeping only lessons (True), only earned rows (False), or both (None).

    Chosen in SQL so rows of the other kind can never fill a caller's limit.
    """
    if lessons is None:
        return ""
    return " AND lesson IS NOT NULL" if lessons else " AND lesson IS NULL"


_logger = logging.getLogger(__name__)
# The free-text fields a procedure carries (extracted, so possibly holding what a
# session typed). Every writer below passes them through _scrub (#4859).
_SCRUBBED_FIELDS = (
    "principle", "scenario", "steps", "attempted_workarounds",
    "extraction_context", "lesson", "failure_modes",
)


# Fields stored as JSON documents; the others are prose and are never parsed.
_JSON_FIELDS = frozenset({"steps", "attempted_workarounds", "lesson", "failure_modes"})
_WITHHELD = WITHHELD


def json_field_document(key: str, value):
    """The parsed document of a JSON field given as text (surrounding whitespace
    allowed), else None: prose, a non-JSON field, or text that does not parse."""
    if key not in _JSON_FIELDS or not isinstance(value, str) or value.lstrip()[:1] not in ("[", "{"):
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def scrub_field(key: str, value, found: list[str]):
    """One field's value with credential spans redacted, on parsed values (a JSON
    field given as text is parsed first, so a cut can never break the document).
    A string the scrub cannot process is withheld on its own (siblings and shape
    survive, ``found`` names "withheld"); a field given as one such string is
    withheld whole, as a JSON document for a JSON field. Raises only when scrubbed
    keys collide. Shared by the writers below and the audit."""
    parsed = json_field_document(key, value)
    if parsed is not None:
        return json.dumps(scrub_value(parsed, found), ensure_ascii=False)
    result = scrub_value(value, found)
    if result == _WITHHELD and key in _JSON_FIELDS and isinstance(value, str):
        return json.dumps([_WITHHELD])
    return result


def _scrub(id: str, fields: dict) -> dict:
    """Redact credential spans from the free-text fields (``scrub_field``). What the
    scrub cannot process is withheld and the row is still written: protecting a
    secret never drops a write."""
    found: list[str] = []
    out = dict(fields)
    for key in _SCRUBBED_FIELDS:
        value = out.get(key)
        if value is None:
            continue
        try:
            before = found.count("withheld")
            out[key] = scrub_field(key, value, found)
            if found.count("withheld") > before:
                _logger.error("Procedure %s: credential scrub could not run on part of %s; withheld", id, key)
        except Exception:
            _logger.error("Procedure %s: credential scrub failed on %s; withheld", id, key, exc_info=True)
            out[key] = [_WITHHELD] if isinstance(value, list) else (
                json.dumps([_WITHHELD]) if key in _JSON_FIELDS and isinstance(value, str) else _WITHHELD
            )
    if found:
        _logger.warning("Procedure %s: redacted credential spans (%s)", id, ", ".join(sorted(set(found))))
    return out


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    created_at: str,
    scenario: str | None = None,
    person_id: str | None = None,
    draft: int = 1,
    attempted_workarounds: list | None = None,
    version: int = 1,
    activation_tier: str = "DORMANT",
    tool_trigger: list[str] | None = None,
    success_count: int = 0,
    confidence: float = 0.0,
    source: str | None = None,
    promotion_history: str | None = None,
    principle_embedding: bytes | None = None,
    extraction_context: str | None = None,
    first_mover: int = 0,
    trigger_spec: dict | None = None,
    lesson: dict | None = None,
    deprecated_reason: str | None = None,
) -> str:
    """Insert a row. A ``deprecated_reason`` writes it deprecated in the same
    statement, so no reader ever sees it live in between."""
    clean = _scrub(id, {
        "principle": principle, "scenario": scenario, "steps": steps,
        "attempted_workarounds": attempted_workarounds,
        "extraction_context": extraction_context, "lesson": lesson,
    })
    principle, scenario, steps = clean["principle"], clean["scenario"], clean["steps"]
    attempted_workarounds = clean["attempted_workarounds"]
    extraction_context, lesson = clean["extraction_context"], clean["lesson"]
    await db.execute(
        """INSERT INTO procedural_memory
           (id, person_id, task_type, principle, scenario, steps, tools_used,
            context_tags, draft, attempted_workarounds, version, created_at,
            activation_tier, tool_trigger, success_count, confidence,
            source, promotion_history, principle_embedding,
            extraction_context, first_mover, trigger_spec, lesson,
            deprecated, deprecated_reason, created_session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, person_id, task_type, principle, scenario,
         json.dumps(steps), json.dumps(tools_used),
         json.dumps(context_tags), draft,
         json.dumps(attempted_workarounds) if attempted_workarounds else None,
         version, created_at, activation_tier,
         json.dumps(tool_trigger) if tool_trigger else None,
         success_count, confidence, source, promotion_history,
         principle_embedding, extraction_context, first_mover,
         _json_or_none(trigger_spec), _json_or_none(lesson),
         1 if deprecated_reason else 0, deprecated_reason, _source_session(source)),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    created_at: str,
    person_id: str | None = None,
    draft: int = 1,
    attempted_workarounds: list | None = None,
    version: int = 1,
    activation_tier: str = "DORMANT",
    tool_trigger: list[str] | None = None,
    success_count: int = 0,
    confidence: float = 0.0,
    source: str | None = None,
    promotion_history: str | None = None,
    trigger_spec: dict | None = None,
    lesson: dict | None = None,
) -> str:
    """Idempotent write: insert or update on conflict.

    Mirrors `create()`'s `success_count` / `confidence` plumbing so that
    callers building idempotent seed/teach paths (e.g., batch seed scripts,
    future explicit-teach upserters) can't accidentally land rows with the
    invisible-to-recall (draft=0, success_count=0, confidence=0.0)
    profile that bit `procedure_store` before this fix.

    ``trigger_spec`` and ``lesson`` replace the stored values only when
    given, so an upsert that does not carry them cannot turn a lesson back
    into an earned row.
    """
    clean = _scrub(id, {
        "principle": principle, "steps": steps,
        "attempted_workarounds": attempted_workarounds, "lesson": lesson,
    })
    principle, steps = clean["principle"], clean["steps"]
    attempted_workarounds, lesson = clean["attempted_workarounds"], clean["lesson"]
    await db.execute(
        """INSERT INTO procedural_memory
           (id, person_id, task_type, principle, steps, tools_used, context_tags,
            draft, attempted_workarounds, version, created_at,
            activation_tier, tool_trigger, success_count, confidence,
            source, promotion_history, trigger_spec, lesson, created_session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             person_id = excluded.person_id,
             task_type = excluded.task_type, principle = excluded.principle,
             steps = excluded.steps, tools_used = excluded.tools_used,
             context_tags = excluded.context_tags, draft = excluded.draft,
             attempted_workarounds = excluded.attempted_workarounds,
             version = excluded.version,
             activation_tier = excluded.activation_tier,
             tool_trigger = excluded.tool_trigger,
             success_count = excluded.success_count,
             confidence = excluded.confidence,
             source = COALESCE(procedural_memory.source, excluded.source),
             promotion_history = COALESCE(procedural_memory.promotion_history, excluded.promotion_history),
             trigger_spec = COALESCE(excluded.trigger_spec, procedural_memory.trigger_spec),
             lesson = COALESCE(excluded.lesson, procedural_memory.lesson)""",
        (id, person_id, task_type, principle, json.dumps(steps), json.dumps(tools_used),
         json.dumps(context_tags), draft,
         json.dumps(attempted_workarounds) if attempted_workarounds else None,
         version, created_at, activation_tier,
         json.dumps(tool_trigger) if tool_trigger else None,
         success_count, confidence, source, promotion_history,
         _json_or_none(trigger_spec), _json_or_none(lesson), _source_session(source)),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM procedural_memory WHERE id = ?", (id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def find_by_task_type(
    db: aiosqlite.Connection,
    task_type: str,
) -> dict | None:
    """Find the best active procedure with an exact task_type match.

    Returns the highest-confidence non-deprecated, non-quarantined row,
    or None. Used by conflict detection before store.
    """
    cursor = await db.execute(
        """SELECT * FROM procedural_memory
           WHERE task_type = ? AND deprecated = 0 AND quarantined = 0
           ORDER BY confidence DESC LIMIT 1""",
        (task_type,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def find_by_context_overlap(
    db: aiosqlite.Connection,
    context_tags: list[str],
    *,
    jaccard_threshold: float = 0.7,
    exclude_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Find active procedures with high context_tag Jaccard overlap.

    Loads active procedures and filters in Python (SQLite lacks native
    JSON set operations). Returns rows with overlap >= threshold.
    """
    query_tags = set(context_tags)
    if not query_tags:
        return []

    # A held lesson is untrusted text: it never counts as an established procedure.
    rows = await list_active(db, limit=200, live_only=True)
    scored = []
    for row in rows:
        if exclude_id and row["id"] == exclude_id:
            continue
        raw = row.get("context_tags", "[]")
        row_tags = set(json.loads(raw) if isinstance(raw, str) else raw)
        union = query_tags | row_tags
        if not union:
            continue
        jaccard = len(query_tags & row_tags) / len(union)
        if jaccard >= jaccard_threshold:
            scored.append((jaccard, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in scored[:limit]]


async def list_by_task_type(
    db: aiosqlite.Connection,
    task_type: str,
    *,
    person_id: str | None = None,
    limit: int | None = 50,
    live_only: bool = False,
    lessons: bool | None = None,
) -> list[dict]:
    # Exclude deprecated/quarantined rows: the dominant caller is the procedure
    # novelty/dedup gate, which must NOT let a deprecated procedure suppress a new
    # one. (matcher.find_best_match already skips them in Python, so this is a
    # consistent no-op there.)
    sql = (
        "SELECT * FROM procedural_memory "
        "WHERE task_type = ? AND deprecated = 0 AND quarantined = 0"
    )
    params: list = [task_type]
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += _live_clause(live_only)
    sql += _kind_clause(lessons)
    sql += " ORDER BY confidence DESC"
    if limit is not None:  # None: every row (an identity lookup must see them all)
        sql += " LIMIT ?"
        params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_tier(
    db: aiosqlite.Connection,
    tier: str,
    *,
    limit: int | None = 100,
    lessons: bool | None = None,
) -> list[dict]:
    """List active procedures at a specific activation tier."""
    sql = (
        "SELECT * FROM procedural_memory "  # noqa: S608 - fixed SQL fragments only
        "WHERE activation_tier = ? AND deprecated = 0 AND quarantined = 0"
        + _kind_clause(lessons)
        + " ORDER BY confidence DESC"
    )
    params: list = [tier]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def list_lessons(
    db: aiosqlite.Connection,
    *,
    include_quarantined: bool = False,
    include_deprecated: bool = False,
) -> list[dict]:
    """Every non-deprecated row that carries a lesson record.

    UNBOUNDED on purpose, unlike :func:`list_by_tier`: lessons are authored
    rather than earned, every one is meant to be live, and a cap would drop
    the oldest silently. Quarantined rows are left out, as every sibling
    listing leaves them out, unless a caller that manages them asks.
    """
    quarantine = "" if include_quarantined else " AND quarantined = 0"
    deprecated = "" if include_deprecated else " AND deprecated = 0"
    cursor = await db.execute(
        "SELECT * FROM procedural_memory WHERE lesson IS NOT NULL"
        + deprecated
        + quarantine
        + " ORDER BY created_at"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_tool_trigger(
    db: aiosqlite.Connection,
    tool_name: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """List active procedures whose tool_trigger JSON array contains tool_name."""
    # SQLite JSON: json_each extracts array elements for matching
    cursor = await db.execute(
        """SELECT p.* FROM procedural_memory p, json_each(p.tool_trigger) j
           WHERE j.value = ? AND p.deprecated = 0 AND p.quarantined = 0
           ORDER BY p.confidence DESC LIMIT ?""",
        (tool_name, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


# Written once at insert: the lesson cap counts a session's creations by them.
_INSERT_ONLY = ("created_session_id", "created_at")


async def update(db: aiosqlite.Connection, id: str, **fields) -> bool:
    if not fields:
        return False
    fields = _scrub(id, fields)
    fixed = [k for k in _INSERT_ONLY if k in fields]
    if fixed:
        raise ValueError(f"procedural_memory {', '.join(fixed)} is set at insert only")
    # Serialize JSON fields
    for key in ("steps", "tools_used", "context_tags", "failure_modes",
                 "attempted_workarounds", "tool_trigger", "promotion_history"):
        if key in fields and isinstance(fields[key], list):
            fields[key] = json.dumps(fields[key])
    for key in ("source", "trigger_spec", "lesson"):
        if key in fields and isinstance(fields[key], dict):
            fields[key] = json.dumps(fields[key])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [id]
    cursor = await db.execute(
        f"UPDATE procedural_memory SET {set_clause} WHERE id = ?", values
    )
    await db.commit()
    return cursor.rowcount > 0


async def record_invocation(db: aiosqlite.Connection, id: str) -> bool:
    """Count a procedure read: a model recalled it (surfaced into context).

    Atomically bumps ``invocation_count`` and stamps ``last_used``. This is the
    top-of-funnel usage signal — distinct from ``record_success``/``record_failure``
    (outcomes). Returns True if the procedure exists.
    """
    cursor = await db.execute(
        "UPDATE procedural_memory "
        "SET invocation_count = invocation_count + 1, last_used = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def quarantine(db: aiosqlite.Connection, id: str) -> bool:
    """Mark a procedure as quarantined (excluded from matching)."""
    cursor = await db.execute(
        "UPDATE procedural_memory SET quarantined = 1 WHERE id = ?", (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def unquarantine(db: aiosqlite.Connection, id: str) -> bool:
    """Remove quarantine flag from a procedure."""
    cursor = await db.execute(
        "UPDATE procedural_memory SET quarantined = 0 WHERE id = ?", (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_active(
    db: aiosqlite.Connection,
    *,
    person_id: str | None = None,
    limit: int | None = 100,
    live_only: bool = False,
    lessons: bool | None = None,
) -> list[dict]:
    """List non-deprecated, non-quarantined procedures.

    ``live_only`` leaves out held lessons and ``lessons`` picks a kind, both in
    SQL, before the limit applies.
    """
    sql = "SELECT * FROM procedural_memory WHERE deprecated = 0 AND quarantined = 0"
    params: list = []
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += _live_clause(live_only) + _kind_clause(lessons)
    sql += " ORDER BY confidence DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def list_quarantined(db: aiosqlite.Connection) -> list[dict]:
    """List all quarantined procedures."""
    cursor = await db.execute(
        "SELECT * FROM procedural_memory WHERE quarantined = 1"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM procedural_memory WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
