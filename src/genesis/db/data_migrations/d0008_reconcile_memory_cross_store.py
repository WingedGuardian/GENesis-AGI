"""d0008 — reconcile cross-store memory inconsistencies (ghosts + lying mirrors).

Two silent cross-store defects accumulate in the memory subsystem and were
never repaired until Phase 0 made them observable. This one-time, idempotent
migration cleans whatever exists on ANY install (they are not unique to the
authoring install — the same latent delete-ordering bug shipped in the code):

- ``ghost_points``  — a Qdrant point with NO ``memory_metadata`` row. These are
  *deleted* memories whose vector was stranded when the old ``MemoryStore.delete``
  removed the SQLite rows first and its best-effort Qdrant delete then failed.
  They are unreachable by normal recall (recall joins metadata), so removing the
  orphaned vector is user-invisible bloat removal. Each deleted point's full
  payload is EXPORTED to ``~/.genesis/output`` first as a safety net. Fixed at
  the source by the point-first delete ordering shipped in the same change.
- ``lying_mirror`` — a ``memory_metadata`` row at ``embedding_status='embedded'``
  with NO Qdrant point. The memory still exists (FTS + metadata) but is not
  vector-searchable. Repaired by re-queuing it for embedding: reset the metadata
  mirror to ``pending`` and insert a ``pending_embeddings`` row, so the existing
  ``EmbeddingRecoveryWorker`` restores the real vector on its normal cycle (we do
  NOT embed inside the migration — the provider may be down at boot). This
  RESTORES the vector rather than relabeling — a genuine repair, not a data patch.

Safety:
- **Min-age floor** (``_MIN_AGE_SECONDS``): the write path upserts a Qdrant point
  *before* committing its metadata row, so a point written milliseconds ago looks
  exactly like a ghost; an in-flight ``delete()`` looks like a mirror. Only
  offenders older than the floor are touched, so a concurrent live store/delete is
  never mistaken for corruption (mirrors the embedding reconciler's guard).
- **Payload export before delete**: every ghost's full payload is appended to
  ``~/.genesis/output/d0008_ghost_export.jsonl`` before its point is removed, so
  the deletion is recoverable.
- **Point-first, lock-free deletes** (phase 1) then the batched SQLite cascade
  (phase 2), never holding the WAL write lock across Qdrant network I/O
  (#1179 / ``commit_in_batches``). A ghost whose point delete fails is dropped
  from the cascade, so ``verify()`` still sees it and the migration retries.
- ``get_client()`` raising (Qdrant unreachable) → the migration fails and retries
  next boot; a genuinely empty install is a clean no-op.

migrate()/verify() are SYNC (framework contract) and use their own connections —
never the runtime's async ``rt._db``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.db.data_migrations._util import commit_in_batches
from genesis.env import genesis_db_path
from genesis.qdrant.collections import delete_point, get_client

logger = logging.getLogger(__name__)

requires_operator = False

_COLLECTIONS = ("episodic_memory", "knowledge_base")

# Spare offenders younger than this. The store() path makes the Qdrant point
# durable before the metadata row commits, so a just-written point (or an
# in-flight delete) would otherwise read as an offender. 1h matches the
# embedding reconciler's ``min_age_seconds`` guard.
_MIN_AGE_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(UTC)


def _cutoff_iso() -> str:
    return (_now() - timedelta(seconds=_MIN_AGE_SECONDS)).isoformat()


def _export_path() -> Path:
    return Path.home() / ".genesis" / "output" / "d0008_ghost_export.jsonl"


def _scroll_points(client) -> dict[str, tuple[str, str]]:
    """Return ``{point_id: (created_at, collection)}`` for every point.

    Selective payload (``created_at`` only) keeps memory bounded on a large
    corpus. A point missing ``created_at`` maps to ``""`` (never cleaned — it
    can't be aged, so fail safe and leave it). The collection is recorded so a
    ghost is deleted from exactly where it lives (no guess-both).
    """
    points: dict[str, tuple[str, str]] = {}
    for coll in _COLLECTIONS:
        offset = None
        while True:
            results, offset = client.scroll(
                collection_name=coll,
                limit=1000,
                offset=offset,
                with_payload=["created_at"],
                with_vectors=False,
            )
            for point in results:
                points[str(point.id)] = ((point.payload or {}).get("created_at") or "", coll)
            if offset is None:
                break
    return points


def _enumerate(
    db: sqlite3.Connection, points: dict[str, tuple[str, str]], cutoff: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Compute offenders older than *cutoff*.

    Returns ``(ghosts, mirrors)`` where a ghost is ``(point_id, collection)``
    with no metadata row and a mirror is ``(memory_id, collection)`` for an
    ``embedded`` metadata row with no point.
    """
    meta_ids = {r[0] for r in db.execute("SELECT memory_id FROM memory_metadata")}
    point_ids = set(points)

    ghosts = [
        (pid, points[pid][1])
        for pid in (point_ids - meta_ids)
        if points[pid][0] and points[pid][0] < cutoff
    ]

    mirror_rows = db.execute(
        "SELECT memory_id, collection FROM memory_metadata "
        "WHERE embedding_status = 'embedded' AND created_at < ?",
        (cutoff,),
    ).fetchall()
    mirrors = [(mid, coll) for mid, coll in mirror_rows if mid not in point_ids]
    return ghosts, mirrors


def _export_ghosts(client, ghosts: list[tuple[str, str]]) -> None:
    """Append each ghost's full payload to the export JSONL before deletion.

    Best-effort per point (a retrieve failure must not block the reconcile);
    the file is the recovery net for the otherwise-irreversible point delete.
    """
    if not ghosts:
        return
    path = _export_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _now().isoformat()
    with path.open("a", encoding="utf-8") as fh:
        for pid, coll in ghosts:
            try:
                got = client.retrieve(
                    collection_name=coll, ids=[pid], with_payload=True, with_vectors=False
                )
                payload = got[0].payload if got else None
            except Exception:
                logger.warning("d0008: payload export retrieve failed for %s", pid, exc_info=True)
                payload = None
            fh.write(
                json.dumps(
                    {"exported_at": ts, "point_id": pid, "collection": coll, "payload": payload}
                )
                + "\n"
            )


def migrate() -> dict:
    """Delete ghost points (exported first) and re-queue lying mirrors."""
    client = get_client()  # raises if Qdrant unreachable → retry next boot
    points = _scroll_points(client)
    cutoff = _cutoff_iso()

    ro = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        ghosts, mirrors = _enumerate(ro, points, cutoff)
    finally:
        ro.close()

    if not ghosts and not mirrors:
        return {"ghosts_deleted": 0, "ghost_delete_failed": 0, "mirrors_requeued": 0}

    # Safety net: export ghost payloads BEFORE any deletion.
    _export_ghosts(client, ghosts)

    # ── Phase 1 (lock-free) — delete ghost points from their known collection.
    # A point whose delete fails is dropped from the cascade so verify() still
    # sees it and the migration retries (no half-deleted state).
    deletable: list[str] = []
    ghost_delete_failed = 0
    for pid, coll in ghosts:
        try:
            delete_point(client, collection=coll, point_id=pid)
            deletable.append(pid)
        except Exception:
            logger.warning(
                "d0008: ghost point delete failed for %s — leaving for retry",
                pid,
                exc_info=True,
            )
            ghost_delete_failed += 1

    # ── Phase 2 (batched writes, lock released between batches) ──
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:

        def _sweep_ghost(conn: sqlite3.Connection, pid: str) -> None:
            # A ghost has no metadata row; sweep any stray rows keyed by the id
            # (completeness — normally no-ops since fts_ghosts is already 0).
            conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (pid,))
            conn.execute(
                "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
                (pid, pid),
            )
            conn.execute("DELETE FROM pending_embeddings WHERE memory_id = ?", (pid,))
            conn.execute("DELETE FROM entity_mentions WHERE memory_id = ?", (pid,))

        ghosts_deleted = commit_in_batches(db, deletable, _sweep_ghost)

        def _requeue_mirror(conn: sqlite3.Connection, row: tuple[str, str]) -> None:
            mid, coll = row
            # Re-read the authoritative metadata row FIRST. Data migrations run
            # concurrently with the live server, so between _enumerate and here a
            # MemoryStore.delete() may have removed this memory (its cascade drops
            # metadata + FTS + pending together). Two signals gate the requeue:
            #   * row gone   → deleted mid-migration; skip entirely. Re-queuing
            #                  would recreate a Qdrant-only ghost for a memory the
            #                  user just deleted (never synthesize a row).
            #   * confidence → carried into the queue row so the rebuilt payload
            #                  keeps the memory's real activation weight; the worker
            #                  reads confidence ONLY from the queue row and defaults
            #                  a NULL to 0.5, which would silently re-rank every
            #                  repaired memory to mid-confidence.
            # A deprecated (superseded) mirror is NOT special-cased here: the
            # recovery worker re-stamps a re-embedded deprecated memory's payload as
            # excluded-from-recall (deprecated=True/merged_into, via get_taxonomy),
            # so a normal requeue produces a correctly-flagged point — the same fix
            # also closes the live superseded-while-pending path in the worker.
            mrow = conn.execute(
                "SELECT created_at, confidence FROM memory_metadata WHERE memory_id = ?",
                (mid,),
            ).fetchone()
            if mrow is None:
                return  # deleted concurrently — do not resurrect as a ghost
            meta_created_at, confidence = mrow
            frow = conn.execute(
                "SELECT content, tags FROM memory_fts WHERE memory_id = ?", (mid,)
            ).fetchone()
            content = frow[0] if frow else None
            if not content:
                # No recoverable content → cannot re-embed. Mark 'failed' so the
                # mirror stops lying 'embedded' (honest vectorless state).
                conn.execute(
                    "UPDATE memory_metadata SET embedding_status = 'failed' WHERE memory_id = ?",
                    (mid,),
                )
                return
            # memory_fts stores tags space-separated; pending_embeddings stores them
            # comma-separated (store.py), and the recovery worker rebuilds the
            # life_domain:/project_type: facet payload keys from that comma list —
            # so translate the format or those facets are silently lost on re-embed.
            tags = ",".join((frow[1] or "").split()) or None
            created_at = meta_created_at or _now().isoformat()
            memory_type = "knowledge" if coll == "knowledge_base" else "episodic"
            # A retained queue row (e.g. a reaped 'failed'/'embedded' item) must be
            # RESET to a drainable 'pending' — the recovery worker only drains
            # status='pending', so merely flipping the metadata mirror to 'pending'
            # while a stale queue row sits there would let verify() pass yet never
            # rebuild the vector. Refresh content/tags too so the rebuild is faithful.
            exists = conn.execute(
                "SELECT 1 FROM pending_embeddings WHERE memory_id = ?", (mid,)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE pending_embeddings SET status = 'pending', "
                    "error_message = NULL, content = ?, tags = ?, memory_type = ?, "
                    "collection = ?, confidence = ? WHERE memory_id = ?",
                    (content, tags, memory_type, coll, confidence, mid),
                )
            else:
                conn.execute(
                    "INSERT INTO pending_embeddings "
                    "(id, memory_id, content, memory_type, tags, collection, "
                    " created_at, status, source, confidence, source_session_id, "
                    " transcript_path, source_line_range, extraction_timestamp, "
                    " source_pipeline, source_subsystem) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        mid,
                        content,
                        memory_type,
                        tags,
                        coll,
                        created_at,
                        "pending",
                        "d0008_reconcile",
                        confidence,  # carry the real activation weight, not NULL→0.5
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                )
            conn.execute(
                "UPDATE memory_metadata SET embedding_status = 'pending' WHERE memory_id = ?",
                (mid,),
            )

        mirrors_requeued = commit_in_batches(db, mirrors, _requeue_mirror)
    finally:
        db.close()

    logger.info(
        "d0008: deleted %d ghost points (%d delete failures left for retry), "
        "re-queued %d lying mirrors for re-embedding",
        ghosts_deleted,
        ghost_delete_failed,
        mirrors_requeued,
    )
    return {
        "ghosts_deleted": ghosts_deleted,
        "ghost_delete_failed": ghost_delete_failed,
        "mirrors_requeued": mirrors_requeued,
    }


def verify() -> bool:
    """Complete only when no aged ghost or lying-mirror remains.

    Re-queued mirrors are now ``pending`` (not ``embedded``) so they no longer
    count as mirrors even before the recovery worker restores their vector.
    Qdrant unavailable → not verified → retry next boot (never a false pass).
    """
    try:
        client = get_client()
        points = _scroll_points(client)
    except Exception:
        logger.warning(
            "d0008 verify: Qdrant unavailable — not verified, will retry",
            exc_info=True,
        )
        return False
    ro = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        ghosts, mirrors = _enumerate(ro, points, _cutoff_iso())
    finally:
        ro.close()
    return not ghosts and not mirrors
