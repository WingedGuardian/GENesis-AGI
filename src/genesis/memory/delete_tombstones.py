"""Delete-intent tombstones — cross-process memory-delete durability.

A memory delete that cannot complete (Qdrant unreachable → ``MemoryStore.delete``
defers, touching nothing) must not be forgotten: the intent is recorded as a
``deferred_work_queue`` row (``work_type='memory_deferred_delete'``) written
through SQLite — which, unlike the process-local ``memory/_locks.py`` registry,
is visible to EVERY Genesis process (genesis-server, the genesis-memory MCP
server, one-off scripts). Consumers:

- the nightly reconcile lane (``memory/integrity_repair.py``) drains open
  tombstones FIRST, re-attempting the full delete before classifying offenders,
  and excludes attempted ids from its ghost/mirror sets for that run;
- ``EmbeddingRecoveryWorker`` skips re-embedding any memory with an open
  tombstone (a deferred delete leaves metadata + queue row intact, so without
  this check the worker would rebuild a vector that is about to be deleted);
- ``pending_embeddings.requeue_for_reembed`` refuses (in the same SQL
  statement, atomically vs writers in any process) to requeue a tombstoned id.

No dedicated store: this reuses ``deferred_work_queue`` per its existing
conventions — delete-intent IS deferred work; retention rides the queue's
``prune_terminal``; the reconcile lane is the work_type's consumer (there is no
central dispatcher). Dedup keys on the queue's ``(topic, category,
signal_type)`` payload identity with ``topic = memory_id``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import deferred_work as deferred_crud

logger = logging.getLogger(__name__)

WORK_TYPE = "memory_deferred_delete"
_CATEGORY = "memory_delete"
_PRIORITY = 50  # mid-band: behind resilience retries, ahead of batch worklists


async def enqueue_tombstone(db: aiosqlite.Connection, *, memory_id: str, reason: str) -> bool:
    """Record a delete intent for *memory_id*.

    Returns True iff the intent is DURABLY RECORDED — either a new row was
    written or an open tombstone already exists (dedup: repeated deferred
    deletes of the same memory collapse to one row; the existing row IS the
    recorded intent). False only when the write genuinely failed (logged; the
    caller's delete result already says ``deferred`` either way).

    The check-then-insert dedup is not atomic across processes — both
    genesis-server and the genesis-memory MCP process can enqueue — but a
    duplicate open tombstone is harmless: ``complete_open_tombstones`` closes
    every open row for the id and a double-drained delete is idempotent.
    """
    try:
        open_count = await deferred_crud.count_open_by_identity(
            db,
            work_type=WORK_TYPE,
            topic=memory_id,
            category=_CATEGORY,
            signal_type=None,
        )
        if open_count:
            return True  # intent already durably recorded
        now = datetime.now(UTC).isoformat()
        await deferred_crud.create(
            db,
            id=uuid.uuid4().hex,
            work_type=WORK_TYPE,
            priority=_PRIORITY,
            payload_json=json.dumps(
                {
                    "topic": memory_id,
                    "category": _CATEGORY,
                    "signal_type": None,
                    "memory_id": memory_id,
                }
            ),
            deferred_at=now,
            deferred_reason=reason,
            created_at=now,
            staleness_policy="drain",
        )
        return True
    except Exception:
        # Best-effort: the delete already reported deferred=True; a failed
        # tombstone write must not mask that result. The consistency check
        # still surfaces the resulting drift if this intent is lost.
        logger.error("tombstone enqueue failed for %s", memory_id, exc_info=True)
        return False


async def has_open_tombstone(db: aiosqlite.Connection, *, memory_id: str) -> bool:
    """True if a pending/processing delete intent exists for *memory_id*."""
    return (
        await deferred_crud.count_open_by_identity(
            db,
            work_type=WORK_TYPE,
            topic=memory_id,
            category=_CATEGORY,
            signal_type=None,
        )
        > 0
    )


async def complete_open_tombstones(db: aiosqlite.Connection, *, memory_id: str) -> int:
    """Close every open tombstone for *memory_id* after a successful delete."""
    return await deferred_crud.complete_open_by_identity(
        db,
        work_type=WORK_TYPE,
        topic=memory_id,
        category=_CATEGORY,
        signal_type=None,
        completed_at=datetime.now(UTC).isoformat(),
    )


def memory_id_from_row(row: dict) -> str | None:
    """Extract the target memory_id from a tombstone queue row (None if corrupt)."""
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return None
    mid = payload.get("memory_id") or payload.get("topic")
    return str(mid) if mid else None
