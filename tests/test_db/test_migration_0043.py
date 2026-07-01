"""Migration 0043 — judge_score + judge_detail columns on surplus_tasks."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

_MIG = "genesis.db.migrations.0043_surplus_judge_score"

# Pre-0043 surplus_tasks: has outcome_quality (from 0041) but no judge columns.
_OLD_DDL = """
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
        not_before        TEXT,
        outcome_quality   TEXT CHECK (outcome_quality IN ('useful', 'hollow'))
    )
"""


async def _cols(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(surplus_tasks)")
    return {r[1] for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_adds_judge_columns(tmp_path) -> None:
    db = tmp_path / "m.db"
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(_OLD_DDL)
        await conn.commit()
        mod = importlib.import_module(_MIG)
        await mod.up(conn)
        await conn.commit()
        cols = await _cols(conn)
        assert "judge_score" in cols
        assert "judge_detail" in cols


@pytest.mark.asyncio
async def test_idempotent(tmp_path) -> None:
    """Running up() twice must not raise (duplicate-column) or drop data."""
    db = tmp_path / "m.db"
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(_OLD_DDL)
        await conn.commit()
        mod = importlib.import_module(_MIG)
        await mod.up(conn)
        await mod.up(conn)
        await conn.commit()
        assert {"judge_score", "judge_detail"} <= await _cols(conn)


@pytest.mark.asyncio
async def test_missing_table_is_noop(tmp_path) -> None:
    db = tmp_path / "empty.db"
    async with aiosqlite.connect(str(db)) as conn:
        mod = importlib.import_module(_MIG)
        await mod.up(conn)  # no surplus_tasks table → safe no-op
        await conn.commit()


@pytest.mark.asyncio
async def test_columns_accept_judge_values(tmp_path) -> None:
    db = tmp_path / "m.db"
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(_OLD_DDL)
        await conn.commit()
        mod = importlib.import_module(_MIG)
        await mod.up(conn)
        await conn.execute(
            "INSERT INTO surplus_tasks "
            "(id, task_type, compute_tier, drive_alignment, created_at, status, "
            " outcome_quality, judge_score, judge_detail) "
            "VALUES ('t1','brainstorm_user','free_api','curiosity','2026-01-01',"
            "'completed','hollow', 0.3, '{\"judge_score\": 0.3}')"
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT judge_score, judge_detail FROM surplus_tasks WHERE id='t1'"
        )
        row = await cur.fetchone()
        assert row[0] == 0.3
        assert "0.3" in row[1]
