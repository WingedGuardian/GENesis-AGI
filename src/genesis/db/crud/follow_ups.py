"""CRUD operations for follow_ups table — the accountability ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _normalize_scheduled_at(iso_str: str | None) -> str | None:
    """Normalize a scheduled_at ISO timestamp to UTC for safe DB comparison."""
    if not iso_str:
        return iso_str
    dt = datetime.fromisoformat(iso_str)
    dt = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _domain_eq(domain: str | None) -> tuple[str, list[str]]:
    """Build an exact-match domain WHERE fragment for the reader queries.

    Returns ``("", [])`` when ``domain`` is None (no-op — the caller behaves
    byte-identically to before), else ``(" AND domain = ?", [domain])``. Exact
    match only: when a domain is given, NULL-domain rows are excluded. This
    mirrors the cockpit's ``_build_filter_where`` exact-match semantics — it is
    deliberately NOT a second NULL-handling idiom (no "domain OR NULL" union).
    """
    if domain is None:
        return "", []
    return " AND domain = ?", [domain]


async def create(
    db: aiosqlite.Connection,
    *,
    content: str,
    source: str,
    strategy: str,
    reason: str | None = None,
    source_session: str | None = None,
    scheduled_at: str | None = None,
    priority: str = "medium",
    pinned: bool = False,
    kind: str = "follow_up",
    domain: str | None = None,
    goal_id: str | None = None,
    dedup_key: str | None = None,
    id: str | None = None,
) -> str:
    """Create a follow-up and return its ID.

    kind:     'follow_up' (intended for action) or 'tabled' (tracked, not for action).
    domain:   'internal' | 'user_world' | None (None = not yet classified).
    goal_id:  optional link to a unified goal (user_goals.id) for future promotion.
    dedup_key: optional idempotency key. Callers that may re-run (e.g. inbox
              re-evaluation) pass a stable hash so the same recommendation does
              not create duplicate rows; a partial unique index backstops races.
    """
    fid = id or _new_id()
    await db.execute(
        """INSERT INTO follow_ups
           (id, source, source_session, content, reason, strategy,
            scheduled_at, status, priority, pinned, kind, domain, goal_id,
            dedup_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
        (fid, source, source_session, content, reason, strategy,
         _normalize_scheduled_at(scheduled_at), priority, int(pinned),
         kind, domain, goal_id, dedup_key, _now_iso()),
    )
    await db.commit()
    return fid


async def exists_by_dedup_key(
    db: aiosqlite.Connection, dedup_key: str,
) -> bool:
    """Return True if any follow-up already exists with *dedup_key*.

    Dedup spans all statuses so a re-evaluation never recreates a follow-up the
    user already completed/dismissed. NULL/empty keys never match.
    """
    if not dedup_key:
        return False
    cursor = await db.execute(
        "SELECT 1 FROM follow_ups WHERE dedup_key = ? LIMIT 1",
        (dedup_key,),
    )
    return await cursor.fetchone() is not None


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM follow_ups WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_pending(
    db: aiosqlite.Connection,
    *,
    source: str | None = None,
    strategy: str | None = None,
    include_tabled: bool = False,
    domain: str | None = None,
) -> list[dict]:
    """Get pending follow-ups, optionally filtered by source/strategy/domain.

    Tabled follow-ups (kind='tabled') are excluded unless include_tabled=True —
    tabled items are tracked but never dispatched or surfaced as action.
    domain (exact match) scopes to that domain only; None = all domains (no-op).
    """
    query = "SELECT * FROM follow_ups WHERE status = 'pending'"
    params: list[str] = []
    if not include_tabled:
        query += " AND kind = 'follow_up'"
    if source is not None:
        query += " AND source = ?"
        params.append(source)
    if strategy is not None:
        query += " AND strategy = ?"
        params.append(strategy)
    dom_clause, dom_params = _domain_eq(domain)
    query += dom_clause
    params.extend(dom_params)
    query += " ORDER BY created_at ASC"
    cursor = await db.execute(query, params)
    return [dict(row) for row in await cursor.fetchall()]


