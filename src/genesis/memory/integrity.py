"""Cross-backend memory consistency checker — Phase 0 "make silence loud".

Read-only. Verifies that the three episodic backends agree: a memory's
``memory_metadata`` row, its Qdrant vector, and its ``memory_fts`` entry. The
episodic write path fans out to all three with no cross-store transaction
(Qdrant and SQLite cannot share one), so a partial write or a half-completed
delete leaves a silent inconsistency that degrades recall without any error.

Classification (six classes):

- ``lying_mirror``    — ``embedding_status='embedded'`` but NO Qdrant point in
  either collection. The memory believes it is vector-searchable; it is not.
  (The #918/#921 bug class.)
- ``ghost_points``    — a Qdrant point with no ``memory_metadata`` row at all.
  Can surface as an unattributable recall hit.
- ``fts_ghosts``      — ``memory_fts`` rows with no metadata row.
- ``fts_invisible``   — metadata rows with no ``memory_fts`` row (invisible to
  keyword/hybrid search).
- ``unexpected_vector`` — ``embedding_status='fts5_only'`` (deliberately
  keyword-only) yet a Qdrant point exists.
- ``deprecated_divergence`` — the ``deprecated`` flag disagrees between the
  SQLite row and the Qdrant payload.

Existence is checked **collection-agnostically**: a memory's point may live in a
different Qdrant collection than its ``memory_metadata.collection`` column
claims (that column is documented-unreliable, and the delete path probes both
collections). Checking "does a point exist in EITHER collection" avoids
false lying-mirror reports.

Fail-closed in the correct direction: if Qdrant is unreachable, the report is
``status='unknown'`` with a reason — a dependency outage must NEVER be reported
as data corruption. Reads go through ``open_ro_connection`` (WAL-aware
``mode=ro``), never the runtime write lock.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

from genesis.db.connection import open_ro_connection
from genesis.db.crud.memory import count_fts_metadata_drift
from genesis.qdrant import collections as qdrant_ops

logger = logging.getLogger(__name__)

_CLASSES = (
    "lying_mirror",
    "ghost_points",
    "fts_ghosts",
    "fts_invisible",
    "unexpected_vector",
    "deprecated_divergence",
)


@dataclass
class MemoryConsistencyReport:
    """One consistency-check result. ``status`` is the headline verdict."""

    status: str  # healthy | degraded | unknown
    counts: dict[str, int] = field(default_factory=dict)
    offender_sample: dict[str, list[str]] = field(default_factory=dict)
    total_rows: int = 0
    sampled_rows: int = 0
    sample_fraction: float = 1.0
    truncated: bool = False
    unknown_reason: str | None = None
    duration_ms: int = 0

    @property
    def total_findings(self) -> int:
        # A -1 count is the "not computed under truncation" sentinel, never a
        # finding — clamp it out so the total is honest.
        return sum(max(self.counts.get(c, 0), 0) for c in _CLASSES)


async def _scroll_all_points(
    qdrant_client,
    *,
    collection: str,
    max_points: int,
) -> tuple[set[str], set[str], bool]:
    """Enumerate a collection's point ids (sync client → to_thread).

    Returns ``(present_ids, deprecated_payload_ids, truncated)``. ``truncated``
    is True if the *max_points* budget was hit before exhausting the collection.
    """
    present: set[str] = set()
    deprecated_ids: set[str] = set()
    offset: str | None = None
    truncated = False
    page_limit = max(1, min(1000, max_points))
    while True:
        points, offset = await asyncio.to_thread(
            qdrant_ops.scroll_points,
            qdrant_client,
            collection=collection,
            limit=page_limit,
            offset=offset,
        )
        for p in points:
            pid = str(p["id"])
            present.add(pid)
            payload = p.get("payload") or {}
            if payload.get("deprecated"):
                deprecated_ids.add(pid)
        if offset is None:
            break  # collection exhausted — results are COMPLETE
        if len(present) >= max_points:
            truncated = True  # budget hit with more points remaining
            break
    return present, deprecated_ids, truncated


# Severe classes are search-path ABSENCES: a memory that exists but cannot be
# found via a retrieval lane (missing vector, or missing FTS row). These are the
# silent-recall-degradation the system exists to surface, so they trip 'degraded'
# at a small absolute floor. The remaining classes are pollution/attribution
# issues (extra points, stale flags) — real but lower-severity, so they use a
# looser corpus-fraction threshold to avoid a permanently-red tile over churn.
_SEVERE_CLASSES = ("lying_mirror", "fts_invisible")
_POLLUTION_CLASSES = ("ghost_points", "unexpected_vector", "deprecated_divergence", "fts_ghosts")


async def run_consistency_check(
    *,
    db_path: str | None = None,
    qdrant_client,
    sample_fraction: float = 1.0,
    max_points: int = 500_000,
    severe_min_count: int = 5,
    pollution_min_count: int = 50,
    pollution_fraction: float = 0.01,
    max_offender_sample: int = 25,
) -> MemoryConsistencyReport:
    """Run the read-only cross-backend consistency check.

    ``sample_fraction`` is recorded for provenance; the scan is exact unless the
    ``max_points`` budget is hit (then ``truncated=True`` and ``lying_mirror`` is
    reported as ``-1`` = not-computed, since absence cannot be proven from a
    partial point set). At single-user scale a full exact scan is cheap.

    Status (severity-aware, Option A): ``degraded`` when the severe search-path-
    absence classes reach ``severe_min_count`` OR the pollution classes reach
    ``max(pollution_min_count, ceil(pollution_fraction * total))``.
    """
    start = time.monotonic()
    conn = await open_ro_connection(db_path) if db_path else await open_ro_connection()
    try:
        rows = await conn.execute_fetchall(
            "SELECT memory_id, collection, embedding_status, deprecated FROM memory_metadata"
        )
        total_rows = len(rows)
        if total_rows == 0:
            # Empty install / fresh state: nothing to be inconsistent about.
            return MemoryConsistencyReport(
                status="healthy",
                counts={c: 0 for c in _CLASSES},
                total_rows=0,
                sampled_rows=0,
                sample_fraction=sample_fraction,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        meta_ids: set[str] = set()
        embedded_ids: set[str] = set()
        fts5_only_ids: set[str] = set()
        deprecated_meta_ids: set[str] = set()
        for mid, _collection, status, deprecated in rows:
            mid = str(mid)
            meta_ids.add(mid)
            if status == "embedded":
                embedded_ids.add(mid)
            elif status == "fts5_only":
                fts5_only_ids.add(mid)
            if deprecated:
                deprecated_meta_ids.add(mid)

        # FTS <-> metadata drift (reuse the existing set-difference helper).
        # NOTE: count_fts_metadata_drift is a whole-table distinct-id set
        # difference — NOT scoped by embedding_status/deprecated and count-only
        # (no offender ids). Safe today: store.store() writes an FTS row for
        # EVERY memory regardless of status, and mark_superseded sets
        # deprecated=1 without deleting the FTS row, so no legitimate row trips
        # fts_invisible. fts_invisible is treated as SEVERE; if a future path can
        # leave a metadata row without an FTS row, revisit the severity/scoping.
        # Offender-id extraction for the FTS classes is a Phase-1 triage add.
        try:
            fts_ghosts, fts_invisible = await count_fts_metadata_drift(conn)
        except Exception:
            logger.warning("consistency: FTS drift query failed", exc_info=True)
            return MemoryConsistencyReport(
                status="unknown",
                counts={c: 0 for c in _CLASSES},
                total_rows=total_rows,
                sample_fraction=sample_fraction,
                unknown_reason="fts_unavailable",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Qdrant enumeration across BOTH collections (collection-agnostic
        # existence). Any transport failure → 'unknown', never a false finding.
        present_ids: set[str] = set()
        deprecated_qdrant_ids: set[str] = set()
        truncated = False
        try:
            for collection in qdrant_ops.COLLECTIONS:
                p, dep, trunc = await _scroll_all_points(
                    qdrant_client, collection=collection, max_points=max_points
                )
                present_ids |= p
                deprecated_qdrant_ids |= dep
                truncated = truncated or trunc
        except Exception as exc:
            # Fail-closed in the correct direction: ANY failure enumerating
            # Qdrant (enumerated transport errors, an exotic client exception, a
            # None client) yields 'unknown' — never a 'degraded'/'healthy' that
            # would report a dependency outage as data corruption. The set-
            # algebra below is deliberately OUTSIDE this try, so it only runs on
            # a fully-enumerated point set.
            logger.warning(
                "consistency: Qdrant enumeration failed — reporting unknown", exc_info=True
            )
            return MemoryConsistencyReport(
                status="unknown",
                counts={c: 0 for c in _CLASSES},
                total_rows=total_rows,
                sample_fraction=sample_fraction,
                unknown_reason=f"qdrant_unavailable: {type(exc).__name__}",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Classify via set algebra (collection-agnostic existence).
        # INVARIANT (ghost_points): both Qdrant collections are populated
        # EXCLUSIVELY via memory.store.store() -> create_metadata (episodic
        # writes, knowledge_ingest, and the KB orchestrator all route through
        # it; embedding_recovery only re-embeds existing metadata). So every
        # legitimate point owns a metadata row, and present-minus-meta is a true
        # orphan set. A future DIRECT upsert into either collection that bypasses
        # create_metadata would manufacture false ghosts — update this if that
        # ever happens.
        ghost_points = present_ids - meta_ids
        unexpected_vector = fts5_only_ids & present_ids
        # deprecated divergence only for embedded rows that HAVE a point.
        embedded_present = embedded_ids & present_ids
        deprecated_divergence = {
            mid
            for mid in embedded_present
            if (mid in deprecated_meta_ids) != (mid in deprecated_qdrant_ids)
        }
        if truncated:
            # Cannot prove a vector's ABSENCE from a partial point set.
            lying_mirror: set[str] | None = None
        else:
            lying_mirror = embedded_ids - present_ids

        counts = {
            "lying_mirror": (-1 if lying_mirror is None else len(lying_mirror)),
            "ghost_points": len(ghost_points),
            "fts_ghosts": int(fts_ghosts),
            "fts_invisible": int(fts_invisible),
            "unexpected_vector": len(unexpected_vector),
            "deprecated_divergence": len(deprecated_divergence),
        }
        offender_sample = {
            "lying_mirror": sorted(lying_mirror)[:max_offender_sample] if lying_mirror else [],
            "ghost_points": sorted(ghost_points)[:max_offender_sample],
            "unexpected_vector": sorted(unexpected_vector)[:max_offender_sample],
            "deprecated_divergence": sorted(deprecated_divergence)[:max_offender_sample],
        }

        # Severity-aware status (Option A). Severe = search-path absences;
        # pollution = extra/stale artifacts. A -1 (not-computed under truncation)
        # never counts toward a threshold.
        severe_total = sum(counts[c] for c in _SEVERE_CLASSES if counts[c] >= 0)
        pollution_total = sum(counts[c] for c in _POLLUTION_CLASSES if counts[c] >= 0)
        pollution_threshold = max(pollution_min_count, math.ceil(pollution_fraction * total_rows))
        status = (
            "degraded"
            if severe_total >= severe_min_count or pollution_total >= pollution_threshold
            else "healthy"
        )

        return MemoryConsistencyReport(
            status=status,
            counts=counts,
            offender_sample=offender_sample,
            total_rows=total_rows,
            sampled_rows=total_rows,
            sample_fraction=sample_fraction,
            truncated=truncated,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    finally:
        await conn.close()
