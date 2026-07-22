"""d0005 — purge operational-telemetry rows that polluted the knowledge base.

Non-KB-routing surplus tasks (action/maintenance/monitor/pipeline-intermediate:
db_maintenance, disk_cleanup, model_eval, j9_eval_batch, backup_verification,
research_query_gen, prompt_review_*, cc_memory_staleness, …) routed their
point-in-time output through ``run_intake`` as curated knowledge before the
``KB_ROUTING_TASK_TYPES`` gate landed (fix(surplus): stop operational-telemetry
pollution). Result: the knowledge_base grew to ~71% ``source_pipeline=surplus``
ops telemetry. The gate stops NEW writes; this migration removes the historical
rows on EVERY install (idempotent, post-boot) — the pollution shipped in the
code since 2026-06-10, so peer installs carry it too; no per-install hand-fix.

Match is deterministic + tightly scoped (near-zero false-delete): a
``knowledge_unit`` whose ``source_pipeline='surplus'`` AND
``domain='intelligence.surplus'`` AND whose body's FIRST LINE equals a
non-KB-routing task's ``single_item`` title (``task_type.replace("_"," ").title()``,
e.g. "Db Maintenance", "Research Query Gen"). Insight-producing tasks kept in the
KB (brainstorm/audit/anticipatory/self_unblock/prompt_effectiveness_review) have
DISTINCT titles or real LLM-authored titles, so they are never matched (the
title sets are disjoint — asserted by test_d0005_purge_surplus_ops_telemetry).

Cross-store: a KB unit lives in Qdrant + memory_metadata + memory_fts +
knowledge_units + knowledge_fts (+ memory_links / pending_embeddings /
entity_mentions cascades) — the same fan-out ``MemoryStore.delete()`` performs.
Deleting only the Qdrant point + knowledge_units would leave the junk
RESURFACING via degraded FTS5 recall (``memory_fts`` is cross-collection), so we
reproduce the full cascade in raw SQL (``migrate()``/``verify()`` are SYNC with
their own connections — they cannot call the async store).

Ordering: the Qdrant point is deleted BEFORE the SQLite rows for that unit; if
the Qdrant delete fails (transient), the unit's SQLite rows are LEFT intact so
``verify()`` still sees it as a candidate → the migration is marked failed and
retries next boot (no half-deleted orphan vector). ``get_client()`` failing
entirely raises → same retry. Idempotent: a missing point deletes as a no-op,
and the DELETEs are no-ops once purged / on a fresh install (no such rows).

migrate()/verify() are SYNC (framework contract, cf. d0003/d0004). Own
connections only — never the runtime's async ``rt._db``.
"""

from __future__ import annotations

import logging
import sqlite3

from genesis.env import genesis_db_path
from genesis.qdrant.collections import delete_point, get_client

logger = logging.getLogger(__name__)

requires_operator = False

# First-line title signatures of the single_item ops-telemetry rows to purge =
# ``task_type.replace("_"," ").title()`` for every TaskType NOT in
# ``surplus.types.KB_ROUTING_TASK_TYPES`` (action/maintenance/monitor/pipeline-
# intermediate). Snapshotted here as an explicit, auditable list for a DESTRUCTIVE
# migration; test_d0005_purge_surplus_ops_telemetry asserts this equals the live
# enum derivation, so it cannot silently drift from the gate.
_OPS_TELEMETRY_TITLES = frozenset(
    {
        "Backup Verification",
        "Cc Memory Staleness",
        "Code Index",
        "Db Maintenance",
        "Dead Letter Replay",
        "Disk Cleanup",
        "Fresh Session Test",
        "Infrastructure Monitor",
        "J9 Eval Batch",
        "Model Eval",
        "Prompt Review Catalog",
        "Prompt Review Sample",
        "Research Query Gen",
    }
)


def _candidate_ids(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return ``(unit_id, qdrant_id)`` for every surplus ops-telemetry KB unit.

    Scoped to ``source_pipeline='surplus' AND domain='intelligence.surplus'``,
    then filtered in Python to units whose body first line is an exact ops-title
    match — the deterministic signature that separates telemetry from the
    real-titled insights sharing the same pipeline/domain.
    """
    rows = db.execute(
        "SELECT id, qdrant_id, body FROM knowledge_units "
        "WHERE source_pipeline = 'surplus' AND domain = 'intelligence.surplus'"
    ).fetchall()
    out: list[tuple[str, str]] = []
    for unit_id, qdrant_id, body in rows:
        first_line = (body or "").split("\n", 1)[0].strip()
        if first_line in _OPS_TELEMETRY_TITLES:
            out.append((unit_id, qdrant_id or ""))
    return out


def migrate() -> dict:
    """Purge surplus ops-telemetry KB units across all stores. Return counts."""
    # Read candidates first (read-only conn) so a no-op run never touches Qdrant.
    ro = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        targets = _candidate_ids(ro)
    finally:
        ro.close()
    if not targets:
        return {"purged": 0, "qdrant_deleted": 0}

    # Raises if Qdrant is unreachable → migration stays pending, retries next boot.
    client = get_client()

    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    purged = 0
    qdrant_deleted = 0
    qdrant_failed = 0
    try:
        for unit_id, qdrant_id in targets:
            if qdrant_id:
                try:
                    delete_point(client, collection="knowledge_base", point_id=qdrant_id)
                    qdrant_deleted += 1
                except Exception:
                    # Leave this unit's SQLite rows intact so verify() still sees
                    # it and the migration retries — never a half-deleted orphan.
                    logger.warning(
                        "d0005: Qdrant delete failed for %s — skipping SQLite purge",
                        qdrant_id,
                        exc_info=True,
                    )
                    qdrant_failed += 1
                    continue
            # Full MemoryStore.delete() cascade, in raw SQL.
            db.execute("DELETE FROM knowledge_fts WHERE unit_id = ?", (unit_id,))
            db.execute("DELETE FROM knowledge_units WHERE id = ?", (unit_id,))
            if qdrant_id:
                db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (qdrant_id,))
                db.execute("DELETE FROM memory_metadata WHERE memory_id = ?", (qdrant_id,))
                db.execute(
                    "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
                    (qdrant_id, qdrant_id),
                )
                db.execute("DELETE FROM pending_embeddings WHERE memory_id = ?", (qdrant_id,))
                db.execute("DELETE FROM entity_mentions WHERE memory_id = ?", (qdrant_id,))
            purged += 1
        db.commit()
    finally:
        db.close()

    logger.info(
        "d0005: purged %d surplus ops-telemetry KB units (%d Qdrant points, "
        "%d Qdrant-delete failures left for retry)",
        purged,
        qdrant_deleted,
        qdrant_failed,
    )
    return {"purged": purged, "qdrant_deleted": qdrant_deleted, "qdrant_failed": qdrant_failed}


def verify() -> bool:
    """Complete only when NO surplus ops-telemetry knowledge_unit remains.

    A unit whose Qdrant delete failed keeps its SQLite rows, so it stays a
    candidate here → verify() returns False → the migration retries next boot.
    """
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        return not _candidate_ids(db)
    finally:
        db.close()
