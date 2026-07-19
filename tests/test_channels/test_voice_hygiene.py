"""Tests for daily voice hygiene: transcript aging + stale-producer blob sweep."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.channels.voice.hygiene import (
    prune_old_transcripts,
    sweep_blob_memories,
)
from genesis.db.crud import cc_sessions as sessions_crud

pytestmark = pytest.mark.asyncio

_BLOB_TAGS = "voice s2s conversation class:fact wing:channels"


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


# ── transcript prune ─────────────────────────────────────────────────


class TestPruneOldTranscripts:
    async def _seed(self, db, tmp_path, name: str, *, started_days_ago: int, status: str):
        await sessions_crud.register_voice_session(
            db,
            id=name,
            started_at=_iso_days_ago(started_days_ago),
        )
        if status != "active":
            await sessions_crud.update_status(db, name, status=status)
        path = tmp_path / f"{name}.jsonl"
        path.write_text('{"type":"user","message":{"content":"hi"}}\n')
        return path

    async def test_prunes_only_old_completed(self, db, tmp_path):
        old = await self._seed(db, tmp_path, "old-done", started_days_ago=400, status="completed")
        recent = await self._seed(db, tmp_path, "recent", started_days_ago=30, status="completed")
        removed = await prune_old_transcripts(db, transcript_dir=tmp_path)
        assert removed == 1
        assert not old.exists()
        assert recent.exists()

    async def test_never_prunes_active_session(self, db, tmp_path):
        path = await self._seed(db, tmp_path, "old-active", started_days_ago=400, status="active")
        assert await prune_old_transcripts(db, transcript_dir=tmp_path) == 0
        assert path.exists()

    async def test_rowless_file_falls_back_to_mtime(self, db, tmp_path):
        old = tmp_path / "rowless-old.jsonl"
        old.write_text("{}\n")
        stamp = (datetime.now(UTC) - timedelta(days=400)).timestamp()
        os.utime(old, (stamp, stamp))
        fresh = tmp_path / "rowless-fresh.jsonl"
        fresh.write_text("{}\n")
        assert await prune_old_transcripts(db, transcript_dir=tmp_path) == 1
        assert not old.exists()
        assert fresh.exists()

    async def test_missing_dir_noops(self, db, tmp_path):
        assert await prune_old_transcripts(db, transcript_dir=tmp_path / "nope") == 0


# ── blob sweep ───────────────────────────────────────────────────────


class TestSweepBlobMemories:
    async def _seed_fts(self, db, memory_id: str, content: str, tags: str):
        await db.execute(
            "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, 'memory', ?, 'episodic_memory')",
            (memory_id, content, tags),
        )
        await db.commit()

    def _store(self) -> MagicMock:
        store = MagicMock()
        store.delete = AsyncMock(return_value={"fts5": True})
        return store

    async def test_sweeps_exactly_the_blob_cohort(self, db):
        # The cohort: voice-tagged AND the legacy blob content prefix
        await self._seed_fts(
            db,
            "blob-1",
            "Voice conversation [s2s-default]:\nUser: hi",
            _BLOB_TAGS,
        )
        await self._seed_fts(
            db,
            "blob-2",
            "Voice conversation [pe]:\nUser: yo\nGenesis: hey",
            _BLOB_TAGS,
        )
        # Voice-tagged but NOT the blob signature — must survive
        await self._seed_fts(
            db,
            "legit-voice",
            "User asked about ice cream via voice",
            _BLOB_TAGS,
        )
        # Unrelated memory — must survive
        await self._seed_fts(
            db,
            "unrelated",
            "Voice conversation preferences discussion",
            "notes wing:memory",
        )
        # Dangling SVO event for a blob (store.delete does not cover this table)
        await db.execute(
            "INSERT INTO memory_events (id, memory_id, subject, verb) "
            "VALUES ('ev1', 'blob-1', 's', 'v')",
        )
        await db.commit()

        store = self._store()
        assert await sweep_blob_memories(db, store) == 2
        deleted = {c.args[0] for c in store.delete.await_args_list}
        assert deleted == {"blob-1", "blob-2"}

        cur = await db.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id='blob-1'")
        assert (await cur.fetchone())[0] == 0

    async def test_no_candidates_noops(self, db):
        await self._seed_fts(db, "other", "unrelated content", "notes wing:memory")
        store = self._store()
        assert await sweep_blob_memories(db, store) == 0
        store.delete.assert_not_awaited()

    async def test_sweep_logs_loudly(self, db, caplog):
        await self._seed_fts(
            db,
            "blob-1",
            "Voice conversation [s2s-default]:\nUser: hi",
            _BLOB_TAGS,
        )
        with caplog.at_level("WARNING"):
            await sweep_blob_memories(db, self._store())
        assert any("stale producer" in r.message for r in caplog.records)
