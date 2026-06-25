"""Tests for the cognitive self-modification ledger (capture + rollback).

Uses create_all_tables so both cognitive_file_modifications and observations
exist (rollback emits an observation). Targets are temp files, not real cognitive
config.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

import genesis.learning.cognitive_ledger as cl
from genesis.db.crud import cognitive_file_modifications as cfm
from genesis.learning.cognitive_ledger import (
    record_existing,
    record_file_modification,
    rollback,
)


@pytest.fixture
async def db(tmp_path):
    """Full-schema DB (table + observations)."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        from genesis.db.schema import create_all_tables

        await create_all_tables(conn)
        await conn.commit()
        yield conn


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
class TestCapture:
    @pytest.mark.asyncio
    async def test_record_file_modification_new_file(self, db, tmp_path):
        p = tmp_path / "SKILL.md"  # absent
        mid = await record_file_modification(
            db, actor="skill_evolution", path=p, new_content="hello",
        )
        assert mid is not None
        assert p.read_text() == "hello"
        row = await cfm.get(db, mid)
        assert row["prior_content"] is None
        assert row["applied_content"] == "hello"

    @pytest.mark.asyncio
    async def test_record_file_modification_existing_captures_prior(self, db, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text("v1")
        mid = await record_file_modification(db, actor="a", path=p, new_content="v2")
        assert p.read_text() == "v2"
        row = await cfm.get(db, mid)
        assert row["prior_content"] == "v1"

    @pytest.mark.asyncio
    async def test_record_existing_records_a_done_write(self, db, tmp_path):
        p = tmp_path / "USER_KNOWLEDGE.md"
        p.write_text("synth")  # caller already wrote it
        mid = await record_existing(
            db, actor="user_model_evolution", path=p,
            prior_content="old", applied_content="synth",
        )
        row = await cfm.get(db, mid)
        assert row["prior_content"] == "old"
        assert row["applied_content"] == "synth"

    @pytest.mark.asyncio
    async def test_best_effort_write_survives_db_failure(self, tmp_path):
        bad_db = AsyncMock()
        bad_db.execute.side_effect = RuntimeError("db down")
        p = tmp_path / "f.md"
        mid = await record_file_modification(
            bad_db, actor="a", path=p, new_content="written",
        )
        assert mid is None  # ledger insert failed
        assert p.read_text() == "written"  # but the cognitive write still happened

    @pytest.mark.asyncio
    async def test_prune_on_write_caps_history(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(cl, "_KEEP_PER_TARGET", 2)
        p = tmp_path / "f.md"
        for i in range(4):
            await record_file_modification(db, actor="a", path=p, new_content=f"v{i}")
        rows = await cfm.recent(db, limit=10)
        assert len([r for r in rows if r["target_path"] == str(p)]) == 2

    @pytest.mark.asyncio
    async def test_atomic_write_cleans_temp_and_preserves_target_on_failure(
        self, db, tmp_path, monkeypatch,
    ):
        p = tmp_path / "f.md"
        p.write_text("orig")

        def _boom(self, _target):
            raise OSError("disk full")

        monkeypatch.setattr(cl.Path, "replace", _boom)
        with pytest.raises(OSError):
            await record_file_modification(db, actor="a", path=p, new_content="new")
        # No orphaned temp; the original file is untouched (rename never completed).
        assert not list(tmp_path.glob("*.cogtmp"))
        assert p.read_text() == "orig"


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
class TestRollback:
    @pytest.mark.asyncio
    async def test_restores_prior(self, db, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("v1")
        mid = await record_file_modification(db, actor="a", path=p, new_content="v2")
        res = await rollback(db, mid)
        assert res["ok"] is True
        assert res["restored_to"] == "prior"
        assert p.read_text() == "v1"
        assert (await cfm.get(db, mid))["status"] == "rolled_back"

    @pytest.mark.asyncio
    async def test_deletes_file_when_prior_absent(self, db, tmp_path):
        p = tmp_path / "new.md"  # absent before the mod
        mid = await record_file_modification(db, actor="a", path=p, new_content="created")
        assert p.exists()
        res = await rollback(db, mid)
        assert res["ok"] is True
        assert res["restored_to"] == "absent"
        assert not p.exists()

    @pytest.mark.asyncio
    async def test_drift_refused_then_forced(self, db, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("v1")
        mid = await record_file_modification(db, actor="a", path=p, new_content="v2")
        p.write_text("v3-external")  # someone/another job changed it since

        refused = await rollback(db, mid)
        assert refused["ok"] is False and refused["refused"] is True
        assert p.read_text() == "v3-external"  # untouched

        forced = await rollback(db, mid, force=True)
        assert forced["ok"] is True
        assert p.read_text() == "v1"

    @pytest.mark.asyncio
    async def test_already_rolled_back(self, db, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("v1")
        mid = await record_file_modification(db, actor="a", path=p, new_content="v2")
        await rollback(db, mid)
        again = await rollback(db, mid)
        assert again["ok"] is False
        assert "already" in again["reason"]

    @pytest.mark.asyncio
    async def test_missing_id(self, db):
        res = await rollback(db, "nope")
        assert res["ok"] is False
        assert "no such" in res["reason"]

    @pytest.mark.asyncio
    async def test_emits_observation_on_success(self, db, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("v1")
        mid = await record_file_modification(db, actor="a", path=p, new_content="v2")
        await rollback(db, mid)
        cur = await db.execute(
            "SELECT content FROM observations WHERE source='cognitive_ledger'"
        )
        rows = await cur.fetchall()
        assert any("rolled_back" in r[0] for r in rows)

    @pytest.mark.asyncio
    async def test_emits_observation_on_drift_refused(self, db, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("v1")
        mid = await record_file_modification(db, actor="a", path=p, new_content="v2")
        p.write_text("drift")
        await rollback(db, mid)
        cur = await db.execute(
            "SELECT content FROM observations WHERE source='cognitive_ledger'"
        )
        rows = await cur.fetchall()
        assert any("refused" in r[0] for r in rows)
