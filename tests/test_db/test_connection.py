"""Tests for SerializedConnection — the concurrency-safe DB proxy."""

import asyncio

import aiosqlite
import pytest

from genesis.db.connection import SerializedConnection


@pytest.fixture
async def sconn():
    """Bare SerializedConnection around an in-memory DB."""
    raw = await aiosqlite.connect(":memory:")
    raw.row_factory = aiosqlite.Row
    await raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    await raw.commit()
    conn = SerializedConnection(raw)
    yield conn
    await raw.close()


async def test_basic_execute_and_commit(sconn):
    await sconn.execute("INSERT INTO t (id, val) VALUES (1, 'a')")
    await sconn.commit()
    cur = await sconn.execute("SELECT val FROM t WHERE id = 1")
    row = await cur.fetchone()
    assert row["val"] == "a"


async def test_execute_as_context_manager(sconn):
    """async with db.execute(...) as cur: pattern must work."""
    await sconn.execute("INSERT INTO t VALUES (1, 'ctx')")
    await sconn.commit()
    async with sconn.execute("SELECT val FROM t WHERE id = 1") as cur:
        row = await cur.fetchone()
        assert row["val"] == "ctx"


async def test_executemany(sconn):
    await sconn.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(1, "a"), (2, "b"), (3, "c")],
    )
    await sconn.commit()
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    row = await cur.fetchone()
    assert row["cnt"] == 3


async def test_execute_fetchall(sconn):
    await sconn.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(1, "x"), (2, "y")],
    )
    await sconn.commit()
    rows = await sconn.execute_fetchall("SELECT val FROM t ORDER BY id")
    assert [r["val"] for r in rows] == ["x", "y"]


async def test_row_factory_passthrough(sconn):
    """row_factory set/get must work through the proxy."""
    assert sconn.row_factory == aiosqlite.Row
    sconn.row_factory = None
    assert sconn.row_factory is None
    sconn.row_factory = aiosqlite.Row


async def test_concurrent_writes_no_errors(sconn):
    """20 concurrent coroutines doing INSERT+commit must all succeed."""
    async def writer(i: int):
        await sconn.execute("INSERT INTO t VALUES (?, ?)", (i, f"val-{i}"))
        await sconn.commit()

    await asyncio.gather(*(writer(i) for i in range(20)))

    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    row = await cur.fetchone()
    assert row["cnt"] == 20


async def test_concurrent_reads_and_writes(sconn):
    """Mixed concurrent reads and writes must not error."""
    async def writer(i: int):
        await sconn.execute("INSERT INTO t VALUES (?, ?)", (i, f"w-{i}"))
        await sconn.commit()

    async def reader():
        cur = await sconn.execute("SELECT count(*) as cnt FROM t")
        row = await cur.fetchone()
        return row["cnt"]

    tasks = []
    for i in range(10):
        tasks.append(writer(i))
        tasks.append(reader())
    await asyncio.gather(*tasks)

    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    row = await cur.fetchone()
    assert row["cnt"] == 10


async def test_rollback(sconn):
    await sconn.execute("INSERT INTO t VALUES (1, 'rollme')")
    await sconn.rollback()
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    row = await cur.fetchone()
    assert row["cnt"] == 0


async def test_in_transaction_passthrough(sconn):
    """in_transaction property must be accessible through proxy."""
    # Access the property — should not raise
    _ = sconn.in_transaction
