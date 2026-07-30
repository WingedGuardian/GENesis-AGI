"""Periodic cross-store reconcile — Phase 1 memory-integrity repair lane.

Phase 0 (``memory/integrity.py``) detects cross-store drift; the d0008 data
migration repaired what had accumulated, once. The residual lock-free
dual-write windows between SQLite and Qdrant mean drift slowly recurs — this
module is the recurring repair that makes the whole class self-healing,
regardless of which race/crash/legacy path produced an offender. It implements
the ``active`` mode that ``integrity_config`` reserved for Phase 1.

Repairs (d0008 semantics, ported to the runtime's async plane):

- **ghost point** (Qdrant point, no ``memory_metadata`` row): payload exported
  to a date-stamped JSONL under ``~/.genesis/output/`` as a recovery net, then
  the point is deleted from the collection it actually lives in, then any stray
  SQLite rows keyed by the id are swept.
- **lying mirror** (``embedding_status='embedded'``, no point): re-queued for
  re-embedding via ``pending_embeddings.requeue_for_reembed`` so the existing
  ``EmbeddingRecoveryWorker`` rebuilds the real vector on its normal cycle
  (repair restores the vector — never relabels). A mirror with no recoverable
  FTS content is marked ``'failed'`` (honest vectorless state).

Safety rails:

- **Own, never-sampled enumeration.** Phase 0's report may be sampled or
  truncated; repair re-enumerates exactly (same set algebra) and acts only on
  what it proved itself.
- **Truncation asymmetry.** If the point scroll hits its budget, every scanned
  point is still genuinely present, so ghost classification (needs the
  COMPLETE ``memory_metadata`` id set — a full SQLite read) stays sound and
  ghost repair proceeds on the partial set. Mirror classification needs the
  COMPLETE point set (it proves vector ABSENCE), so mirror repair is SKIPPED
  under truncation — mirroring ``run_consistency_check``'s ``lying_mirror=-1``
  sentinel.
- **Min-age floor** (default 1h): the write path makes the point durable before
  its metadata row commits, and a delete is multi-step — a young offender is
  indistinguishable from an in-flight write/delete, so only offenders older
  than the floor are touched. A point with no ``created_at`` cannot be aged →
  never touched.
- **Per-run cap**: bounds nightly work; hitting it sets ``capped`` and logs
  loudly (no silent truncation of the work list).
- **Shared-connection writes.** All SQLite writes go through the runtime's
  serialized async connection — cooperative with live writers by construction
  (d0008's separate sync connection starved runtime writers for ~35s at boot;
  this lane must never do that). NEVER ``rollback()`` on the shared connection.
- **Dependency outage → ``skipped``**, touching nothing — an unreachable
  vector store must never read as "nothing needed repair".

The lane runs as the ``memory_reconcile`` job (04:40 local, see
``runtime/init/memory_integrity.py``) only when ``effective_mode() ==
'active'``; each run persists a ``memory_reconcile_runs`` audit row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.db.connection import open_ro_connection
from genesis.db.crud import pending_embeddings as pending_crud
from genesis.memory._locks import memory_id_lock
from genesis.memory.integrity import _scroll_all_points
from genesis.qdrant import collections as qdrant_ops

logger = logging.getLogger(__name__)

_SAMPLE_CAP = 25  # offender-id samples kept in the run details
_SWEEP_COMMIT_EVERY = 50  # ghost stray-row sweep: commit cadence on the shared conn


@dataclass
class ReconcileResult:
    """One reconcile run's outcome — persisted by the job as an audit row."""

    status: str  # ok | partial | skipped
    ghosts_deleted: int = 0
    ghost_delete_failed: int = 0
    mirrors_requeued: int = 0
    mirrors_skipped_no_content: int = 0
    tombstones_drained: int = 0  # populated by the tombstone drain (PR-2)
    truncated: bool = False
    capped: bool = False
    duration_ms: int = 0
    details: dict = field(default_factory=dict)
    unknown_reason: str | None = None


def _export_path(export_dir: Path, now: datetime) -> Path:
    # Date-stamped (NOT append-forever): a single append-only file refreshes its
    # mtime on every run, so disk_hygiene's age-based prune would never fire.
    return export_dir / f"memory_reconcile_ghost_export-{now.strftime('%Y%m%d')}.jsonl"


