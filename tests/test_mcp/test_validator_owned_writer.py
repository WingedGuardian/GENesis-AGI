"""Owned handles must never commit a foreign transaction or lose cleanup."""

import asyncio
import importlib
import importlib.util
import sqlite3
from types import SimpleNamespace

import pytest


def writer_module():
    name = "genesis.mcp.health.validator_writes"
    assert importlib.util.find_spec(name) is not None, "owned writer boundary is required"
    return importlib.import_module(name)


async def test_owned_writer_refuses_to_create_a_missing_store(tmp_path):
    writes = writer_module()
    called = []

    async def operation(db):
        called.append(db)
        return {"ok": True}

    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        await writes.owned_call(SimpleNamespace(_db_path=path), operation)
    assert not path.exists()
    assert called == []


@pytest.mark.parametrize("cancels", [0, 1, 2])
async def test_owned_close_finishes_same_handle_before_cancel(cancels):
    writes = writer_module()
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def close():
        entered.set()
        await release.wait()
        closed.append(True)

    task = asyncio.create_task(writes.finish_connection(SimpleNamespace(close=close)))
    await entered.wait()
    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    if cancels:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert closed == [True]


@pytest.mark.parametrize("phase, retained", [("before_write", 0), ("uncommitted", 0), ("committed", 1)])
async def test_owned_failure_closes_handle_and_reports_uncertainty(tmp_path, monkeypatch, caplog, phase, retained):
    writes = writer_module()
    path = tmp_path / "owned.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE measurements (value TEXT)")
    opened = []
    connect = writes.connect_aiosqlite_rw

    async def record_handle(*args, **kwargs):
        handle = await connect(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(writes, "connect_aiosqlite_rw", record_handle)

    async def operation(db):
        if phase != "before_write":
            await db.execute("INSERT INTO measurements VALUES ('synthetic')")
        if phase == "committed":
            await db.commit()
        raise RuntimeError("Synthetic operation failure")

    with pytest.raises(RuntimeError, match="Synthetic operation failure"):
        await writes.owned_call(SimpleNamespace(_db_path=path), operation)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == retained
    with pytest.raises(ValueError, match="no active connection"):
        await opened[0].execute("SELECT 1")
    assert "outcome unconfirmed; re-read" in caplog.text


@pytest.mark.parametrize("committed", [False, True])
async def test_owned_cancel_closes_actual_handle_without_inventing_outcome(tmp_path, monkeypatch, committed):
    writes = writer_module()
    path = tmp_path / "owned.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE measurements (value TEXT)")
    entered = asyncio.Event()
    opened = []
    connect = writes.connect_aiosqlite_rw

    async def record_handle(*args, **kwargs):
        handle = await connect(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(writes, "connect_aiosqlite_rw", record_handle)

    async def operation(db):
        await db.execute("INSERT INTO measurements VALUES ('synthetic')")
        if committed:
            await db.commit()
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(writes.owned_call(SimpleNamespace(_db_path=path), operation))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == int(committed)
    with pytest.raises(ValueError, match="no active connection"):
        await opened[0].execute("SELECT 1")


async def test_known_committed_result_survives_close_error(tmp_path, monkeypatch):
    writes = writer_module()
    path = tmp_path / "owned.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE measurements (value TEXT)")
    close = writes.finish_connection

    async def closed_then_failed(db):
        await close(db)
        raise OSError("Synthetic cleanup failure")

    monkeypatch.setattr(writes, "finish_connection", closed_then_failed)

    async def operation(db):
        cursor = await db.execute("INSERT INTO measurements VALUES ('synthetic')")
        await db.commit()
        return {"id": cursor.lastrowid}

    result = await writes.owned_call(SimpleNamespace(_db_path=path), operation)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT rowid FROM measurements").fetchall() == [(result["id"],)]
    assert "completed; cleanup failed" in result["warning"]
