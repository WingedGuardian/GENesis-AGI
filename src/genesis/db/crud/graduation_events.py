"""CRUD for ``graduation_events`` — voice graduation quarantine (W0).

Landing store for ``POST /v1/voice/graduate``: typed events synthesized on the
voice edge (claims, never raw transcripts) land here verbatim with
``disposition='pending'``. The W2 policy drainer (separate PR) is the only
thing that moves a row out of ``pending`` — this module deliberately ships no
disposition-update helper until that PR exists.

Dedup is by construction: ``event_id`` UNIQUE + ``INSERT OR IGNORE`` turns the
edge outbox's at-least-once delivery into effectively-once landing. The
transport contract is 2xx only after the row is durable, so
:func:`insert_event` commits before returning.
"""

from __future__ import annotations

import json
import uuid

import aiosqlite


async def insert_event(
    db: aiosqlite.Connection,
    *,
    event_id: str,
    schema_version: int,
    type: str,
    source: str,
    occurred_at: str,
    received_at: str,
    payload: dict,
    provenance: dict,
) -> bool:
    """Land a graduation event in quarantine (``disposition='pending'``).

    Returns True if the row was inserted, False on an ``event_id`` replay
    (caller answers ``duplicate``). Commits before returning — the transport
    contract is 2xx only after the INSERT is durable.
    """
    cursor = await db.execute(
        """INSERT OR IGNORE INTO graduation_events
           (id, event_id, schema_version, type, source,
            occurred_at, received_at, payload, provenance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid.uuid4().hex,
            event_id,
            schema_version,
            type,
            source,
            occurred_at,
            received_at,
            json.dumps(payload),
            json.dumps(provenance),
        ),
    )
    inserted = (cursor.rowcount or 0) == 1
    if not inserted:
        # OR IGNORE swallows ANY conflict (CHECK/NOT NULL too, not just the
        # event_id UNIQUE). Only a genuine replay may answer "duplicate" —
        # anything else ignored here is silent data loss (e.g. a future
        # EVENT_TYPES addition missing from an old DB's frozen CHECK list)
        # and must surface as an error so the edge retries and logs show it.
        check = await db.execute(
            "SELECT 1 FROM graduation_events WHERE event_id = ?", (event_id,)
        )
        if await check.fetchone() is None:
            await db.rollback()
            raise aiosqlite.IntegrityError(
                f"graduation_events insert ignored for non-duplicate {event_id!r} "
                "(constraint violation — schema/validator drift?)"
            )
    await db.commit()
    return inserted


async def get_by_event_id(db: aiosqlite.Connection, *, event_id: str) -> dict | None:
    """Fetch one event by its edge-assigned ``event_id`` (tests / ops / drainer)."""
    cursor = await db.execute(
        "SELECT id, event_id, schema_version, type, source, occurred_at, "
        "received_at, payload, provenance, disposition, memory_id, "
        "disposition_reason, disposed_at "
        "FROM graduation_events WHERE event_id = ?",
        (event_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def prune_older_than(db: aiosqlite.Connection, *, days: int = 90) -> int:
    """Delete DISPOSITIONED rows older than ``days`` (by ``disposed_at``).

    NEVER deletes a pending row — pending is the drainer's inbox and the
    audit obligation. Signature matches the drip-retention prune contract.

    Contract for the W2 drainer: ``disposed_at`` MUST be written as UTC
    ISO8601 — the cutoff compares lexically against SQLite's UTC
    ``datetime('now')`` (house pattern, cf. ``alert_events``).
    """
    cursor = await db.execute(
        "DELETE FROM graduation_events "
        "WHERE disposition != 'pending' AND disposed_at IS NOT NULL "
        "AND disposed_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    await db.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0
