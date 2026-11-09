"""A write transaction held open on a shared connection is named in the log.

In WAL mode the connection inside a write transaction holds the database's
writer slot until it commits; every other process that writes meanwhile waits
and gives up after its busy_timeout. These pin that a long gap between the
opening write and the commit is logged with WHO opened it, that a short one is
not, and that the instrument can never break the write path.

Time is a fake clock (``_txn_hold._clock``), advanced between statements, the
way a stalled event loop or slow work would hold the transaction open.
"""

from __future__ import annotations

import contextlib
import logging

import aiosqlite
import pytest

from genesis.db import _txn_hold
from genesis.db.connection import SerializedConnection
from genesis.env import sqlite_txn_hold_ms

LOGGER = "genesis.db._txn_hold"


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
    monkeypatch.setattr(_txn_hold, "_clock", c)
    monkeypatch.delenv("GENESIS_SQLITE_TXN_HOLD_MS", raising=False)
    _txn_hold._reset()
    yield c
    _txn_hold._reset()


@pytest.fixture
async def sconn(tmp_path, clock):
    conn = await aiosqlite.connect(str(tmp_path / "hold.db"))
    sc = SerializedConnection(conn)
    await sc.execute("CREATE TABLE t (x INTEGER)")
    await sc.commit()
    yield sc
    with contextlib.suppress(Exception):
        await sc.close()


def _held_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


async def _write_then_hold(sc, clock, ms, *, close="commit"):
    await sc.execute("INSERT INTO t VALUES (1)")  # opens the implicit transaction
    clock.advance(ms)
    await (sc.commit() if close == "commit" else sc.rollback())


async def test_a_long_held_transaction_names_its_opener(sconn, clock, caplog, monkeypatch):
    from genesis.util import loop_health

    monkeypatch.setattr(loop_health, "_last_lagging_monotonic", None)  # no lag seen
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _write_then_hold(sconn, clock, 6500)
    lines = _held_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("sqlite txn held: 6500ms opened_by=INSERT INTO t VALUES (1)")
    # The origin is the first caller outside genesis.db: this test.
    assert f"via {__name__}:_write_then_hold" in line
    assert "closed_by=COMMIT" in line
    assert line.endswith("loop_lag=no")


async def test_a_hold_overlapping_loop_lag_is_marked(sconn, clock, caplog, monkeypatch):
    """Under a stalled loop the opener is a bystander; the line must say so."""
    from genesis.util import loop_health

    monkeypatch.setattr(loop_health, "_last_lagging_monotonic", __import__("time").monotonic())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _write_then_hold(sconn, clock, 4000)
    (line,) = _held_lines(caplog)
    assert line.endswith("loop_lag=yes")


async def test_an_async_with_write_is_credited_to_its_caller_not_the_driver(sconn, clock, caplog):
    async def job():
        async with sconn.execute("INSERT INTO t VALUES (3) RETURNING x") as cur:
            await cur.fetchall()
        clock.advance(2000)
        await sconn.commit()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await job()
    (line,) = _held_lines(caplog)
    assert "aiosqlite" not in line.split(" task=")[0], line
    assert f"via {__name__}:job" in line


async def test_closing_with_a_transaction_open_reports_the_hold(sconn, clock, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await sconn.execute("INSERT INTO t VALUES (4)")
        clock.advance(3000)
        await sconn.close()
    (line,) = _held_lines(caplog)
    assert "closed_by=<close>" in line


async def test_site_is_the_crud_function_and_origin_is_its_caller(sconn, clock, caplog):
    """Through a crud layer, `site` names the crud function and `origin` the
    business code above it, which is the one worth fixing."""
    crud_globals = {"__name__": "genesis.db.crud.fake_store"}
    exec(  # noqa: S102 - builds a function whose module is a crud module
        "async def insert_row(db):\n    await db.execute('INSERT INTO t VALUES (2)')\n",
        crud_globals,
    )
    insert_row = crud_globals["insert_row"]

    async def business_job():
        await insert_row(sconn)
        clock.advance(3000)
        await sconn.commit()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await business_job()
    (line,) = _held_lines(caplog)
    assert "at genesis.db.crud.fake_store:insert_row via " in line
    assert f"via {__name__}:business_job" in line


async def test_a_short_transaction_is_not_logged(sconn, clock, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _write_then_hold(sconn, clock, 999)
    assert _held_lines(caplog) == []


async def test_a_rollback_closes_it_too(sconn, clock, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _write_then_hold(sconn, clock, 2000, close="rollback")
    (line,) = _held_lines(caplog)
    assert "closed_by=ROLLBACK" in line


async def test_reads_outside_a_transaction_are_not_tracked(sconn, clock, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await sconn.execute_fetchall("SELECT * FROM t")
        clock.advance(10_000)
        await sconn.execute_fetchall("SELECT * FROM t")
    assert _held_lines(caplog) == []
    assert sconn._txn_hold.opened_at is None


async def test_zero_turns_it_off(sconn, clock, caplog, monkeypatch):
    monkeypatch.setenv("GENESIS_SQLITE_TXN_HOLD_MS", "0")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _write_then_hold(sconn, clock, 60_000)
    assert _held_lines(caplog) == []


def test_threshold_parsing(monkeypatch):
    monkeypatch.delenv("GENESIS_SQLITE_TXN_HOLD_MS", raising=False)
    assert sqlite_txn_hold_ms() == 1000
    monkeypatch.setenv("GENESIS_SQLITE_TXN_HOLD_MS", "250")
    assert sqlite_txn_hold_ms() == 250
    monkeypatch.setenv("GENESIS_SQLITE_TXN_HOLD_MS", "junk")
    assert sqlite_txn_hold_ms() == 1000


async def test_repeats_are_rate_limited_but_a_much_longer_hold_is_not(sconn, clock, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _write_then_hold(sconn, clock, 2000)
        await _write_then_hold(sconn, clock, 2500)  # within 60 s, < 2x: suppressed
        await _write_then_hold(sconn, clock, 5000)  # >= 2x the last logged: logged
    lines = _held_lines(caplog)
    assert [line.split("ms ", 1)[0] for line in lines] == [
        "sqlite txn held: 2000",
        "sqlite txn held: 5000",
    ]
    assert lines[1].endswith("(+1 suppressed)")


async def test_a_broken_instrument_never_breaks_the_write(sconn, clock, monkeypatch):
    def _boom(_sql):
        raise RuntimeError("label failure")

    monkeypatch.setattr(_txn_hold, "sql_label", _boom)
    await _write_then_hold(sconn, clock, 5000)
    rows = await sconn.execute_fetchall("SELECT COUNT(*) FROM t")
    assert rows[0][0] == 1, "the write must commit even when the tracker raises"
