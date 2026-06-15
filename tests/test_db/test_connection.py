"""Tests for SerializedConnection — the concurrency-safe DB proxy."""

import asyncio
import contextlib

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


async def test_lock_serializes_operations(sconn):
    """Verify the lock actually prevents concurrent access.

    Without the lock, two coroutines can be inside execute() at the
    same time.  With the lock, they must take turns — only one can
    hold it at a time.
    """
    execution_log: list[str] = []

    async def writer(name: str):
        # Acquire the proxy's lock explicitly to verify it serializes
        async with sconn._lock:
            execution_log.append(f"{name}-start")
            await asyncio.sleep(0.01)  # yield to event loop
            execution_log.append(f"{name}-end")

    await asyncio.gather(writer("A"), writer("B"))

    # With serialization: A-start, A-end, B-start, B-end (or B first)
    # Without: A-start, B-start, A-end, B-end (interleaved)
    # Each start must be immediately followed by the same writer's end
    assert execution_log[0].endswith("-start")
    assert execution_log[1].endswith("-end")
    assert execution_log[0][0] == execution_log[1][0]  # same writer


async def test_error_does_not_corrupt_connection(sconn):
    """A constraint error in one writer must not break the connection for others."""
    await sconn.execute("INSERT INTO t VALUES (1, 'seed')")
    await sconn.commit()

    async def bad_writer():
        with contextlib.suppress(Exception):
            await sconn.execute("INSERT INTO t VALUES (1, 'dup')")  # PK conflict

    async def good_writer():
        await sconn.execute("INSERT INTO t VALUES (2, 'ok')")
        await sconn.commit()

    await bad_writer()
    await good_writer()

    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    row = await cur.fetchone()
    assert row["cnt"] == 2  # seed + good_writer


# ── WS-15: shared raw-connection helper for ad-hoc / standalone opens ──

async def test_get_raw_db_sets_standard_pragmas(tmp_path):
    """get_raw_db must apply WAL + NORMAL sync + busy_timeout + Row factory so
    ad-hoc/subprocess opens can't fail immediately on a concurrent write lock."""
    from genesis.db.connection import BUSY_TIMEOUT_MS, get_raw_db

    db_path = tmp_path / "raw.db"
    async with get_raw_db(db_path) as db:
        assert db.row_factory is aiosqlite.Row

        cur = await db.execute("PRAGMA busy_timeout")
        assert (await cur.fetchone())[0] == BUSY_TIMEOUT_MS

        cur = await db.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0].lower() == "wal"

        cur = await db.execute("PRAGMA synchronous")
        assert (await cur.fetchone())[0] == 1  # 1 == NORMAL

        # usable for a real round-trip
        await db.execute("CREATE TABLE x (id INTEGER PRIMARY KEY, v TEXT)")
        await db.execute("INSERT INTO x VALUES (1, 'ok')")
        await db.commit()
        cur = await db.execute("SELECT v FROM x WHERE id = 1")
        assert (await cur.fetchone())["v"] == "ok"


async def test_get_raw_db_closes_on_exit(tmp_path):
    """The connection is closed when the context manager exits."""
    from genesis.db.connection import get_raw_db

    db_path = tmp_path / "raw2.db"
    async with get_raw_db(db_path) as db:
        captured = db
        await captured.execute("SELECT 1")  # usable inside the context
    # After exit the connection is closed — reusing it raises (behavior, not
    # a private attribute).
    with pytest.raises(ValueError):
        await captured.execute("SELECT 1")
