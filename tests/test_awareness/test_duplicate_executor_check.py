"""Tests for the duplicate-CC-executor awareness check (_check_duplicate_cc_executor).

Two live `claude` processes executing the SAME conversation transcript
(2026-07-13 incident) are detected by the hook layer, which writes
``~/.genesis/session-owners/<key>.conflict``; this per-tick check is the
paging layer (critical observation -> Telegram via the critical-observations
job) and the registry GC (stale conflicts + overrides, dead-owner files).

Liveness is a (pid, starttime) match via process_reaper.proc_starttime_ticks,
monkeypatched here — no test depends on real process state or the wall clock.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.awareness import loop
from genesis.db.schema import create_all_tables
from genesis.runtime.init import process_reaper


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def owners(tmp_path, monkeypatch):
    d = tmp_path / "session-owners"
    d.mkdir()
    monkeypatch.setattr(loop, "_SESSION_OWNERS_DIR", d)
    return d


@pytest.fixture
def liveness(monkeypatch):
    """Controllable (pid, starttime) liveness; starts with nothing alive."""
    live: dict[int, int] = {}
    monkeypatch.setattr(process_reaper, "proc_starttime_ticks", lambda pid: live.get(pid))
    return live


def _write_conflict(owners, key="abcd1234", pids=((100, 5), (200, 9))):
    (owners / f"{key}.conflict").write_text(
        json.dumps(
            {
                "transcript_path": "/t/x.jsonl",
                "executors": [{"pid": p, "starttime": s} for p, s in pids],
            }
        )
    )


async def _unresolved(db):
    cur = await db.execute(
        "SELECT content, priority FROM observations "
        "WHERE source='duplicate_session_monitor' AND resolved=0"
    )
    return list(await cur.fetchall())


async def test_missing_dir_is_noop(db, tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "_SESSION_OWNERS_DIR", tmp_path / "nope")
    await loop._check_duplicate_cc_executor(db)
    assert await _unresolved(db) == []


async def test_live_conflict_pages_critical_once(db, owners, liveness):
    liveness.update({100: 5, 200: 9})
    _write_conflict(owners)
    await loop._check_duplicate_cc_executor(db)
    rows = await _unresolved(db)
    assert len(rows) == 1
    assert rows[0]["priority"] == "critical"
    assert "100" in rows[0]["content"] and "200" in rows[0]["content"]
    # Second tick with the alert still unresolved: content_hash dedup, no spam.
    await loop._check_duplicate_cc_executor(db)
    assert len(await _unresolved(db)) == 1
    # Conflict file stays while both executors live (the guard needs it).
    assert (owners / "abcd1234.conflict").exists()


async def test_stale_conflict_gcs_files_and_resolves(db, owners, liveness):
    liveness.update({100: 5, 200: 9})
    _write_conflict(owners)
    await loop._check_duplicate_cc_executor(db)
    assert len(await _unresolved(db)) == 1

    # The older executor dies -> conflict AND its override are collected,
    # and the standing alert auto-resolves.
    del liveness[100]
    (owners / "abcd1234.override").write_text("")
    await loop._check_duplicate_cc_executor(db)
    assert not (owners / "abcd1234.conflict").exists()
    assert not (owners / "abcd1234.override").exists()
    assert await _unresolved(db) == []


async def test_pid_reuse_counts_as_dead(db, owners, liveness):
    liveness.update({100: 5, 200: 777})  # 200 recycled (starttime mismatch)
    _write_conflict(owners)
    await loop._check_duplicate_cc_executor(db)
    assert await _unresolved(db) == []
    assert not (owners / "abcd1234.conflict").exists()


async def test_torn_conflict_is_skipped(db, owners, liveness):
    (owners / "torn.conflict").write_text('{"executors": [{')
    await loop._check_duplicate_cc_executor(db)
    assert await _unresolved(db) == []
    assert (owners / "torn.conflict").exists()  # left for the hook layer


async def test_owner_file_gc_only_dead_and_old(db, owners, liveness, monkeypatch):
    monkeypatch.setattr(loop.time, "time", lambda: 1_000_000.0)
    old = 1_000_000.0 - loop._OWNER_FILE_GC_AGE_S - 1
    liveness.update({300: 7})
    (owners / "dead-old.json").write_text(
        json.dumps({"pid": 100, "starttime": 5, "updated_at": old})
    )
    (owners / "live-old.json").write_text(
        json.dumps({"pid": 300, "starttime": 7, "updated_at": old})
    )
    (owners / "dead-recent.json").write_text(
        json.dumps({"pid": 100, "starttime": 5, "updated_at": 999_999.0})
    )
    await loop._check_duplicate_cc_executor(db, gc_owner_files=True)
    assert not (owners / "dead-old.json").exists()
    assert (owners / "live-old.json").exists()
    assert (owners / "dead-recent.json").exists()


async def test_gc_disabled_leaves_owner_files(db, owners, liveness, monkeypatch):
    monkeypatch.setattr(loop.time, "time", lambda: 1_000_000.0)
    old = 1_000_000.0 - loop._OWNER_FILE_GC_AGE_S - 1
    (owners / "dead-old.json").write_text(
        json.dumps({"pid": 100, "starttime": 5, "updated_at": old})
    )
    await loop._check_duplicate_cc_executor(db, gc_owner_files=False)
    assert (owners / "dead-old.json").exists()


async def test_check_never_raises_into_tick(db, owners, monkeypatch):
    # Whole-body guard: even a pathological failure only logs.
    monkeypatch.setattr(loop, "_live_conflict_pids", lambda c: 1 / 0, raising=True)
    _write_conflict(owners)
    await loop._check_duplicate_cc_executor(db)  # must not raise
