"""CRUD for reflex_verdicts — the taste corpus (every human verdict in the arc).

One row per human judgment at a card (dismissal, execute tap, PR verdict,
promotion decision). An append-only log: the future judgment layer reads it as
``(context, judgment)`` training examples, so verdicts are never mutated or
deleted. ``context_snapshot`` is a JSON blob of the signal state at judgment
time. ``verdict_point`` / ``verdict`` are CHECK-constrained by the schema
(see ``db/schema/_tables.py``); callers pass values from that vocabulary.

Callers pass the shared SerializedConnection: commit on write, never rollback.
Timestamps are injected ISO-UTC strings — no wall clock in this module.
"""

from __future__ import annotations

import json
import uuid

import aiosqlite


async def record(
    db: aiosqlite.Connection,
    *,
    signal_id: str,
    verdict_point: str,
    verdict: str,
    resolved_by: str,
    context_snapshot: dict,
    now: str,
    diagnosis_id: str | None = None,
    approval_request_id: str | None = None,
) -> str:
    """Append one verdict row (taste corpus). Returns the new row id."""
    verdict_id = uuid.uuid4().hex[:16]
    await db.execute(
        """INSERT INTO reflex_verdicts
           (id, signal_id, diagnosis_id, verdict_point, verdict, resolved_by,
            approval_request_id, context_snapshot, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            verdict_id,
            signal_id,
            diagnosis_id,
            verdict_point,
            verdict,
            resolved_by,
            approval_request_id,
            json.dumps(context_snapshot),
            now,
        ),
    )
    await db.commit()
    return verdict_id


async def list_for_signal(db: aiosqlite.Connection, signal_id: str) -> list[dict]:
    """All verdicts recorded for one signal, oldest first."""
    cursor = await db.execute(
        """SELECT id, signal_id, diagnosis_id, verdict_point, verdict, resolved_by,
                  approval_request_id, context_snapshot, created_at
           FROM reflex_verdicts WHERE signal_id = ? ORDER BY created_at""",
        (signal_id,),
    )
    rows = await cursor.fetchall()
    cols = (
        "id",
        "signal_id",
        "diagnosis_id",
        "verdict_point",
        "verdict",
        "resolved_by",
        "approval_request_id",
        "context_snapshot",
        "created_at",
    )
    return [dict(zip(cols, tuple(r), strict=True)) for r in rows]
