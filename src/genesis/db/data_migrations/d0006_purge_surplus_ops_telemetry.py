"""d0006 — purge operational-telemetry rows that polluted the knowledge base.

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
``domain='intelligence.surplus'`` AND whose body is ``"<title>\n\n<prefix>…"`` for
an entry in ``_OPS_SIGNATURES`` — the exact non-KB-routing ``single_item`` title
AND the deterministic machine-report opener the executor writes (e.g.
"Db Maintenance" + "Database maintenance report:"). The body-prefix guard is what
makes it safe against a legitimate insight an LLM happened to title "Model Eval"
in the same scope (Codex P2, #1179) — an insight's prose body lacks the machine
prefix. Insight-producing tasks are never matched (their titles are disjoint from
the signatures — asserted by test_d0006_purge_surplus_ops_telemetry).

Cross-store: a KB unit lives in Qdrant + memory_metadata + memory_fts +
knowledge_units + knowledge_fts (+ memory_links / pending_embeddings /
entity_mentions cascades) — the same fan-out ``MemoryStore.delete()`` performs.
Deleting only the Qdrant point + knowledge_units would leave the junk
RESURFACING via degraded FTS5 recall (``memory_fts`` is cross-collection), so we
reproduce the full cascade in raw SQL (``migrate()``/``verify()`` are SYNC with
their own connections — they cannot call the async store).

Ordering: Qdrant points are deleted (phase 1) BEFORE any SQLite rows (phase 2),
never while holding the SQLite write lock. If a unit's Qdrant delete fails
(transient), that unit is dropped from phase 2 so its SQLite rows are LEFT intact
— ``verify()`` still sees it as a candidate → the migration is marked failed and
retries next boot (no half-deleted orphan vector). ``get_client()`` failing
entirely raises → same retry. Idempotent: a missing point deletes as a no-op, and
the SQLite DELETEs are no-ops once purged / on a fresh install (no such rows) —
so the intermediate "point gone, rows still present" window is simply re-run.

migrate()/verify() are SYNC (framework contract, cf. d0003/d0004). Own
connections only — never the runtime's async ``rt._db``.
"""

from __future__ import annotations

import logging
import sqlite3

from genesis.db.data_migrations._util import commit_in_batches
from genesis.env import genesis_db_path
from genesis.qdrant.collections import delete_point, get_client

logger = logging.getLogger(__name__)

requires_operator = False

# Purge signatures: ``{first-line title -> required body prefix}``. A row is
# telemetry only if its body is ``"<title>\n\n<prefix>…"`` — i.e. the exact
# single_item title AND the deterministic machine-report opener the executor
# writes. Every title here is a non-KB-routing task's ``task_type.title()``
# (asserted by test) so it can never name an insight-producing task.
#
# The body-prefix guard exists because title-alone is unsafe for GENERIC
# operational names: an insight-producing task could produce a finding an LLM
# titled "Model Eval" or "Db Maintenance" in the same surplus/intelligence.surplus
# scope, and a title-only match would false-delete it (Codex P2, PR #1179). The
# machine-report prefix an LLM insight would never reproduce makes the match safe.
# Pipeline-intermediate titles (RESEARCH_QUERY_GEN / PROMPT_REVIEW_*) are SPECIFIC
# internal step-names no insight would ever carry, and their bodies are free-form
# LLM prose with no stable prefix, so they use ``""`` (title-only — still safe).
_OPS_SIGNATURES: dict[str, str] = {
    "Backup Verification": "Backup verification:",
    "Cc Memory Staleness": "CC Memory Staleness Scan:",
    "Db Maintenance": "Database maintenance report:",
    "Disk Cleanup": "Disk cleanup scan",
    "Fresh Session Test": "Fresh Session Test completed",
    "J9 Eval Batch": "J9 eval batch:",
    "Model Eval": "Model evaluation:",
    "Research Query Gen": "",  # specific pipeline-step name — title-only is safe
    "Prompt Review Sample": "",  # specific pipeline-step name — title-only is safe
    "Prompt Review Catalog": "",  # specific pipeline-step name — title-only is safe
}


def _candidate_ids(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return ``(unit_id, qdrant_id)`` for every surplus ops-telemetry KB unit.

    Scoped to ``source_pipeline='surplus' AND domain='intelligence.surplus'``,
    then filtered in Python to units whose body is ``"<title>\\n\\n<prefix>…"`` for
    an entry in ``_OPS_SIGNATURES`` — the deterministic title+machine-report
    signature that separates telemetry from real insights in the same scope.
    """
    rows = db.execute(
        "SELECT id, qdrant_id, body FROM knowledge_units "
        "WHERE source_pipeline = 'surplus' AND domain = 'intelligence.surplus'"
    ).fetchall()
    out: list[tuple[str, str]] = []
    for unit_id, qdrant_id, body in rows:
        b = body or ""
        for title, prefix in _OPS_SIGNATURES.items():
            if b.startswith(f"{title}\n\n{prefix}"):
                out.append((unit_id, qdrant_id or ""))
                break
    return out


def migrate() -> dict:
    """Purge surplus ops-telemetry KB units across all stores. Return counts.

    Two phases so the WAL write lock is never held during Qdrant network I/O
    (holding it there is what let the original single-transaction form starve the
    live server for ~13s, #1179):

    Phase 1 (lock-free — no write connection open): delete each unit's Qdrant
    point. A unit whose point-delete FAILS is dropped from phase 2, so its SQLite
    rows stay put, verify() still sees it, and the migration retries — never a
    half-deleted orphan vector.

    Phase 2 (``commit_in_batches``): the fast raw-SQL cross-store cascade, holding
    the write lock only for a bounded batch of quick DELETEs at a time.
    """
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

    # Phase 1 — Qdrant deletes, no SQLite write lock held. Only units whose point
    # is gone (or that never had one) advance to the SQLite cascade.
    deletable: list[tuple[str, str]] = []
    qdrant_deleted = 0
    qdrant_failed = 0
    for unit_id, qdrant_id in targets:
        if qdrant_id:
            try:
                delete_point(client, collection="knowledge_base", point_id=qdrant_id)
                qdrant_deleted += 1
            except Exception:
                logger.warning(
                    "d0006: Qdrant delete failed for %s — leaving SQLite rows for retry",
                    qdrant_id,
                    exc_info=True,
                )
                qdrant_failed += 1
                continue
        deletable.append((unit_id, qdrant_id))

    # Phase 2 — the full MemoryStore.delete() cascade in raw SQL, batched so the
    # write lock is released between batches (see _util.commit_in_batches).
    def _purge_sqlite(conn: sqlite3.Connection, target: tuple[str, str]) -> None:
        unit_id, qdrant_id = target
        conn.execute("DELETE FROM knowledge_fts WHERE unit_id = ?", (unit_id,))
        conn.execute("DELETE FROM knowledge_units WHERE id = ?", (unit_id,))
        if qdrant_id:
            conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (qdrant_id,))
            conn.execute("DELETE FROM memory_metadata WHERE memory_id = ?", (qdrant_id,))
            conn.execute(
                "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
                (qdrant_id, qdrant_id),
            )
            conn.execute("DELETE FROM pending_embeddings WHERE memory_id = ?", (qdrant_id,))
            conn.execute("DELETE FROM entity_mentions WHERE memory_id = ?", (qdrant_id,))

    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:
        purged = commit_in_batches(db, deletable, _purge_sqlite)
    finally:
        db.close()

    logger.info(
        "d0006: purged %d surplus ops-telemetry KB units (%d Qdrant points, "
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
