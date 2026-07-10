"""Migration 0052 — drop the stale schedule_code_audit job_health row.

Verifies the row is deleted on the existing-DB upgrade path, sibling rows
survive, the delete is idempotent, and a missing table is a safe no-op
(the runner's lifecycle tests apply migrations to a bare DB). Mirrors 0049.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M52 = importlib.import_module(
    "genesis.db.migrations.0052_drop_stale_code_audit_job_health"
)

_DDL = """
    CREATE TABLE job_health (
        job_name TEXT PRIMARY KEY, last_run TEXT, last_success TEXT,
        last_failure TEXT, last_error TEXT,
        consecutive_failures INTEGER DEFAULT 0, total_runs INTEGER DEFAULT 0,
        total_successes INTEGER DEFAULT 0, total_failures INTEGER DEFAULT 0,
        updated_at TEXT
    )
"""


async def _names(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("SELECT job_name FROM job_health")
    return {row[0] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_deletes_stale_row_keeps_siblings(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_DDL)
        await db.execute(
            "INSERT INTO job_health (job_name) VALUES "
            "('schedule_code_audit'), ('surplus_dispatch')"
        )
        await M52.up(db)
        assert await _names(db) == {"surplus_dispatch"}


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_DDL)
        await M52.up(db)
        await M52.up(db)  # second run must not raise
        assert await _names(db) == set()


@pytest.mark.asyncio
async def test_up_noop_when_table_absent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M52.up(db)  # no job_health table — safe no-op
