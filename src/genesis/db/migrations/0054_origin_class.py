"""Add ``origin_class`` (WS-3 provenance taxonomy) + deterministic backfill.

``origin_class ∈ {owner, first_party, external_untrusted}`` is stamped at
store time (see ``genesis.memory.provenance.derive_origin_class``); future
immunity gates key on ``external_untrusted`` vs not. Column is NULLable —
NULL means "written before this migration by a path the backfill couldn't
classify"; gates normalize NULL fail-closed at GATE time, so nullable is
safe and the ADD COLUMN stays O(1) metadata-only (a NOT NULL DEFAULT would
rewrite ~54K rows twice).

Backfill is deterministic from columns that already exist, and deliberately
emits only first_party/external_untrusted — the owner signal is weak in
historical rows and is NEVER heuristically backfilled (binding WS-3
decision):

- knowledge_units: Genesis-authored pipelines (surplus, reference_store,
  extraction_job) → first_party; every other KB unit (curated uploads/URLs,
  knowledge_ingest*, recon, NULL-pipeline legacy) is world-derived text →
  external_untrusted.
- memory_metadata outside the knowledge_base collection → first_party
  (episodic/self content; matches provenance.is_external's stance).
- memory_metadata IN knowledge_base → the joined knowledge_units row's
  class via qdrant_id, else external_untrusted (conservative for KB).

All UPDATEs are guarded ``WHERE origin_class IS NULL`` → idempotent.
Fresh/test DBs get the column from the canonical CREATE TABLE in
``db/schema/_tables.py``; this migration covers the existing-DB path.
No commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite

_FIRST_PARTY_KU_PIPELINES = ("surplus", "reference_store", "extraction_job")


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cursor.fetchone() is not None


async def _ensure_column(db: aiosqlite.Connection, table: str) -> None:
    col_cursor = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608 - fixed table names from this module only
    cols = {row[1] for row in await col_cursor.fetchall()}
    if "origin_class" not in cols:
        await db.execute(
            f"ALTER TABLE {table} ADD COLUMN origin_class TEXT"  # noqa: S608 - fixed table names from this module only
        )


async def up(db: aiosqlite.Connection) -> None:
    has_ku = await _has_table(db, "knowledge_units")
    has_mm = await _has_table(db, "memory_metadata")

    if has_ku:
        await _ensure_column(db, "knowledge_units")
        placeholders = ",".join("?" * len(_FIRST_PARTY_KU_PIPELINES))
        await db.execute(
            "UPDATE knowledge_units SET origin_class = CASE "  # noqa: S608 - literal SQL; the IN placeholders are bound params
            f"  WHEN source_pipeline IN ({placeholders}) THEN 'first_party' "
            "  ELSE 'external_untrusted' END "
            "WHERE origin_class IS NULL",
            _FIRST_PARTY_KU_PIPELINES,
        )

    if has_mm:
        await _ensure_column(db, "memory_metadata")
        await db.execute(
            "UPDATE memory_metadata SET origin_class = 'first_party' "
            "WHERE origin_class IS NULL AND collection != 'knowledge_base'"
        )
        if has_ku:
            await db.execute(
                "UPDATE memory_metadata SET origin_class = COALESCE("
                "  (SELECT ku.origin_class FROM knowledge_units ku"
                "   WHERE ku.qdrant_id = memory_metadata.memory_id),"
                "  'external_untrusted') "
                "WHERE origin_class IS NULL AND collection = 'knowledge_base'"
            )
        else:
            await db.execute(
                "UPDATE memory_metadata SET origin_class = 'external_untrusted' "
                "WHERE origin_class IS NULL AND collection = 'knowledge_base'"
            )
