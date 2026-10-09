"""Slow SQLite statements are named in the log the first time they happen.

Time is driven by a fake clock (``_slow_log._clock``), never the wall clock: a
SQLite function ``tick(ms, …)`` advances it from inside a running statement,
and lock waits are advanced while the test holds the connection's lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.db import _slow_log
from genesis.db.connection import SerializedConnection
from genesis.env import sqlite_slow_ms

LOGGER = "genesis.db._slow_log"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms / 1000


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(_slow_log, "_clock", c)
    monkeypatch.delenv("GENESIS_SQLITE_SLOW_MS", raising=False)
    _slow_log._reset()
    yield c
    _slow_log._reset()


@pytest.fixture
async def sconn(tmp_path, clock):
    conn = await aiosqlite.connect(str(tmp_path / "slow.db"))

    def tick(ms, *_):
        clock.advance(ms)
        return 1

    await conn.create_function("tick", -1, tick)
    sc = SerializedConnection(conn)
    yield sc
    with contextlib.suppress(Exception):
        await sc.close()


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


async def test_a_fast_statement_logs_nothing(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await sconn.execute_fetchall("SELECT tick(999)")
    assert _lines(caplog) == []


async def test_a_slow_statement_is_named_once_with_its_split(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await sconn.execute_fetchall("SELECT   tick(1500)\n  -- spread over lines")
    (line,) = _lines(caplog)
    assert line.startswith("sqlite slow: SELECT tick(1500) -- spread over lines ")
    assert "total=1500ms waited=0ms ran=1500ms outcome=ok blocked_by=-" in line


async def test_parameters_are_never_logged(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await sconn.execute_fetchall("SELECT tick(?, ?)", [2000, "SECRET-PROMPT-TEXT"])
    (line,) = _lines(caplog)
    assert "SECRET-PROMPT-TEXT" not in line
    assert "SELECT tick(?, ?)" in line


async def test_every_timed_method_carries_its_label(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await sconn.execute("CREATE TABLE t(x)")
    await sconn.execute("INSERT INTO t SELECT tick(1200)")
    await sconn.executemany("INSERT INTO t SELECT tick(?)", [(1200,)])
    await sconn.execute_insert("INSERT INTO t SELECT tick(1202)")
    await sconn.executescript("INSERT INTO t SELECT tick(1201);")
    await sconn.commit()  # fast: no line
    labels = [line.split(" total=")[0] for line in _lines(caplog)]
    assert labels == [
        "sqlite slow: INSERT INTO t SELECT tick(1200)",
        "sqlite slow: INSERT INTO t SELECT tick(?)",
        "sqlite slow: INSERT INTO t SELECT tick(1202)",
        "sqlite slow: INSERT INTO t SELECT tick(1201);",
    ]


async def test_commit_and_rollback_are_labelled(sconn, clock, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    real = sconn._conn

    class _SlowEnd:
        def __getattr__(self, name):
            return getattr(real, name)

        async def commit(self):
            clock.advance(1100)

        async def rollback(self):
            clock.advance(1100)

    object.__setattr__(sconn, "_conn", _SlowEnd())
    await sconn.commit()
    await sconn.rollback()
    object.__setattr__(sconn, "_conn", real)
    assert [line.split(" total=")[0] for line in _lines(caplog)] == [
        "sqlite slow: COMMIT",
        "sqlite slow: ROLLBACK",
    ]


async def test_rate_limited_per_label_then_reports_the_suppressed(sconn, clock, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    for _ in range(3):
        await sconn.execute_fetchall("SELECT tick(1500)")
    assert len(_lines(caplog)) == 1
    await sconn.execute_fetchall("SELECT tick(1501)")  # a different label still logs
    assert len(_lines(caplog)) == 2
    clock.advance(60_000)
    await sconn.execute_fetchall("SELECT tick(1500)")
    assert _lines(caplog)[-1].endswith("(+2 suppressed)")


async def test_a_much_slower_repeat_is_never_suppressed(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await sconn.execute_fetchall("SELECT tick(?)", [1500])
    await sconn.execute_fetchall("SELECT tick(?)", [2999])  # under 2x: held back
    await sconn.execute_fetchall("SELECT tick(?)", [3000])  # 2x: logged
    totals = [line.split("total=")[1].split("ms")[0] for line in _lines(caplog)]
    assert totals == ["1500", "3000"]
    assert _lines(caplog)[-1].endswith("(+1 suppressed)")


async def test_a_different_outcome_is_not_suppressed_by_an_ok_line(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    sql = "SELECT tick(1500) + abs(?)"
    await sconn.execute_fetchall(sql, [1])
    with pytest.raises(sqlite3.OperationalError):
        await sconn.execute_fetchall(sql, [-9223372036854775808])
    outcomes = [line.split("outcome=")[1].split(" ")[0] for line in _lines(caplog)]
    assert outcomes == ["ok", "error:OperationalError"]


async def test_cursor_fetches_are_timed_under_the_statement(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    sql = "SELECT tick(600) FROM (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3)"
    cur = await sconn.execute(sql)  # steps to the first row: 600 ms, under the bar
    assert _lines(caplog) == []
    rows = await cur.fetchall()  # all 3 rows; stepping the last two costs 1200 ms
    assert len(rows) == 3
    (line,) = _lines(caplog)
    assert line.startswith(f"sqlite slow: {sql} total=1200ms waited=0ms ran=1200ms outcome=ok")


async def test_a_fetch_in_flight_is_named_by_a_statement_behind_it(sconn, clock, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    seen: list[object] = []
    await sconn._conn.create_function(
        "seen", 0, lambda: seen.append(list(sconn._inflight.values())) or 1
    )
    cur = await sconn.execute("SELECT seen() FROM (SELECT 1 UNION ALL SELECT 2)")
    await cur.fetchall()
    # set while its rows were fetched, cleared afterwards
    assert seen == [[], ["SELECT seen() FROM (SELECT 1 UNION ALL SELECT 2)"]]
    assert sconn._inflight == {}
    sconn._inflight[object()] = "SELECT big scan"
    await sconn.execute_fetchall("SELECT tick(1500)")
    assert _lines(caplog)[-1].endswith("blocked_by=SELECT big scan")


async def test_a_cancelled_holder_is_named_by_the_next_statement_once(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)

    async def hold():
        async with sconn._locked_timed("CANCELLED SQL"):
            await asyncio.sleep(3600)

    task = asyncio.ensure_future(hold())
    for _ in range(3):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sconn.execute_fetchall("SELECT tick(1500)")
    await sconn.execute_fetchall("SELECT tick(1501)")
    blocked = [line.split("blocked_by=")[1] for line in _lines(caplog)]
    assert blocked == ["CANCELLED SQL", "-"]


async def test_the_cursor_still_works_as_a_cursor(sconn):
    await sconn.execute("CREATE TABLE t(x)")
    await sconn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    cur = await sconn.execute("INSERT INTO t VALUES (9)")
    assert cur.lastrowid == 6 and cur.rowcount == 1
    async with sconn.execute("SELECT x FROM t ORDER BY x") as cur:
        assert [row[0] async for row in cur] == [0, 1, 2, 3, 4, 9]
    cur = await sconn.execute("SELECT x FROM t ORDER BY x")
    assert (await cur.fetchone())[0] == 0
    assert [r[0] for r in await cur.fetchmany(2)] == [1, 2]
    await cur.close()


async def test_async_with_closes_the_cursor(sconn, monkeypatch):
    closed: list[bool] = []
    async with sconn.execute("SELECT 1") as cur:
        real_close = cur._cursor.close

        async def spy():
            closed.append(True)
            await real_close()

        monkeypatch.setattr(cur._cursor, "close", spy)
        await cur.fetchone()
    assert closed == [True]


async def test_async_for_fetches_in_aiosqlite_batches(sconn):
    await sconn.execute("CREATE TABLE t(x)")
    await sconn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(200)])
    cur = await sconn.execute("SELECT x FROM t")
    sizes: list[object] = []
    real = cur._cursor.fetchmany

    async def spy(size=None):
        sizes.append(size)
        return await real(size)

    cur._cursor.fetchmany = spy
    assert len([row async for row in cur]) == 200
    assert sizes == [64, 64, 64, 64, 64]  # 200 rows in 4 batches, then an empty one


async def test_cursor_attributes_can_be_set_through_the_wrapper(sconn):
    cur = await sconn.execute("SELECT 1 AS one")
    cur.row_factory = sqlite3.Row
    cur.arraysize = 7
    assert cur._cursor.arraysize == 7
    assert (await cur.fetchone())["one"] == 1


async def test_a_cancelled_fetch_is_named_by_the_next_statement(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    cur = await sconn.execute("SELECT 'FETCHED SQL'")
    started = asyncio.Event()

    async def never(*_):
        started.set()
        await asyncio.sleep(3600)

    cur._cursor.fetchall = never
    task = asyncio.ensure_future(cur.fetchall())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sconn._inflight == {}
    await sconn.execute_fetchall("SELECT tick(1500)")
    assert _lines(caplog)[-1].endswith("blocked_by=SELECT 'FETCHED SQL'")


async def test_a_finished_fetch_leaves_another_in_flight_fetch_marked(sconn):
    outer = object()
    sconn._inflight[outer] = "OUTER FETCH"
    cur = await sconn.execute("SELECT 1")
    await cur.fetchall()
    assert sconn._inflight == {outer: "OUTER FETCH"}


def test_savepoint_ids_collapse_in_labels():
    assert _slow_log.sql_label("RELEASE zd_sweep_" + "a1" * 16) == "RELEASE zd_sweep_<id>"


async def test_threshold_zero_turns_it_off(sconn, caplog, monkeypatch):
    monkeypatch.setenv("GENESIS_SQLITE_SLOW_MS", "0")
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await sconn.execute_fetchall("SELECT tick(60000)")
    assert _lines(caplog) == []


async def _queue_behind_holder(sconn, clock, coro_factory, wait_ms):
    """Hold the lock as "HOLDER SQL", queue ``coro_factory()`` behind it, let
    ``wait_ms`` pass, and return the queued task (lock released on return)."""
    async with sconn._locked_timed("HOLDER SQL"):
        task = asyncio.ensure_future(coro_factory())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()
        clock.advance(wait_ms)
    return task


async def test_wait_is_split_from_run_and_names_the_holder(sconn, clock, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    task = await _queue_behind_holder(
        sconn, clock, lambda: sconn.execute_fetchall("SELECT tick(100)"), 2000
    )
    await task
    waiter = [line for line in _lines(caplog) if "SELECT tick(100)" in line]
    assert waiter == [
        "sqlite slow: SELECT tick(100) total=2100ms waited=2000ms ran=100ms"
        " outcome=ok blocked_by=HOLDER SQL"
    ]


async def test_a_cancelled_wait_is_logged_as_cancelled(sconn, clock, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    async with sconn._locked_timed("HOLDER SQL"):
        task = asyncio.ensure_future(sconn.execute_fetchall("SELECT 1"))
        for _ in range(5):
            await asyncio.sleep(0)
        clock.advance(4500)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    waiter = [line for line in _lines(caplog) if "SELECT 1 " in line]
    assert waiter == [
        "sqlite slow: SELECT 1 total=4500ms waited=4500ms ran=0ms"
        " outcome=cancelled blocked_by=HOLDER SQL"
    ]


async def test_an_error_is_logged_and_still_raised(sconn, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    # tick runs, then abs() overflows at RUN time: a slow statement that fails.
    with pytest.raises(sqlite3.OperationalError, match="integer overflow"):
        await sconn.execute_fetchall("SELECT tick(1500) + abs(-9223372036854775808)")
    (line,) = _lines(caplog)
    assert "ran=1500ms outcome=error:OperationalError" in line


# ── recall's pooled reads ─────────────────────────────────────────────────────


async def test_ro_read_is_labelled_by_helper_not_arguments(clock, caplog):
    from genesis.memory.retrieval import HybridRetriever

    caplog.set_level(logging.WARNING, logger=LOGGER)

    class _Pool:
        @contextlib.asynccontextmanager
        async def acquire(self):
            clock.advance(300)  # checkout wait
            yield object()

    async def search_helper(conn, query):
        clock.advance(900)
        return [query]

    stub = SimpleNamespace(_read_pool=_Pool(), _db=None)
    out = await HybridRetriever._ro_read(stub, search_helper, "SECRET PROMPT")
    assert out == ["SECRET PROMPT"]
    (line,) = _lines(caplog)
    assert "SECRET PROMPT" not in line
    assert line.startswith("sqlite slow: ro:")
    assert "search_helper total=1200ms waited=300ms ran=900ms outcome=ok" in line


# ── the threshold accessor ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 1000), ("", 1000), ("250", 250), ("0", 0), ("junk", 1000)]
)
def test_threshold_accessor(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("GENESIS_SQLITE_SLOW_MS", raising=False)
    else:
        monkeypatch.setenv("GENESIS_SQLITE_SLOW_MS", raw)
    assert sqlite_slow_ms() == expected


async def test_overlapping_fetches_never_leave_a_finished_one_named(sconn, caplog):
    """Codex review (#3122): fetches on two cursors overlap. A saved None, B saved
    A; A finished and cleared the marker while B ran; B then restored A, which
    every later slow statement named as its blocker, indefinitely."""
    from genesis.db.connection import _TimedCursor

    caplog.set_level(logging.WARNING, logger=LOGGER)
    gates = {"A": asyncio.Event(), "B": asyncio.Event()}

    def cursor(name):
        async def fetch():
            await gates[name].wait()
            return []

        return _TimedCursor(SimpleNamespace(fetchall=fetch), f"SELECT {name}", sconn)

    a = asyncio.ensure_future(cursor("A").fetchall())
    await asyncio.sleep(0)
    b = asyncio.ensure_future(cursor("B").fetchall())
    await asyncio.sleep(0)
    gates["A"].set()
    await a  # A is done; B is still fetching
    await sconn.execute_fetchall("SELECT tick(1500)")
    assert _lines(caplog)[-1].endswith("blocked_by=SELECT B")
    gates["B"].set()
    await b
    await sconn.execute_fetchall("SELECT tick(1501)")
    assert _lines(caplog)[-1].endswith("blocked_by=-"), "a finished fetch is still named"
