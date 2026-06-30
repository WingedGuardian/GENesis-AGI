"""Migration 0041 — add ``outcome_quality`` to surplus_tasks.

Records the verified-correctness verdict for insight-producing surplus tasks
('useful' / 'hollow' / NULL). The test builds the *pre-migration* schema
explicitly (no outcome_quality column) so it exercises the real
``ALTER TABLE … ADD COLUMN`` regardless of current DDL.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M41 = importlib.import_module(
    "genesis.db.migrations.0041_surplus_outcome_quality"
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _build_pre_state(conn: aiosqlite.Connection) -> None:
    """surplus_tasks as it existed *before* 0041 — no outcome_quality column."""
    await conn.execute(
        """
        CREATE TABLE surplus_tasks (
            id                TEXT PRIMARY KEY,
            task_type         TEXT NOT NULL,
            compute_tier      TEXT NOT NULL,
            priority          REAL NOT NULL DEFAULT 0.5,
            drive_alignment   TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            payload           TEXT,
            created_at        TEXT NOT NULL,
            started_at        TEXT,
            completed_at      TEXT,
            result_staging_id TEXT,
            failure_reason    TEXT,
            attempt_count     INTEGER NOT NULL DEFAULT 0,
            not_before        TEXT
        )
        """
    )
    await conn.commit()


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await _build_pre_state(conn)
        yield conn


@pytest.mark.asyncio
async def test_adds_column(db):
    assert "outcome_quality" not in await _columns(db, "surplus_tasks")
    await M41.up(db)
    assert "outcome_quality" in await _columns(db, "surplus_tasks")


@pytest.mark.asyncio
async def test_preserves_existing_rows_with_null(db):
    await db.execute(
        "INSERT INTO surplus_tasks "
        "(id, task_type, compute_tier, drive_alignment, status, created_at) "
        "VALUES ('t1', 'code_audit', 'local', 'competence', 'completed', "
        "'2026-06-24T00:00:00+00:00')"
    )
    await db.commit()

    await M41.up(db)

    cur = await db.execute(
        "SELECT task_type, outcome_quality FROM surplus_tasks WHERE id='t1'"
    )
    row = await cur.fetchone()
    assert row["task_type"] == "code_audit"
    assert row["outcome_quality"] is None  # new column, no backfill


@pytest.mark.asyncio
async def test_check_constraint_allows_valid_and_null(db):
    await M41.up(db)
    for verdict in ("useful", "hollow", None):
        await db.execute(
            "INSERT INTO surplus_tasks "
            "(id, task_type, compute_tier, drive_alignment, status, created_at, "
            " outcome_quality) "
            "VALUES (?, 'brainstorm_self', 'local', 'curiosity', 'completed', "
            "'2026-06-24T00:00:00+00:00', ?)",
            (f"ok-{verdict}", verdict),
        )
    await db.commit()  # no IntegrityError


@pytest.mark.asyncio
async def test_check_constraint_rejects_invalid(db):
    await M41.up(db)
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO surplus_tasks "
            "(id, task_type, compute_tier, drive_alignment, status, created_at, "
            " outcome_quality) "
            "VALUES ('bad', 'brainstorm_self', 'local', 'curiosity', 'completed', "
            "'2026-06-24T00:00:00+00:00', 'garbage')"
        )


@pytest.mark.asyncio
async def test_idempotent_on_rerun(db):
    await M41.up(db)
    await M41.up(db)  # must not raise "duplicate column name"
    assert "outcome_quality" in await _columns(db, "surplus_tasks")


@pytest.mark.asyncio
async def test_skips_when_base_table_absent(tmp_path):
    """The runner applies migrations against a bare DB; 0041 must skip cleanly
    rather than fail on a missing table."""
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("CREATE TABLE schema_migrations (version TEXT)")
        await conn.commit()
        await M41.up(conn)  # must not raise (no such table: surplus_tasks)