async def get_by_status(
    db: aiosqlite.Connection,
    status: str,
    *,
    domain: str | None = None,
) -> list[dict]:
    """Get follow-ups by status. domain (exact match) scopes to that domain
    only; None = all domains (no-op, identical to the prior behaviour)."""
    dom_clause, dom_params = _domain_eq(domain)
    cursor = await db.execute(
        f"SELECT * FROM follow_ups WHERE status = ?{dom_clause} "
        "ORDER BY created_at ASC",
        (status, *dom_params),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_actionable(
    db: aiosqlite.Connection, *, limit: int = 50, include_tabled: bool = False,
    domain: str | None = None,
) -> list[dict]:
    """Get follow-ups needing attention: pending, failed, blocked.

    Tabled follow-ups are excluded unless include_tabled=True. Capped at `limit`
    to prevent unbounded growth from flooding contexts. domain (exact match)
    scopes to that domain only — applied in SQL BEFORE the LIMIT, so the cap
    samples within the scoped domain (None = all domains, identical to before).
    """
    kind_clause = "" if include_tabled else "AND kind = 'follow_up' "
    dom_clause, dom_params = _domain_eq(domain)
    cursor = await db.execute(
        "SELECT * FROM follow_ups WHERE status IN ('pending', 'failed', 'blocked') "
        f"{kind_clause}{dom_clause} "
        "ORDER BY CASE priority "
        "  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "  WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC "
        "LIMIT ?",
        (*dom_params, limit),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_scheduled_due(
    db: aiosqlite.Connection, *, include_tabled: bool = False,
) -> list[dict]:
    """Get scheduled follow-ups whose time has arrived.

    Tabled follow-ups are excluded unless include_tabled=True.
    """
    kind_clause = "" if include_tabled else "AND kind = 'follow_up' "
    cursor = await db.execute(
        "SELECT * FROM follow_ups "
        "WHERE strategy = 'scheduled_task' AND status = 'pending' "
        f"{kind_clause}"
        "AND scheduled_at IS NOT NULL "
        "AND datetime(scheduled_at) <= datetime('now') "
        "ORDER BY scheduled_at ASC",
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_linked_active(
    db: aiosqlite.Connection, *, include_tabled: bool = False,
) -> list[dict]:
    """Get follow-ups linked to surplus tasks that are in flight.

    Tabled follow-ups are excluded unless include_tabled=True.
    """
    kind_clause = "" if include_tabled else "AND kind = 'follow_up' "
    cursor = await db.execute(
        "SELECT * FROM follow_ups "
        "WHERE linked_task_id IS NOT NULL "
        "AND status IN ('scheduled', 'in_progress') "
        f"{kind_clause}"
        "ORDER BY created_at ASC",
    )
    return [dict(row) for row in await cursor.fetchall()]


async def update_status(
    db: aiosqlite.Connection,
    id: str,
    status: str,
    *,
    resolution_notes: str | None = None,
    blocked_reason: str | None = None,
    verified_at: str | None = None,
    verification_notes: str | None = None,
) -> bool:
    """Update follow-up status. Sets completed_at on terminal states."""
    parts = ["status = ?"]
    params: list[str | None] = [status]
    if status in ("completed", "failed"):
        parts.append("completed_at = ?")
        params.append(_now_iso())
    if resolution_notes is not None:
        parts.append("resolution_notes = ?")
        params.append(resolution_notes)
    if blocked_reason is not None:
        parts.append("blocked_reason = ?")
        params.append(blocked_reason)
    if verified_at is not None:
        parts.append("verified_at = ?")
        params.append(verified_at)
    if verification_notes is not None:
        parts.append("verification_notes = ?")
        params.append(verification_notes)
    params.append(id)
    cursor = await db.execute(
        f"UPDATE follow_ups SET {', '.join(parts)} WHERE id = ?",
        params,
    )
    await db.commit()
    return cursor.rowcount > 0


async def link_task(
    db: aiosqlite.Connection,
    id: str,
    surplus_task_id: str,
) -> bool:
    """Link a follow-up to a surplus task and mark as scheduled."""
    cursor = await db.execute(
        "UPDATE follow_ups SET linked_task_id = ?, status = 'scheduled' WHERE id = ?",
        (surplus_task_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def escalate(
    db: aiosqlite.Connection,
    id: str,
    target: str,
) -> bool:
    """Mark follow-up as escalated to ego or promoted to task."""
    cursor = await db.execute(
        "UPDATE follow_ups SET escalated_to = ? WHERE id = ?",
        (target, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_pinned(
    db: aiosqlite.Connection,
    id: str,
    pinned: bool,
) -> bool:
    """Pin or unpin a follow-up."""
    cursor = await db.execute(
        "UPDATE follow_ups SET pinned = ? WHERE id = ?",
        (int(pinned), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_summary_counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Get counts by status for dashboard badges."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM follow_ups GROUP BY status"
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}


async def get_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
    exclude_source: str | None = None,
    source_mode: str = "all",
) -> list[dict]:
    """Get recent follow-ups for dashboard display.

    Parameters
    ----------
    exclude_source:
        If set, exclude rows where ``source LIKE %{exclude_source}%``.
        Use ``"ego"`` to hide ego-generated follow-ups from the user view.
    source_mode:
        Filter by source category:
        - ``"all"`` — no filter (default)
        - ``"mine"`` — only ``foreground_session`` source
        - ``"system"`` — everything except ``foreground_session``
        Takes precedence over ``exclude_source`` when not ``"all"``.
    """
    if source_mode == "mine":
        cursor = await db.execute(
            "SELECT * FROM follow_ups "
            "WHERE source = 'foreground_session' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    elif source_mode == "system":
        cursor = await db.execute(
            "SELECT * FROM follow_ups "
            "WHERE source != 'foreground_session' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    elif exclude_source:
        cursor = await db.execute(
            "SELECT * FROM follow_ups "
            "WHERE source NOT LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{exclude_source}%", limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM follow_ups ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(row) for row in await cursor.fetchall()]


async def get_by_source(
    db: aiosqlite.Connection,
    source: str,
    *,
    status: str | None = None,
    days: int | None = None,
    limit: int = 50,
    include_tabled: bool = False,
) -> list[dict]:
    """Get follow-ups by source, optionally filtered by status and recency.

    Tabled follow-ups are excluded unless include_tabled=True.
    """
    query = "SELECT * FROM follow_ups WHERE source = ?"
    params: list[str | int] = [source]
    if not include_tabled:
        query += " AND kind = 'follow_up'"
    if status:
        query += " AND status = ?"
        params.append(status)
    if days:
        query += " AND created_at >= datetime('now', ? || ' days')"
        params.append(f"-{days}")
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(query, params)
    return [dict(row) for row in await cursor.fetchall()]


async def get_recently_resolved(
    db: aiosqlite.Connection,
    *,
    source: str | None = None,
    days: int = 7,
    limit: int = 20,
) -> list[dict]:
    """Get recently completed follow-ups, optionally filtered by source."""
    days = max(1, days)
    query = "SELECT * FROM follow_ups WHERE status = 'completed'"
    params: list[str | int] = []
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " AND completed_at >= datetime('now', ? || ' days')"
    params.append(f"-{days}")
    query += " ORDER BY completed_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(query, params)
    return [dict(row) for row in await cursor.fetchall()]


async def purge_completed(
    db: aiosqlite.Connection,
    *,
    max_age_days: int = 30,
) -> int:
    """Delete completed/failed follow-ups older than *max_age_days*.

    Pinned follow-ups are always preserved regardless of age.
    Returns the number of records deleted.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    cursor = await db.execute(
        "DELETE FROM follow_ups "
        "WHERE status IN ('completed', 'failed') "
        "AND pinned = 0 "
        "AND completed_at IS NOT NULL AND completed_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def get_recently_completed(
    db: aiosqlite.Connection,
    *,
    hours: int = 24,
    limit: int = 5,
    domain: str | None = None,
) -> list[dict]:
    """Get follow-ups completed within the given time window.

    domain (exact match) scopes to that domain only and is applied in SQL
    BEFORE the LIMIT (so the cap samples within the scoped domain — a Python
    post-filter would be wrong here). None = all domains (no-op).
    """
    dom_clause, dom_params = _domain_eq(domain)
    cursor = await db.execute(
        "SELECT content, resolution_notes FROM follow_ups "
        "WHERE status = 'completed' "
        f"AND completed_at >= datetime('now', ? || ' hours'){dom_clause} "
        "ORDER BY completed_at DESC LIMIT ?",
        (f"-{hours}", *dom_params, limit),
    )
    return [dict(row) for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Cockpit support — mutations + paginated/filtered query (consumed by the
# dashboard Follow-ups tab). Pure data layer; kept here alongside the table.
# ---------------------------------------------------------------------------

_VALID_KIND = {"follow_up", "tabled"}
_VALID_DOMAIN = {"internal", "user_world"}
_VALID_PRIORITY = {"low", "medium", "high", "critical"}
_VALID_STATUS = {
    "pending", "scheduled", "in_progress", "completed", "failed", "blocked",
}

# Allowlisted sort keys → ORDER BY fragment (never interpolate caller input).
# Every fragment floats pinned rows to the top (pinned is a "keep visible"
# flag, honored regardless of the chosen sort). The status sort ranks by
# actionability — active work first, terminal states last — not alphabetically
# (plain ``status ASC`` buried pending/blocked items under completed ones).
_SORT_MAP: dict[str, str] = {
    "priority": (
        "pinned DESC, "
        "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC"
    ),
    "created_desc": "pinned DESC, created_at DESC",
    "created_asc": "pinned DESC, created_at ASC",
    "status": (
        "pinned DESC, "
        "CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 "
        "WHEN 'pending' THEN 2 WHEN 'scheduled' THEN 3 "
        "WHEN 'failed' THEN 4 WHEN 'completed' THEN 5 ELSE 6 END, "
        "created_at DESC"
    ),
    "source": "pinned DESC, source ASC, created_at DESC",
}


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    """Permanently delete a follow-up. Returns True if a row was removed."""
    cursor = await db.execute("DELETE FROM follow_ups WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0


async def set_kind(db: aiosqlite.Connection, id: str, kind: str) -> bool:
    """Move a follow-up between the 'follow_up' and 'tabled' lanes."""
    if kind not in _VALID_KIND:
        raise ValueError(f"invalid kind {kind!r}; must be one of {sorted(_VALID_KIND)}")
    cursor = await db.execute(
        "UPDATE follow_ups SET kind = ? WHERE id = ?", (kind, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_domain(db: aiosqlite.Connection, id: str, domain: str | None) -> bool:
    """Set/override a follow-up's domain (or clear it with None)."""
    if domain is not None and domain not in _VALID_DOMAIN:
        raise ValueError(
            f"invalid domain {domain!r}; must be one of {sorted(_VALID_DOMAIN)} or None"
        )
    cursor = await db.execute(
        "UPDATE follow_ups SET domain = ? WHERE id = ?", (domain, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_priority(db: aiosqlite.Connection, id: str, priority: str) -> bool:
    """Set a follow-up's priority (validated against the schema CHECK set)."""
    if priority not in _VALID_PRIORITY:
        raise ValueError(
            f"invalid priority {priority!r}; must be one of {sorted(_VALID_PRIORITY)}"
        )
    cursor = await db.execute(
        "UPDATE follow_ups SET priority = ? WHERE id = ?", (priority, id),
    )
    await db.commit()
    return cursor.rowcount > 0


# Batch mutations — single-statement WHERE id IN (...) so a multi-row cockpit
# action is one transaction (no silent partial failure on a 200-id selection).
async def delete_batch(db: aiosqlite.Connection, ids: list[str]) -> int:
    """Permanently delete multiple follow-ups in one statement. Returns count."""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"DELETE FROM follow_ups WHERE id IN ({placeholders})", ids,
    )
    await db.commit()
    return cursor.rowcount


async def set_kind_batch(db: aiosqlite.Connection, ids: list[str], kind: str) -> int:
    """Move multiple follow-ups between lanes in one statement. Returns count."""
    if kind not in _VALID_KIND:
        raise ValueError(f"invalid kind {kind!r}; must be one of {sorted(_VALID_KIND)}")
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE follow_ups SET kind = ? WHERE id IN ({placeholders})",
        [kind, *ids],
    )
    await db.commit()
    return cursor.rowcount


async def update_status_batch(
    db: aiosqlite.Connection,
    ids: list[str],
    status: str,
    *,
    resolution_notes: str | None = None,
) -> int:
    """Update status for multiple follow-ups in one statement. Returns count.

    Mirrors update_status: stamps completed_at on terminal states.
    """
    if status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {status!r}; must be one of {sorted(_VALID_STATUS)}"
        )
    if not ids:
        return 0
    parts = ["status = ?"]
    params: list[str | None] = [status]
    if status in ("completed", "failed"):
        parts.append("completed_at = ?")
        params.append(_now_iso())
    if resolution_notes is not None:
        parts.append("resolution_notes = ?")
        params.append(resolution_notes)
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE follow_ups SET {', '.join(parts)} WHERE id IN ({placeholders})",
        [*params, *ids],
    )
    await db.commit()
    return cursor.rowcount


async def get_distinct_sources(db: aiosqlite.Connection) -> list[str]:
    """Distinct source values present, for cockpit filter dropdowns."""
    cursor = await db.execute(
        "SELECT DISTINCT source FROM follow_ups ORDER BY source"
    )
    return [row[0] for row in await cursor.fetchall()]


def _build_filter_where(
    *,
    kind: str | None,
    domain: str | None,
    status: str | None,
    source: str | None,
    search: str | None,
    status_exclude: list[str] | None = None,
) -> tuple[str, list]:
    """Build a parameterized WHERE clause shared by query_page/count_filtered.

    Only static column/clause text is assembled here; every caller value is
    bound via a ``?`` placeholder. ``domain='__null__'`` matches unclassified
    rows (domain IS NULL). ``status_exclude`` hides terminal states (e.g.
    completed/failed) and is ignored when an explicit ``status`` is requested
    — filtering *to* a status and excluding it are mutually exclusive intents.
    """
    clauses: list[str] = []
    params: list = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if domain is not None:
        if domain == "__null__":
            clauses.append("domain IS NULL")
        else:
            clauses.append("domain = ?")
            params.append(domain)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    elif status_exclude:
        placeholders = ",".join("?" for _ in status_exclude)
        clauses.append(f"status NOT IN ({placeholders})")
        params.extend(status_exclude)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if search:
        clauses.append("(content LIKE ? OR reason LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


async def count_filtered(
    db: aiosqlite.Connection,
    *,
    kind: str | None = None,
    domain: str | None = None,
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
    status_exclude: list[str] | None = None,
) -> int:
    """Count follow-ups matching the cockpit filters."""
    where, params = _build_filter_where(
        kind=kind, domain=domain, status=status, source=source, search=search,
        status_exclude=status_exclude,
    )
    cursor = await db.execute(f"SELECT COUNT(*) FROM follow_ups{where}", params)
    row = await cursor.fetchone()
    return row[0] if row else 0


async def query_page(
    db: aiosqlite.Connection,
    *,
    kind: str | None = None,
    domain: str | None = None,
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
    status_exclude: list[str] | None = None,
    sort: str = "priority",
    offset: int = 0,
    limit: int = 50,
) -> list[dict]:
    """Paginated/sorted/filtered follow-up query for the cockpit.

    ``sort`` is allowlisted (see ``_SORT_MAP``); unknown values fall back to
    priority. ``domain='__null__'`` matches rows with no domain set.
    ``status_exclude`` hides terminal states (ignored when ``status`` is set).
    """
    where, params = _build_filter_where(
        kind=kind, domain=domain, status=status, source=source, search=search,
        status_exclude=status_exclude,
    )
    order = _SORT_MAP.get(sort, _SORT_MAP["priority"])
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    cursor = await db.execute(
        f"SELECT * FROM follow_ups{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [dict(row) for row in await cursor.fetchall()]