def _export_ghosts(
    qdrant_client, ghosts: list[tuple[str, str]], path: Path, now_iso: str
) -> set[str]:
    """Append each ghost's full payload to the export JSONL before deletion.

    Sync (runs via to_thread). Returns the set of point ids whose payload was
    SUCCESSFULLY captured — the export is the recovery net for an irreversible
    point delete, so a ghost whose ``retrieve`` raised (or returned nothing) is
    NOT in the returned set and the caller must NOT delete it: deleting content
    we failed to preserve would silently destroy the only copy. A skipped ghost
    is simply retried on the next run.
    """
    exported_ok: set[str] = set()
    if not ghosts:
        return exported_ok
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for pid, coll in ghosts:
            try:
                got = qdrant_client.retrieve(
                    collection_name=coll, ids=[pid], with_payload=True, with_vectors=False
                )
            except Exception:
                logger.warning("reconcile: ghost export retrieve failed for %s", pid, exc_info=True)
                continue  # not exported → caller leaves it for next run
            if not got:
                # Point vanished between enumeration and export — the export would
                # capture only a null. Treat as an export failure: do NOT authorize
                # the delete+sweep (which could still destroy stray FTS/pending
                # content that IS present). Leave it for the next run.
                logger.warning("reconcile: ghost %s absent at export time — deferring", pid)
                continue
            payload = got[0].payload
            fh.write(
                json.dumps(
                    {
                        "exported_at": now_iso,
                        "point_id": pid,
                        "collection": coll,
                        "payload": payload,
                    }
                )
                + "\n"
            )
            exported_ok.add(pid)
    return exported_ok


