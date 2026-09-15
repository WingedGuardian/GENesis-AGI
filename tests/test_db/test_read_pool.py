"""ReadConnectionPool + open_ro_connection (follow-up ac27b693, PR-4).

A dedicated ``mode=ro`` pool lets recall's read stages run off the shared
SerializedConnection write lock. These tests pin the load-bearing invariants:

- **checkout exclusivity** — the entire no-per-connection-lock design rests on a
  connection only ever being held by one coroutine at a time;
- **``mode=ro`` is truly read-only** — a pooled connection cannot write;
- **WAL-awareness** — a pooled reader sees committed writes (the ``immutable=1``
  trap would miss un-checkpointed WAL frames);
- **clean close/lifecycle** — idempotent close, ReadPoolClosed after close/before
  open, and no slot leak when a read errors inside the block.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3

import aiosqlite
import pytest

from genesis.db.connection import (
    ReadConnectionPool,
    ReadPoolClosed,
    get_db,
    open_ro_connection,
)


async def _seed_db(path) -> None:
    db = await aiosqlite.connect(str(path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    await db.execute("INSERT INTO t (id, v) VALUES (1, 'a'), (2, 'b')")
    await db.commit()
    await db.close()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "pool.db"


async def test_open_ro_connection_reads_and_row_factory(db_path):
    await _seed_db(db_path)
    conn = await open_ro_connection(db_path)
    try:
        rows = await conn.execute_fetchall("SELECT v FROM t ORDER BY id")
        assert [r[0] for r in rows] == ["a", "b"]
        # Row-factory parity with get_db: named column access must work, or the
        # RO connection silently diverges from self._db (architect SHOULD-FIX).
        named = await conn.execute_fetchall("SELECT v FROM t WHERE id = 1")
        assert named[0]["v"] == "a"
    finally:
        await conn.close()


async def test_ro_connection_cannot_write(db_path):
    """Pins the zero-write invariant — a mode=ro handle rejects INSERT."""
    await _seed_db(db_path)
    conn = await open_ro_connection(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await conn.execute("INSERT INTO t (id, v) VALUES (3, 'c')")
    finally:
        await conn.close()


async def test_pool_acquire_reads(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    try:
        async with pool.acquire() as conn:
            rows = await conn.execute_fetchall("SELECT COUNT(*) FROM t")
            assert rows[0][0] == 2
    finally:
        await pool.close()


async def test_checkout_is_exclusive(db_path):
    """Load-bearing invariant: a checked-out connection is NEVER handed to a
    second coroutine while held. With size=1 a second acquire blocks until the
    first releases; a bug that double-hands the connection resurrects the exact
    in_transaction corruption SerializedConnection exists to prevent.
    """
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=1)
    await pool.open()
    order: list[str] = []
    try:
        async with pool.acquire() as c1:
            started = asyncio.Event()

            async def second() -> None:
                started.set()
                async with pool.acquire() as c2:
                    order.append("second-acquired")
                    assert c2 is c1  # size=1 → the one connection, reused

            task = asyncio.create_task(second())
            await started.wait()
            await asyncio.sleep(0.05)  # give second() a chance to (not) acquire
            assert order == []  # still blocked behind the held checkout
            order.append("first-releasing")
        await task
        assert order == ["first-releasing", "second-acquired"]
    finally:
        await pool.close()


async def test_pool_sees_committed_writes(db_path):
    """mode=ro is WAL-aware: a write committed on the writer is visible to a
    pooled reader. (immutable=1 would miss the un-checkpointed WAL frame — the
    documented trap this pool must not fall into.)
    """
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=1)
    await pool.open()
    writer = await get_db(db_path)
    try:
        await writer.execute("INSERT INTO t (id, v) VALUES (3, 'c')")
        await writer.commit()
        async with pool.acquire() as conn:
            rows = await conn.execute_fetchall("SELECT v FROM t WHERE id = 3")
            assert rows and rows[0][0] == "c"
    finally:
        await writer.close()
        await pool.close()


async def test_error_in_block_returns_connection(db_path):
    """An error inside the acquire block must still return the connection — an
    autocommit mode=ro SELECT leaves no dangling transaction, so the slot is
    safe to reuse. A leaked slot would deadlock the (size=1) pool on the next
    acquire; this test would hang instead of passing if the finally regressed.
    """
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=1)
    await pool.open()
    try:
        with pytest.raises(ValueError):
            async with pool.acquire() as conn:
                await conn.execute_fetchall("SELECT 1")
                raise ValueError("boom")
        # Slot returned — this acquire would block forever if it leaked.
        async with asyncio.timeout(2):
            async with pool.acquire() as conn:
                rows = await conn.execute_fetchall("SELECT COUNT(*) FROM t")
                assert rows[0][0] == 2
    finally:
        await pool.close()


async def test_acquire_after_close_raises(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    await pool.close()
    with pytest.raises(ReadPoolClosed):
        async with pool.acquire():
            pass


async def test_acquire_before_open_raises(db_path):
    pool = ReadConnectionPool(db_path, size=2)
    with pytest.raises(ReadPoolClosed):
        async with pool.acquire():
            pass


async def test_close_is_idempotent(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    await pool.close()
    await pool.close()  # second close must not raise


async def test_open_is_idempotent(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    await pool.open()  # second open is a no-op, does not double the pool
    try:
        assert pool._queue.qsize() == 2
    finally:
        await pool.close()


async def test_size_floored_at_one(db_path):
    assert ReadConnectionPool(db_path, size=0).size == 1
    assert ReadConnectionPool(db_path, size=-3).size == 1


def test_default_pool_size_is_derived_within_explicit_bounds():
    """The shipped default is DERIVED from the host, never a fixed number.

    Generalizability gate: a fixed 4 was the binding constraint that produced
    recall request-budget timeouts once several sessions each ran a per-prompt
    recall. It must scale with the host while staying inside bounds that are
    stated rather than implied.
    """
    from genesis.db.connection import (
        DEFAULT_READ_POOL_SIZE,
        MAX_READ_POOL_SIZE,
        MIN_READ_POOL_SIZE,
    )

    assert MIN_READ_POOL_SIZE <= DEFAULT_READ_POOL_SIZE <= MAX_READ_POOL_SIZE
    # The floor is the previously shipped value: a low-core host must not regress.
    assert MIN_READ_POOL_SIZE == 4


@pytest.mark.parametrize(
    ("cpus", "expected"),
    [
        pytest.param(1, 4, id="single-core-gets-the-floor-not-1"),
        pytest.param(4, 4, id="at-the-floor"),
        pytest.param(8, 8, id="tracks-cpu-count-in-range"),
        pytest.param(64, 12, id="many-cores-capped"),
        pytest.param(None, 4, id="unknown-cpu-count-falls-back-to-floor"),
    ],
)
def test_default_pool_size_derivation_across_hosts(cpus, expected):
    """Both directions of the derivation, so neither bound can silently rot.

    Calls the real ``derive_read_pool_size`` with the count INJECTED. Patching
    ``os.cpu_count`` and re-reading ``DEFAULT_READ_POOL_SIZE`` would measure
    nothing (the constant is computed at import time and the module is cached),
    and recomputing the clamp here would only test this test's own arithmetic.
    """
    from genesis.db.connection import derive_read_pool_size

    assert derive_read_pool_size(cpus) == expected


def test_pool_size_uses_process_affinity_not_host_cpus(monkeypatch):
    """A container constrained to N CPUs must size from N, not the host's cores.

    ``os.cpu_count()`` reports HOST logical CPUs, so on a 4-CPU container on a
    32-core host it would open the ceiling's worth of readers — the exact
    "more readers than cores buys queueing" case the bounds exist to prevent.
    """
    from genesis.db import connection

    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3})
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    assert connection.available_cpu_count() == 4
    assert connection.derive_read_pool_size(connection.available_cpu_count()) == 4


def test_available_cpu_count_falls_back_to_host_when_affinity_unavailable(monkeypatch):
    """Non-Linux / unreadable affinity must degrade to the host count, not crash."""
    from genesis.db import connection

    def _boom(_pid):
        raise OSError("no affinity")

    monkeypatch.setattr(os, "sched_getaffinity", _boom)
    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    assert connection.available_cpu_count() == 6


async def test_partial_open_closes_what_it_opened(db_path, monkeypatch):
    """A failed open must not leak the connections it already made.

    Each aiosqlite connection owns a worker thread, and the caller
    (runtime/init/memory.py) drops the pool reference on failure — so anything
    left open becomes unreachable AND alive. Asserts against the real close()
    calls, not a flag.
    """
    from genesis.db import connection as conn_mod

    await _seed_db(db_path)  # mode=ro requires the file to already exist
    opened: list[object] = []
    real_open = conn_mod.open_ro_connection

    async def flaky(*args, **kwargs):
        if len(opened) >= 3:
            raise OSError("simulated fd/thread ceiling")
        c = await real_open(*args, **kwargs)
        opened.append(c)
        return c

    monkeypatch.setattr(conn_mod, "open_ro_connection", flaky)
    pool = conn_mod.ReadConnectionPool(db_path, size=8)
    with pytest.raises(OSError):
        await pool.open()

    assert len(opened) == 3, "fixture must actually have opened some connections first"
    # Every connection it opened is closed: a closed aiosqlite connection has no
    # live worker thread, which is the resource that was leaking.
    for c in opened:
        assert not c._running, "connection left open after a failed pool open"
    # And the pool is UN-opened rather than permanently closed, so acquire()
    # gives callers their documented fallback.
    assert pool._all == []
    with pytest.raises(conn_mod.ReadPoolClosed):
        async with pool.acquire():
            pass


async def test_checkout_is_bounded_and_degrades_to_the_fallback(db_path):
    """An exhausted pool must DEGRADE, never wait forever.

    The class docstring outsources the bound to "the route's own timeout", which
    is true only inside genesis-server. An MCP child serves tool calls with no
    route budget, so an unbounded checkout there waits indefinitely — and a
    pooled reader can legitimately hold its slot for the whole busy_timeout
    (15s in MCP children) behind a WAL checkpoint. Exhaustion now raises
    ReadPoolClosed, which every caller already handles by falling back to the
    shared connection.
    """
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=1, checkout_timeout_s=0.05)
    await pool.open()
    try:
        async with pool.acquire():  # holds the only slot
            async with asyncio.timeout(2):  # would hang here without the bound
                with pytest.raises(ReadPoolClosed):
                    async with pool.acquire():
                        pass
        # The slot is returned, so a later checkout still succeeds — the timeout
        # must not have poisoned the pool.
        async with pool.acquire() as conn:
            rows = await conn.execute_fetchall("SELECT COUNT(*) FROM t")
            assert rows[0][0] == 2
    finally:
        await pool.close()


def test_session_pool_is_sized_by_ROLE_not_by_the_host(monkeypatch):
    """A per-session MCP child must NOT take the host-derived size.

    There are exactly two pool constructors and they differ in CARDINALITY: the
    server is ONE per box, an MCP child is one per CC SESSION. Feeding both from
    one host-derived number multiplies it by the number of live sessions —
    measured at 6 children on an 8-core box, 56 pooled connections instead of 28.
    """
    from genesis import env
    from genesis.db.connection import (
        DEFAULT_SESSION_READ_POOL_SIZE,
        MIN_READ_POOL_SIZE,
    )

    monkeypatch.delenv("GENESIS_SESSION_READ_POOL_SIZE", raising=False)
    monkeypatch.delenv("GENESIS_RECALL_READ_POOL_SIZE", raising=False)
    # The session size is small and FIXED — it must not track the host at all,
    # which is the whole point of the split.
    assert env.session_read_pool_size() == DEFAULT_SESSION_READ_POOL_SIZE
    assert MIN_READ_POOL_SIZE > DEFAULT_SESSION_READ_POOL_SIZE
    # Its own lever, independent of the server's.
    monkeypatch.setenv("GENESIS_SESSION_READ_POOL_SIZE", "5")
    assert env.session_read_pool_size() == 5
    monkeypatch.setenv("GENESIS_SESSION_READ_POOL_SIZE", "not-an-int")
    assert env.session_read_pool_size() == DEFAULT_SESSION_READ_POOL_SIZE


def test_session_pool_honours_the_legacy_knob_on_upgrade(monkeypatch):
    """An install that CONSTRAINED the child via the old variable must stay
    constrained after the role split.

    Before the split this process honoured GENESIS_RECALL_READ_POOL_SIZE. An
    operator who set it to 1 to limit per-session resource use would otherwise be
    silently RAISED to the new default on upgrade, in every live MCP child at
    once — the opposite of what they asked for, with nothing reporting it.
    """
    from genesis import env
    from genesis.db.connection import DEFAULT_SESSION_READ_POOL_SIZE

    monkeypatch.delenv("GENESIS_SESSION_READ_POOL_SIZE", raising=False)
    monkeypatch.setenv("GENESIS_RECALL_READ_POOL_SIZE", "1")
    assert env.session_read_pool_size() == 1, "legacy constraint silently lifted"

    # The NEW variable wins when both are set — the split still has to work.
    monkeypatch.setenv("GENESIS_SESSION_READ_POOL_SIZE", "6")
    assert env.session_read_pool_size() == 6

    # A malformed PREFERRED value takes the default rather than falling through
    # to the legacy one; a typo must not change which knob is in effect.
    monkeypatch.setenv("GENESIS_SESSION_READ_POOL_SIZE", "oops")
    assert env.session_read_pool_size() == DEFAULT_SESSION_READ_POOL_SIZE


def test_session_pool_knob_reaches_the_mcp_child():
    """A knob the MCP child READS must also be in its env ALLOWLIST, or it is inert.

    `genesis_mcp_server.py` filters the environment it passes to the child through
    `_MCP_VARS`; a variable absent from that list is silently dropped, so a
    documented lever does nothing and nothing reports it. This exact class has
    shipped twice before (the list's own comments cite #1302 and #1587), and this
    PR made it a third time — the new `session_read_pool_size` reader was wired
    without its allowlist entry.

    Asserted against the file text because the allowlist is a module-level literal
    consumed at subprocess-spawn time; importing the module is not needed and would
    drag in the whole MCP stack.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "scripts/genesis_mcp_server.py").read_text()
    assert "session_read_pool_size()" in src, "the child no longer reads the session knob"
    assert '"GENESIS_SESSION_READ_POOL_SIZE"' in src, (
        "GENESIS_SESSION_READ_POOL_SIZE is read by the child but missing from "
        "_MCP_VARS — the documented lever would be INERT"
    )
    assert '"GENESIS_RECALL_READ_POOL_OFF"' in src  # the kill switch it still honours


