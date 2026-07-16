"""CRUD for ``alert_events`` — persisted alert/incident store (WS-2 M10).

Replaces the in-memory, per-process, one-generation ``_alert_history`` dict
(``mcp/health/__init__.py``) with a durable open-set. ONE designated writer
(the awareness tick) calls :func:`reconcile_open_set` with the currently-firing
alert set; the table itself is the transition state:

- a new firing alert → an open row (``resolved_at IS NULL``);
- an alert that stops firing → its open row gets ``resolved_at`` stamped.

Idempotency is by construction: the partial UNIQUE index
``idx_ae_open_alert`` (on ``alert_id`` WHERE ``resolved_at IS NULL``) permits at
most one open row per alert, so ``INSERT OR IGNORE`` is safe even when the
runtime process and the health-MCP process race to reconcile the same set.

NOTE: severity/message are captured at first-fire and not rewritten while an
alert stays open — a mid-incident escalation (WARNING→CRITICAL for the same
alert_id) keeps the original open row. Acceptable for v1; the incident's
created_at is the load-bearing fact.
"""

from __future__ import annotations

import uuid

import aiosqlite


async def reconcile_open_set(
    db: aiosqlite.Connection,
    *,
    active: list[dict],
    now: str,
) -> dict[str, int]:
    """Reconcile the durable open-set against the currently-firing ``active`` alerts.

    ``active`` is a list of ``{alert_id, source, severity, message}`` dicts.
    Opens rows for newly-firing alerts (idempotent via the partial unique
    index) and resolves any open row whose ``alert_id`` is no longer firing.
    Returns ``{"opened": n, "resolved": n}``.
    """
    active_ids = [a["alert_id"] for a in active]

    opened = 0
    for a in active:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO alert_events
               (id, alert_id, source, severity, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex,
                a["alert_id"],
                a["source"],
                a["severity"],
                a["message"],
                now,
            ),
        )
        opened += cursor.rowcount or 0

    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        cursor = await db.execute(
            f"UPDATE alert_events SET resolved_at = ? "  # noqa: S608 - placeholders are bound params
            f"WHERE resolved_at IS NULL AND alert_id NOT IN ({placeholders})",
            (now, *active_ids),
        )
    else:
        # nothing firing → resolve every open row
        cursor = await db.execute(
            "UPDATE alert_events SET resolved_at = ? WHERE resolved_at IS NULL",
            (now,),
        )
    resolved = cursor.rowcount or 0

    await db.commit()
    return {"opened": opened, "resolved": resolved}


async def list_open(db: aiosqlite.Connection) -> list[dict]:
    """All currently-open alerts (resolved_at IS NULL), newest first."""
    cursor = await db.execute(
        "SELECT id, alert_id, source, severity, message, created_at, resolved_at "
        "FROM alert_events WHERE resolved_at IS NULL ORDER BY created_at DESC"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_recent(db: aiosqlite.Connection, *, since: str) -> list[dict]:
    """Alert history (open + resolved) created at/after ``since`` (ISO), newest first."""
    cursor = await db.execute(
        "SELECT id, alert_id, source, severity, message, created_at, resolved_at "
        "FROM alert_events WHERE created_at >= ? ORDER BY created_at DESC",
        (since,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def prune_older_than(db: aiosqlite.Connection, *, days: int = 90) -> int:
    """Delete RESOLVED alerts older than ``days``. Never deletes an open alert.

    Signature matches the drip-retention prune contract.
    """
    cursor = await db.execute(
        "DELETE FROM alert_events "
        "WHERE resolved_at IS NOT NULL AND created_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    await db.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0
