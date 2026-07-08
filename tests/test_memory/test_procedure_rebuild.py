"""Tests for the procedure-rebuild recovery path (N1 cognition-loss leak fix).

When the whole-session procedure builder dies on provider exhaustion, the
extraction watermark has already advanced past the session's lines, so the
normal cycle never revisits it. These tests cover the durable rebuild queue that
recovers it: enqueue-on-failure, drain-and-complete, retry-while-exhausted,
discard-on-missing-transcript, and attempt-cap exhaustion (with a visible
observation so the loss is never silent).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import genesis.learning.procedural.judge as jm
import genesis.learning.procedural.struggle_detector as sd
from genesis.db.crud import deferred_work as dw_crud
from genesis.db.schema import create_all_tables
from genesis.memory import extraction_job as ej


@pytest.fixture
async def mdb():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _pending(db):
    return await dw_crud.query_pending(db, work_type="procedure_rebuild", limit=50)


def _stub_builder(monkeypatch, *, result):
    """Point the drain's builder imports at fakes. ``result`` is a return value
    (list of ids) or an exception instance to raise."""
    monkeypatch.setattr(ej, "_find_transcript", lambda d, s: Path("/fake/cc1.jsonl"))
    monkeypatch.setattr(sd, "build_spine_and_haystack", lambda p: ([{"turn": 1}], "hay"))
    monkeypatch.setattr(sd, "score_struggle", lambda spine: 0.5)
    if isinstance(result, Exception):
        monkeypatch.setattr(jm, "judge_multi_procedure", AsyncMock(side_effect=result))
    else:
        monkeypatch.setattr(jm, "judge_multi_procedure", AsyncMock(return_value=result))


async def _drain(mdb):
    await ej._drain_procedure_rebuilds(
        db=mdb, router=MagicMock(), transcript_dir=Path("/fake"),
        summary={}, max_procedures_per_session=7,
    )


@pytest.mark.asyncio
async def test_enqueue_creates_pending_item(mdb):
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    items = await _pending(mdb)
    assert len(items) == 1
    assert json.loads(items[0]["payload_json"])["cc_session_id"] == "cc1"


@pytest.mark.asyncio
async def test_drain_success_completes_and_counts(mdb, monkeypatch):
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    _stub_builder(monkeypatch, result=["proc-1", "proc-2"])
    summary: dict = {}
    await ej._drain_procedure_rebuilds(
        db=mdb, router=MagicMock(), transcript_dir=Path("/fake"),
        summary=summary, max_procedures_per_session=7,
    )
    assert summary["procedures_rebuilt"] == 2
    assert await _pending(mdb) == []  # completed → no longer pending


@pytest.mark.asyncio
async def test_drain_completes_even_with_zero_procedures(mdb, monkeypatch):
    """A successful rebuild that finds nothing still COMPLETES (provider was up,
    nothing to recover) — it must not linger and retry forever."""
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    _stub_builder(monkeypatch, result=[])
    await _drain(mdb)
    assert await _pending(mdb) == []


@pytest.mark.asyncio
async def test_drain_still_exhausted_stays_pending(mdb, monkeypatch):
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    _stub_builder(monkeypatch, result=jm.ProcedureBuilderUnavailable("still down"))
    await _drain(mdb)
    items = await _pending(mdb)
    assert len(items) == 1           # kept for a later cycle
    assert items[0]["attempts"] == 1  # one attempt consumed


@pytest.mark.asyncio
async def test_drain_never_rolls_back_shared_connection(mdb, monkeypatch):
    """Regression: the drain must never call rollback() — ``db`` is the shared
    SerializedConnection, so a rollback would discard other concurrent jobs'
    pending writes, not just ours."""
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    _stub_builder(monkeypatch, result=jm.ProcedureBuilderUnavailable("down"))
    rollback_spy = AsyncMock()
    monkeypatch.setattr(mdb, "rollback", rollback_spy)
    await _drain(mdb)
    rollback_spy.assert_not_called()


@pytest.mark.asyncio
async def test_drain_transcript_gone_discards(mdb, monkeypatch):
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    monkeypatch.setattr(ej, "_find_transcript", lambda d, s: None)
    await _drain(mdb)
    assert await _pending(mdb) == []  # discarded — nothing to rebuild from


@pytest.mark.asyncio
async def test_drain_attempts_exhausted_discards_and_observes(mdb, monkeypatch):
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    items = await _pending(mdb)
    await mdb.execute(
        "UPDATE deferred_work_queue SET attempts = ? WHERE id = ?",
        (ej._MAX_PROCEDURE_REBUILD_ATTEMPTS, items[0]["id"]),
    )
    await mdb.commit()
    await _drain(mdb)
    assert await _pending(mdb) == []  # given up
    rows = await mdb.execute_fetchall(
        "SELECT COUNT(*) FROM observations WHERE type='procedure_extraction_lost'"
    )
    assert rows[0][0] == 1  # the loss is surfaced, not silent


@pytest.mark.asyncio
async def test_reference_only_mode_skips_drain(mdb, monkeypatch):
    """History-mining / reference-only cycles must not touch the rebuild queue."""
    await ej._enqueue_procedure_rebuild(mdb, "s1", "cc1")
    monkeypatch.setattr(ej, "_find_extractable_sessions", AsyncMock(return_value=[]))
    spy = AsyncMock()
    monkeypatch.setattr(ej, "_drain_procedure_rebuilds", spy)
    await ej.run_extraction_cycle(
        db=mdb, store=MagicMock(), router=MagicMock(), reference_only_mode=True,
    )
    spy.assert_not_called()
    assert len(await _pending(mdb)) == 1  # untouched


@pytest.mark.asyncio
async def test_normal_mode_runs_drain(mdb, monkeypatch):
    monkeypatch.setattr(ej, "_find_extractable_sessions", AsyncMock(return_value=[]))
    spy = AsyncMock()
    monkeypatch.setattr(ej, "_drain_procedure_rebuilds", spy)
    await ej.run_extraction_cycle(
        db=mdb, store=MagicMock(), router=MagicMock(), reference_only_mode=False,
    )
    spy.assert_called_once()
