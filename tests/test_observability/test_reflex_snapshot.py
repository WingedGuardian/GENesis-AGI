"""Tests for the reflex health-snapshot section.

The section must degrade to DB-only facts when no runtime/ingestor exists
(MCP child process, early bootstrap) and to an empty-but-shaped dict when
even the DB is absent — it can never poison the gathered snapshot.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest
from genesis.observability.snapshots.reflex import reflex

from genesis.db.crud import reflex_signals as crud

M70 = importlib.import_module("genesis.db.migrations.0070_reflex_arc")

T0 = "2026-07-21T00:00:00+00:00"


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        await M70.up(conn)
        await conn.commit()
        yield conn


async def _seed(db):
    await crud.upsert_occurrence(
        db,
        fingerprint="fp00000000000001",
        class_key="KeyErrorxmemory",
        task_name="mem-sync",
        subsystem="memory",
        error_type="KeyError",
        error_message="KeyError: 'x'",
        traceback_tail="memory/sync.py:_apply",
        now=T0,
    )


class TestReflexSnapshot:
    async def test_db_only_when_no_runtime(self, db):
        await _seed(db)
        with patch("genesis.runtime.GenesisRuntime.peek", return_value=None):
            snap = await reflex(db)
        assert snap["ingestor"] is None
        assert snap["counts"] == {"new": 1}
        assert snap["total_signals"] == 1
        assert snap["top_classes"][0]["class_key"] == "KeyErrorxmemory"

    async def test_ingestor_stats_included_when_present(self, db):
        await _seed(db)
        rt = MagicMock()
        rt._reflex_ingestor.stats = {
            "enabled": True,
            "queued": 0,
            "processed": 3,
            "dropped": 0,
        }
        with patch("genesis.runtime.GenesisRuntime.peek", return_value=rt):
            snap = await reflex(db)
        assert snap["ingestor"] == {
            "enabled": True,
            "queued": 0,
            "processed": 3,
            "dropped": 0,
        }
        assert snap["counts"] == {"new": 1}

    async def test_runtime_without_ingestor_degrades(self, db):
        rt = MagicMock()
        rt._reflex_ingestor = None
        with patch("genesis.runtime.GenesisRuntime.peek", return_value=rt):
            snap = await reflex(db)
        assert snap["ingestor"] is None
        assert snap["counts"] == {}
        assert snap["total_signals"] == 0

    async def test_no_db_returns_shaped_empty(self):
        snap = await reflex(None)
        assert snap == {
            "ingestor": None,
            "counts": {},
            "top_classes": [],
            "total_signals": 0,
        }

    async def test_db_error_degrades_not_raises(self):
        bad_db = MagicMock()
        bad_db.execute.side_effect = RuntimeError("boom")
        with patch("genesis.runtime.GenesisRuntime.peek", return_value=None):
            snap = await reflex(bad_db)
        assert snap["counts"] == {}
        assert snap["total_signals"] == 0