def test_the_two_pool_constructors_use_different_size_readers():
    """Lock the SPLIT itself, not just the numbers.

    A refactor that points the MCP child back at ``recall_read_pool_size`` would
    silently restore the N-times multiplication while every other test still
    passed, because each reader is individually correct. Assert on the actual
    call sites.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    server = (repo / "src/genesis/runtime/init/memory.py").read_text()
    mcp_child = (repo / "scripts/genesis_mcp_server.py").read_text()

    # The ONE-per-box server takes the host-derived size.
    assert "size=recall_read_pool_size()" in server
    # The PER-SESSION child must not.
    assert "size=session_read_pool_size()" in mcp_child
    assert "recall_read_pool_size()" not in mcp_child


def test_recall_read_pool_size_env(monkeypatch):
    """The size env-reader parses an int and falls back to the default on a
    missing/blank/non-integer value (a bad env value must never crash boot)."""
    from genesis import env
    from genesis.db.connection import DEFAULT_READ_POOL_SIZE

    monkeypatch.delenv("GENESIS_RECALL_READ_POOL_SIZE", raising=False)
    assert env.recall_read_pool_size() == DEFAULT_READ_POOL_SIZE
    monkeypatch.setenv("GENESIS_RECALL_READ_POOL_SIZE", "7")
    assert env.recall_read_pool_size() == 7
    monkeypatch.setenv("GENESIS_RECALL_READ_POOL_SIZE", "not-an-int")
    assert env.recall_read_pool_size() == DEFAULT_READ_POOL_SIZE  # ValueError → default
    monkeypatch.setenv("GENESIS_RECALL_READ_POOL_SIZE", "   ")
    assert env.recall_read_pool_size() == DEFAULT_READ_POOL_SIZE  # blank → default


def test_recall_read_pool_off_env(monkeypatch):
    from genesis import env

    monkeypatch.delenv("GENESIS_RECALL_READ_POOL_OFF", raising=False)
    assert env.recall_read_pool_off() is False
    monkeypatch.setenv("GENESIS_RECALL_READ_POOL_OFF", "1")
    assert env.recall_read_pool_off() is True
    monkeypatch.setenv("GENESIS_RECALL_READ_POOL_OFF", "no")
    assert env.recall_read_pool_off() is False
