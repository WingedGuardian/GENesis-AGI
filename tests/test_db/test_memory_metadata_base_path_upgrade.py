"""Regression: ``create_all_tables`` upgrades a legacy ``memory_metadata``.

Root cause (MW-1, PR #1325 — Codex P1): the five judgment columns
(``speech_act``/``speech_act_confidence``/``assertion_provenance``/
``durability``/``expires_at``) were added to the canonical CREATE TABLE *and*
the numbered migration ``0079`` — but NOT to ``_migrate_add_columns``.
``create_all_tables`` runs ``_migrate_add_columns`` and NOT the numbered
runner, so on a DB whose ``memory_metadata`` predates the columns the
``CREATE TABLE IF NOT EXISTS`` is a no-op and the columns never appear.

Unlike the #1123 ``ego_directives`` case, these columns are UNINDEXED, so the
static ``INDEXES``-parity guard (``test_schema_base_path_parity``) correctly
stays green — this is a *different* failure mode: any ``create_all_tables``
upgrade path that then stores memories (e.g. ``scripts/migrate_faiss_to_qdrant``
→ ``create_all_tables`` → ``MemoryStore.store`` → ``create_metadata`` INSERT)
raises ``table memory_metadata has no column named speech_act`` — AFTER the
Qdrant + FTS writes already committed, leaving a cross-store partial record.

This guards the class the static guard cannot: a migration-added column that
create_all_tables callers INSERT into must be mirrored on the base path too.
``schema_both_build_paths``.

The legacy table is constructed by building the current schema and DROPping the
five columns — a faithful, low-maintenance stand-in for a pre-MW-1 DB (the real
table is too large to hand-freeze the way ``ego_directives`` was).
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.schema._migrations import create_all_tables

_JUDGMENT_COLS = {
    "speech_act",
    "speech_act_confidence",
    "assertion_provenance",
    "durability",
    "expires_at",
}


async def _columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(memory_metadata)")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_create_all_tables_upgrades_legacy_memory_metadata(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as db:
        # Build the current schema, then strip the MW-1 columns to simulate a
        # pre-0079 existing DB whose table predates them.
        await create_all_tables(db)
        for col in _JUDGMENT_COLS:
            await db.execute(f"ALTER TABLE memory_metadata DROP COLUMN {col}")
        assert not (_JUDGMENT_COLS & await _columns(db)), "setup failed to strip"

        # A pre-existing metadata row, to prove the upgrade preserves data.
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at) "
            "VALUES ('m-old', '2026-01-01T00:00:00')"
        )

        # This is the exact call the FAISS→Qdrant migration makes before storing.
        await create_all_tables(db)

        assert await _columns(db) >= _JUDGMENT_COLS, "base path did not add MW-1 columns"

        # Existing row survives; new judgment columns are NULL (opt-in, no backfill).
        cur = await db.execute(
            "SELECT durability, speech_act FROM memory_metadata WHERE memory_id='m-old'"
        )
        row = await cur.fetchone()
        assert row == (None, None)


@pytest.mark.asyncio
async def test_create_all_tables_is_idempotent_on_legacy(tmp_path):
    """A second create_all_tables pass must not raise (duplicate column)."""
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as db:
        await create_all_tables(db)
        for col in _JUDGMENT_COLS:
            await db.execute(f"ALTER TABLE memory_metadata DROP COLUMN {col}")

        await create_all_tables(db)
        await create_all_tables(db)  # must be a no-op, not "duplicate column"
        assert await _columns(db) >= _JUDGMENT_COLS
