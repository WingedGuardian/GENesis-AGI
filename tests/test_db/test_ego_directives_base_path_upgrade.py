"""Regression: ``create_all_tables`` upgrades a legacy ``ego_directives``.

Root cause (2026-07-18): PR #1123 added the decision columns
(``kind``/``source_proposal_id``/``reaffirm_count``/``last_reaffirmed_at``) to
the canonical CREATE TABLE *and* the index ``idx_ego_directives_kind_status``
to ``INDEXES``, but the existing-DB column-add lived only in the numbered
migration ``0066`` — not in ``_migrate_add_columns``. ``create_all_tables``
runs ``_migrate_add_columns`` and *then* builds ``INDEXES``; on a DB whose
``ego_directives`` predates the decision columns the ``CREATE TABLE IF NOT
EXISTS`` is a no-op, so the index build hit ``no such column: kind`` and
bootstrap crashed. Fresh-DB CI never exercised this path (the canonical DDL
already carries the columns), so it went green.

This guards the *class* of bug — any migration-added column that an index in
``INDEXES`` references must also be added on the base ``create_all_tables``
path, not only in a numbered migration.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.schema._migrations import create_all_tables

# ego_directives exactly as it existed BEFORE the decision columns (PR #1123).
_LEGACY_DDL = """
    CREATE TABLE ego_directives (
        id          TEXT PRIMARY KEY,
        content     TEXT NOT NULL,
        priority    TEXT NOT NULL DEFAULT 'normal'
            CHECK (priority IN ('low', 'normal', 'high', 'critical')),
        source      TEXT NOT NULL DEFAULT 'user',
        ego_target  TEXT NOT NULL DEFAULT 'user_ego',
        status      TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'completed', 'cancelled')),
        created_at  TEXT NOT NULL,
        resolved_at TEXT,
        resolution  TEXT
    )
"""


async def _columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(ego_directives)")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_create_all_tables_upgrades_legacy_ego_directives(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as db:
        await db.execute(_LEGACY_DDL)
        # A pre-existing directive row, to prove the upgrade preserves data.
        await db.execute(
            "INSERT INTO ego_directives (id, content, created_at) "
            "VALUES ('d-old', 'legacy directive', '2026-01-01T00:00:00')"
        )
        assert "kind" not in await _columns(db)

        # This is the exact call that crashed at boot before the fix.
        await create_all_tables(db)

        cols = await _columns(db)
        assert {"kind", "source_proposal_id", "reaffirm_count", "last_reaffirmed_at"} <= cols

        # The index that referenced the missing column now builds cleanly.
        cur = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_ego_directives_kind_status'"
        )
        assert await cur.fetchone() is not None

        # Existing rows keep their data and get the 'directive' default.
        cur = await db.execute("SELECT kind FROM ego_directives WHERE id='d-old'")
        assert (await cur.fetchone())[0] == "directive"


@pytest.mark.asyncio
async def test_create_all_tables_is_idempotent_on_legacy(tmp_path):
    """A second create_all_tables pass must not raise (duplicate column)."""
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as db:
        await db.execute(_LEGACY_DDL)
        await create_all_tables(db)
        await create_all_tables(db)  # must be a no-op, not "duplicate column"
        assert "kind" in await _columns(db)