async def run_reconcile(
    *,
    db,
    qdrant_client,
    db_path: str | None = None,
    min_age_seconds: int = 3600,
    max_repairs_per_run: int = 500,
    max_points: int = 500_000,
    export_dir: Path | None = None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Enumerate and repair aged cross-store offenders. Returns the run outcome.

    ``db`` is the runtime's shared serialized aiosqlite connection (all writes);
    enumeration reads go through a WAL-aware read-only connection so the write
    lock is never held across the scan. ``now`` is injectable for tests.
    """
    start = time.monotonic()
    now_dt = now or datetime.now(UTC)
    cutoff = (now_dt - timedelta(seconds=min_age_seconds)).isoformat()
    export_dir = export_dir or (Path.home() / ".genesis" / "output")

    # ── Enumerate: Qdrant point set (with ages) across both collections ──
    points: dict[str, tuple[str, str]] = {}  # pid -> (created_at, collection)
    truncated = False
    try:
        for collection in qdrant_ops.COLLECTIONS:
            present, _dep, trunc, ages = await _scroll_all_points(
                qdrant_client,
                collection=collection,
                max_points=max_points,
                collect_created_at=True,
            )
            for pid in present:
                points[pid] = (ages.get(pid, ""), collection)
            truncated = truncated or trunc
    except Exception as exc:
        logger.warning("reconcile: Qdrant enumeration failed — skipping run", exc_info=True)
        return ReconcileResult(
            status="skipped",
            unknown_reason=f"qdrant_unavailable: {type(exc).__name__}",
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    # ── Enumerate: metadata (complete, read-only connection) ──
    ro = await open_ro_connection(db_path) if db_path else await open_ro_connection()
    try:
        cursor = await ro.execute(
            "SELECT memory_id, created_at, embedding_status, collection FROM memory_metadata"
        )
        meta_rows = await cursor.fetchall()
    finally:
        await ro.close()
    meta_ids = {str(r[0]) for r in meta_rows}

    ghosts = [
        (pid, coll)
        for pid, (created, coll) in points.items()
        if pid not in meta_ids and created and created < cutoff
    ]
    if truncated:
        # Vector ABSENCE cannot be proven from a partial point set — a mirror
        # verdict would be unsound. Ghosts stay sound (meta_ids is complete).
        mirrors: list[tuple[str, str]] = []
    else:
        mirrors = [
            (str(mid), str(coll) if coll else "episodic_memory")
            for mid, created, status, coll in meta_rows
            if status == "embedded" and str(mid) not in points and created and str(created) < cutoff
        ]

    # ── Per-run cap (ghosts first — pure cleanup; mirrors restore recall) ──
    capped = False
    work_budget = max(1, int(max_repairs_per_run))
    if len(ghosts) + len(mirrors) > work_budget:
        capped = True
        logger.warning(
            "reconcile: work list capped at %d (found %d ghosts + %d mirrors) — "
            "remainder left for the next run",
            work_budget,
            len(ghosts),
            len(mirrors),
        )
        if len(ghosts) >= work_budget:
            ghosts, mirrors = ghosts[:work_budget], []
        else:
            mirrors = mirrors[: work_budget - len(ghosts)]

    details: dict = {
        "ghost_sample": [pid for pid, _ in ghosts[:_SAMPLE_CAP]],
        "mirror_sample": [mid for mid, _ in mirrors[:_SAMPLE_CAP]],
    }
    if truncated:
        details["mirrors_skipped_truncated"] = True

    # ── Repair: ghosts (export → point delete + stray-row sweep, per id) ──
    ghost_delete_failed = 0
    ghost_export_skipped = 0
    deleted: list[str] = []
    if ghosts:
        now_iso = now_dt.isoformat()
        # Export is a PRECONDITION for deletion: only ghosts whose payload we
        # actually captured are eligible. A ghost whose export failed keeps its
        # point (the only copy of its content) and is retried next run — never
        # delete content the recovery net didn't preserve.
        exported_ok = await asyncio.to_thread(
            _export_ghosts, qdrant_client, ghosts, _export_path(export_dir, now_dt), now_iso
        )
        deletable = [(pid, coll) for pid, coll in ghosts if pid in exported_ok]
        ghost_export_skipped = len(ghosts) - len(deletable)

        # Each ghost's point delete + stray-row sweep runs UNDER the per-id lock,
        # so the recovery worker (which may hold a 'pending' queue row for this id
        # and takes the same lock) cannot upsert the point back between our delete
        # and our sweep — which would strand a vector-only ghost. A ghost has no
        # metadata row by definition, so the sweep is a completeness no-op in the
        # normal case. Shared serialized connection, periodic commit, never rollback.
        for i, (pid, coll) in enumerate(deletable, 1):
            async with memory_id_lock(pid):
                try:
                    await asyncio.to_thread(
                        qdrant_ops.delete_point, qdrant_client, collection=coll, point_id=pid
                    )
                except Exception:
                    logger.warning(
                        "reconcile: ghost point delete failed for %s — left for next run",
                        pid,
                        exc_info=True,
                    )
                    ghost_delete_failed += 1
                    continue
                await db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (pid,))
                await db.execute(
                    "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?", (pid, pid)
                )
                await db.execute("DELETE FROM pending_embeddings WHERE memory_id = ?", (pid,))
                await db.execute("DELETE FROM entity_mentions WHERE memory_id = ?", (pid,))
                deleted.append(pid)
            if i % _SWEEP_COMMIT_EVERY == 0:
                await db.commit()
        await db.commit()

    # ── Repair: mirrors (re-read → requeue for re-embed) ──
    mirrors_requeued = 0
    mirrors_skipped_no_content = 0
    for mid, coll in mirrors:
        # Hold the per-memory-id lock across the re-read → requeue so a
        # concurrent MemoryStore.delete() of this memory cannot land between the
        # status=='embedded' check and the requeue insert (which would resurrect
        # a memory the user just deleted). The re-read is still authoritative:
        # if the memory was deleted (row gone) or changed (status no longer
        # 'embedded') BEFORE we take the lock, we skip — never resurrect or
        # double-queue. Same lock the delete/re-embed paths take (memory/_locks).
        async with memory_id_lock(mid):
            cursor = await db.execute(
                "SELECT created_at, confidence, embedding_status FROM memory_metadata "
                "WHERE memory_id = ?",
                (mid,),
            )
            mrow = await cursor.fetchone()
            if mrow is None or mrow[2] != "embedded":
                continue
            meta_created_at, confidence = mrow[0], mrow[1]
            cursor = await db.execute(
                "SELECT content, tags FROM memory_fts WHERE memory_id = ?", (mid,)
            )
            frow = await cursor.fetchone()
            content = frow[0] if frow else None
            if not content:
                # No recoverable content → cannot re-embed. Stop the mirror lying
                # 'embedded' (honest vectorless state).
                await db.execute(
                    "UPDATE memory_metadata SET embedding_status = 'failed' WHERE memory_id = ?",
                    (mid,),
                )
                await db.commit()
                mirrors_skipped_no_content += 1
                continue
            # memory_fts stores tags space-separated; the queue stores them
            # comma-separated (the worker rebuilds facet payload keys from that).
            tags = ",".join((frow[1] or "").split()) or None
            await pending_crud.requeue_for_reembed(
                db,
                memory_id=mid,
                content=content,
                tags=tags,
                memory_type="knowledge" if coll == "knowledge_base" else "episodic",
                collection=coll,
                created_at=str(meta_created_at) if meta_created_at else now_dt.isoformat(),
                confidence=confidence,
            )
            mirrors_requeued += 1

    if ghost_export_skipped:
        # Ghosts whose export failed were left undeleted (retried next run) —
        # surface it so a persistent export failure is visible, not silent.
        details["ghost_export_skipped"] = ghost_export_skipped
    status = (
        "partial" if (ghost_delete_failed or ghost_export_skipped or capped or truncated) else "ok"
    )
    result = ReconcileResult(
        status=status,
        ghosts_deleted=len(deleted),
        ghost_delete_failed=ghost_delete_failed,
        mirrors_requeued=mirrors_requeued,
        mirrors_skipped_no_content=mirrors_skipped_no_content,
        truncated=truncated,
        capped=capped,
        duration_ms=int((time.monotonic() - start) * 1000),
        details=details,
    )
    if deleted or mirrors_requeued or mirrors_skipped_no_content or ghost_delete_failed:
        logger.info(
            "reconcile: deleted %d ghost points (%d failures), re-queued %d mirrors, "
            "%d mirrors marked failed (no content)%s%s",
            len(deleted),
            ghost_delete_failed,
            mirrors_requeued,
            mirrors_skipped_no_content,
            " [truncated: mirrors skipped]" if truncated else "",
            " [capped]" if capped else "",
        )
    return result
