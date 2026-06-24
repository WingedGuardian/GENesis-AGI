"""Tests for SessionStart procedure injection tier gating (Surfacing v2 PR-A).

`load_active_procedures` blindly injects procedures at session start, before
the session topic is known. v2 narrows this to the CORE tier (L1) only — the
most-proven always-on procedures — so session start stops injecting up to ~5
mid-tier procedures of uncertain relevance.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.learning.procedural.session_inject import load_active_procedures

_COLS = (
    "id, task_type, principle, steps, tools_used, context_tags, "
    "confidence, created_at, activation_tier, deprecated, quarantined"
)


async def _seed(db_path, rows: list[dict]) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await create_all_tables(conn)
        for r in rows:
            await conn.execute(
                f"INSERT INTO procedural_memory ({_COLS}) "
                "VALUES (?, ?, ?, '[]', '[]', '[]', ?, ?, ?, ?, ?)",
                (
                    r["id"], r["task_type"], r["principle"],
                    r.get("confidence", 0.8), "2026-01-01T00:00:00",
                    r["activation_tier"], r.get("deprecated", 0),
                    r.get("quarantined", 0),
                ),
            )
        await conn.commit()


@pytest.mark.asyncio
async def test_only_core_tier_injected(tmp_path):
    """Only L1 (CORE) procedures are injected; L2/L3/L4 are excluded."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "c", "task_type": "core_proc", "principle": "core", "activation_tier": "L1"},
        {"id": "a", "task_type": "adv_proc", "principle": "adv", "activation_tier": "L2"},
        {"id": "l", "task_type": "lib_proc", "principle": "lib", "activation_tier": "L3"},
        {"id": "d", "task_type": "dorm_proc", "principle": "dorm", "activation_tier": "L4"},
    ])
    out = await load_active_procedures(db)
    assert out is not None
    assert "core_proc" in out
    assert "adv_proc" not in out
    assert "lib_proc" not in out
    assert "dorm_proc" not in out


@pytest.mark.asyncio
async def test_returns_none_when_no_core(tmp_path):
    """With only mid/low-tier procedures, nothing is injected at session start."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "a", "task_type": "adv_proc", "principle": "p", "activation_tier": "L2"},
        {"id": "l", "task_type": "lib_proc", "principle": "p", "activation_tier": "L3"},
    ])
    assert await load_active_procedures(db) is None


@pytest.mark.asyncio
async def test_excludes_deprecated_and_quarantined_core(tmp_path):
    """A deprecated or quarantined CORE procedure is never injected."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "dep", "task_type": "dep_core", "principle": "p", "activation_tier": "L1", "deprecated": 1},
        {"id": "q", "task_type": "q_core", "principle": "p", "activation_tier": "L1", "quarantined": 1},
    ])
    assert await load_active_procedures(db) is None
