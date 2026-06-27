"""C-honest: the PreToolUse procedure advisor bumps ``surfaced_count`` on a
match — honest funnel observability, deliberately NOT ``invocation_count`` (so
advisory surfacing can never feed the promoter / promote an unproven draft).

``_record_procedures_surfaced`` resolves the DB path via ``genesis.env`` (proven
to work in hook runtime), so we monkeypatch that to a tmp DB.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables

# Load the advisor script as a module (scripts/ is not a package).
_ADVISOR_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "procedure_advisor.py"
_spec = importlib.util.spec_from_file_location("procedure_advisor", _ADVISOR_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["procedure_advisor"] = _mod
_spec.loader.exec_module(_mod)

_record_procedures_surfaced = _mod._record_procedures_surfaced


async def _seed(db_path, ids):
    async with aiosqlite.connect(str(db_path)) as conn:
        await create_all_tables(conn)
        for pid in ids:
            await conn.execute(
                "INSERT INTO procedural_memory "
                "(id, task_type, principle, steps, tools_used, context_tags, created_at, "
                " activation_tier) "
                "VALUES (?, 't', 'p', '[]', '[]', '[]', '2026-01-01T00:00:00', 'CORE')",
                (pid,),
            )
        await conn.commit()


async def _counts(db_path, pid):
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT surfaced_count, invocation_count FROM procedural_memory WHERE id = ?",
            (pid,),
        )
        return await cur.fetchone()


@pytest.fixture
def _db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    env = importlib.import_module("genesis.env")
    monkeypatch.setattr(env, "genesis_db_path", lambda: db)
    return db


@pytest.mark.asyncio
async def test_advisor_bumps_all_matched_ids(_db):
    await _seed(_db, ["a", "b"])
    _record_procedures_surfaced(["a", "b"])
    assert (await _counts(_db, "a"))[0] == 1
    assert (await _counts(_db, "b"))[0] == 1


@pytest.mark.asyncio
async def test_advisor_does_not_touch_invocation(_db):
    await _seed(_db, ["a"])
    _record_procedures_surfaced(["a"])
    surfaced, invocation = await _counts(_db, "a")
    assert surfaced == 1 and invocation == 0


@pytest.mark.asyncio
async def test_advisor_missing_id_tolerated(_db):
    await _seed(_db, ["a"])
    _record_procedures_surfaced(["a", "ghost"])   # 'ghost' absent — must not raise
    assert (await _counts(_db, "a"))[0] == 1


def test_advisor_empty_list_is_noop(_db):
    # No DB access at all when there are no matches.
    _record_procedures_surfaced([])
