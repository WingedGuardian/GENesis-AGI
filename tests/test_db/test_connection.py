"""Tests for SerializedConnection — the concurrency-safe DB proxy."""

import asyncio
import contextlib
import sqlite3

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

async def test_get_raw_db_sets_standard_pragmas(tmp_path, monkeypatch):
    """get_raw_db must apply WAL + NORMAL sync + busy_timeout + Row factory so
    ad-hoc/subprocess opens can't fail immediately on a concurrent write lock."""
    from genesis.db.connection import BUSY_TIMEOUT_MS, get_raw_db

    # Hermetic against the per-process override (e.g. an MCP-child test env or
    # a dev shell exporting it) — this asserts the DEFAULT.
    monkeypatch.delenv("GENESIS_DB_BUSY_TIMEOUT_MS", raising=False)

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


# ── get_db foreign_keys param (WS-15 follow-up: long-lived MCP connections) ──

async def test_get_db_enforces_foreign_keys_by_default(tmp_path):
    """get_db() defaults to FK enforcement — runtime behavior unchanged."""
    from genesis.db.connection import get_db

    db = await get_db(tmp_path / "fk_on.db")
    try:
        cur = await db.execute("PRAGMA foreign_keys")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


async def test_get_db_foreign_keys_opt_out(tmp_path):
    """get_db(foreign_keys=False) leaves FK off — preserves the legacy behavior
    of the long-lived MCP connections, which never enforced FK."""
    from genesis.db.connection import get_db

    db = await get_db(tmp_path / "fk_off.db", foreign_keys=False)
    try:
        cur = await db.execute("PRAGMA foreign_keys")
        assert (await cur.fetchone())[0] == 0
    finally:
        await db.close()


# ── transaction(): multi-statement atomicity (F1) ─────────────────────────


async def test_transaction_commits_on_clean_exit(sconn):
    """Statements inside the block are committed as a unit on clean exit."""
    async with sconn.transaction():
        await sconn.execute("INSERT INTO t VALUES (1, 'a')")
        await sconn.execute("INSERT INTO t VALUES (2, 'b')")
    # visible AFTER the block (single commit at exit)
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 2


async def test_transaction_rolls_back_on_error(sconn):
    """ANY exception in the block rolls the WHOLE transaction back — the first
    insert must not survive when the block later raises."""
    with contextlib.suppress(RuntimeError):
        async with sconn.transaction():
            await sconn.execute("INSERT INTO t VALUES (1, 'a')")
            raise RuntimeError("boom")
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 0  # rolled back
    # connection is not wedged — a later normal write still works
    await sconn.execute("INSERT INTO t VALUES (9, 'ok')")
    await sconn.commit()
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 1


async def test_execute_is_reentrant_inside_transaction(sconn):
    """execute() from the OWNING task inside the block runs on the held lock
    (does not deadlock re-acquiring the non-reentrant lock)."""
    async with sconn.transaction():
        # would hang forever if execute() tried to re-acquire self._lock
        await asyncio.wait_for(
            sconn.execute("INSERT INTO t VALUES (1, 'reentrant')"), timeout=2.0
        )
    cur = await sconn.execute("SELECT val FROM t WHERE id = 1")
    assert (await cur.fetchone())["val"] == "reentrant"


async def test_transaction_holds_lock_across_body(sconn):
    """The core mechanism: while task A is INSIDE an open transaction (paused
    between statements), task B's execute() on the same connection BLOCKS until
    A's transaction commits — no peer can interleave.

    Verify-RED: make _maybe_lock always yield (never take the lock) and B is no
    longer blocked, so 'b-done' appears before A is released and the final order
    assertion fails."""
    order: list[str] = []
    a_inside = asyncio.Event()
    release_a = asyncio.Event()

    async def task_a():
        async with sconn.transaction():
            await sconn.execute("INSERT INTO t VALUES (100, 'a1')")
            order.append("a-mid")
            a_inside.set()
            await release_a.wait()  # hold the transaction open
            await sconn.execute("INSERT INTO t VALUES (101, 'a2')")
            order.append("a-commit")

    async def task_b():
        await a_inside.wait()
        order.append("b-attempt")
        await sconn.execute("INSERT INTO t VALUES (200, 'b')")  # must block on held lock
        order.append("b-done")

    ta = asyncio.create_task(task_a())
    tb = asyncio.create_task(task_b())
    await a_inside.wait()
    await asyncio.sleep(0.05)  # give B every chance to run — it must be blocked
    assert "b-attempt" in order and "b-done" not in order
    release_a.set()
    await asyncio.gather(ta, tb)
    # A's ENTIRE transaction committed before B's write ran
    assert order == ["a-mid", "b-attempt", "a-commit", "b-done"]


async def test_transaction_isolates_peer_from_uncommitted_write(sconn):
    """ACCEPTANCE BAR — a deterministic replay of the exact fork defect F1 fixes.

    A peer coroutine's ROLLBACK must not discard an uncommitted write that
    belongs to another coroutine's atomic unit. Task A opens a transaction and
    writes r1, then pauses (mid read-modify-write). While A is paused a peer B
    issues rollback() on the SAME shared connection. With transaction() holding
    the lock, B's rollback BLOCKS until A commits, so BOTH of A's rows survive.

    Verify-RED: this is the real known-positive. Break the mechanism (make
    transaction() release the lock during the body, i.e. the pre-F1 per-call
    behavior) and B's rollback lands between A's two writes and discards r1 — the
    final count is 1, not 2. Confirmed RED during development against a
    lock-releasing transaction()."""
    a_wrote_r1 = asyncio.Event()
    release_a = asyncio.Event()

    async def task_a():
        async with sconn.transaction():
            await sconn.execute("INSERT INTO t VALUES (1, 'a1')")
            a_wrote_r1.set()
            await release_a.wait()  # pause mid-unit, transaction still open
            await sconn.execute("INSERT INTO t VALUES (2, 'a2')")

    ta = asyncio.create_task(task_a())
    await a_wrote_r1.wait()

    # Peer rollback while A is paused — must block on the held transaction lock.
    tb = asyncio.create_task(sconn.rollback())
    await asyncio.sleep(0.05)
    assert not tb.done()  # B's rollback is blocked, cannot nuke A's uncommitted r1

    release_a.set()
    await asyncio.gather(ta, tb)  # A commits both rows, THEN B's rollback no-ops

    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 2  # both survived — no peer contamination


async def test_transaction_is_not_reentrant(sconn):
    """A nested transaction() on the SAME task raises rather than deadlocking or
    silently sharing the transaction."""
    with pytest.raises(RuntimeError, match="not re-entrant"):
        async with sconn.transaction():
            async with sconn.transaction():
                pass


async def test_transaction_waits_out_concurrent_implicit_txn(sconn):
    """Repro (Codex P1, PR #1576): ordinary CRUD does execute();commit() as TWO
    lock acquisitions, so the connection sits inside an IMPLICIT transaction
    (legacy isolation mode) between them with the lock released. A peer's
    transaction() entering that window used to raise 'cannot start a transaction
    within a transaction' (an error _retry_locked does not retry — it retries
    lock errors only). transaction() must instead wait the implicit transaction
    out and then proceed."""
    # open an implicit transaction the way every CRUD helper does (no commit yet)
    await sconn.execute("INSERT INTO t VALUES (1, 'implicit')")
    assert sconn.in_transaction

    async def peer_txn():
        async with sconn.transaction():
            await sconn.execute("INSERT INTO t VALUES (2, 'txn')")

    tb = asyncio.create_task(peer_txn())
    await asyncio.sleep(0.05)
    # not failed — waiting for the implicit transaction to resolve
    assert not tb.done()
    await sconn.commit()  # the CRUD pair's second half lands
    await asyncio.wait_for(tb, timeout=5.0)  # transaction() proceeds and commits
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 2


async def test_transaction_raises_loud_when_implicit_txn_never_resolves(sconn, monkeypatch):
    """Bounded, not infinite: an implicit transaction a peer opened and never
    commits (errored between execute and commit) can only be closed by a later
    commit/rollback — transaction() gives up LOUDLY once the stall budget is
    spent, instead of blocking every caller forever. The error must name the
    WEDGE hypothesis, which is the true one here (no op completes)."""
    from genesis.db import connection as conn_mod

    slept: list[float] = []

    async def fast_sleep(d):
        slept.append(d)

    monkeypatch.setattr(conn_mod, "_async_sleep", fast_sleep)
    await sconn.execute("INSERT INTO t VALUES (1, 'wedged')")  # never committed
    with pytest.raises(sqlite3.OperationalError, match="never committed/rolled back"):
        async with sconn.transaction():
            pass  # pragma: no cover — must not be reached
    # spent exactly the stall budget (N checks → N-1 backoffs), no more
    assert len(slept) == len(conn_mod._WRITE_RETRY_DELAYS)
    # the lock is RELEASED on the exhaustion path — a leak here would surface as
    # a HANG in a later test rather than a failure, so assert it explicitly
    assert not sconn._lock.locked()
    # the connection is not wedged further: resolving the implicit txn works
    await sconn.commit()
    async with sconn.transaction():
        await sconn.execute("INSERT INTO t VALUES (2, 'after')")


async def test_transaction_wait_is_progress_aware_not_blind(sconn, monkeypatch):
    """A CONTENDED connection is not a wedged one. While peers keep completing
    ops, the stall budget must RESET rather than burn down — a progress-blind
    bound fails transaction() entries while nothing is actually wrong. Here a
    peer holds an implicit transaction open across far more checks than the
    stall budget, but keeps executing, so entry keeps waiting; when the peer
    finally commits, the transaction proceeds."""
    from genesis.db import connection as conn_mod

    ticks = 0

    async def peer_progresses_then_commits(d):
        # Stand in for the backoff sleep: each time the waiter backs off, the
        # peer completes another op (progress), until it finally commits.
        nonlocal ticks
        ticks += 1
        if ticks <= 3 * (len(conn_mod._WRITE_RETRY_DELAYS) + 1):
            await sconn.execute(f"INSERT INTO t VALUES ({100 + ticks}, 'peer')")
        else:
            await sconn.commit()

    monkeypatch.setattr(conn_mod, "_async_sleep", peer_progresses_then_commits)
    await sconn.execute("INSERT INTO t VALUES (1, 'peer-open')")  # implicit txn open
    async with sconn.transaction():  # must NOT raise despite many busy checks
        await sconn.execute("INSERT INTO t VALUES (2, 'mine')")
    # waited out far more checks than a progress-blind budget would have allowed
    assert ticks > len(conn_mod._WRITE_RETRY_DELAYS) + 1
    cur = await sconn.execute("SELECT count(*) as cnt FROM t WHERE val = 'mine'")
    assert (await cur.fetchone())["cnt"] == 1


async def test_transaction_contention_bound_is_the_busy_timeout(sconn, monkeypatch):
    """Contention still has an outer bound (a permanently-saturated connection
    must not starve the waiter forever), and it is the connection's configured
    busy_timeout — not an invented number. When peers keep progressing past
    that budget, the error names CONTENTION, not a wedge."""
    from genesis.db import connection as conn_mod

    clock = {"t": 0.0}

    async def progress_and_advance_clock(d):
        # peer completes an op every backoff (so the stall budget never fires)
        await sconn.execute("INSERT INTO t VALUES (NULL, 'peer')")
        clock["t"] += 10.0  # blow past any plausible busy_timeout budget

    monkeypatch.setattr(conn_mod, "_async_sleep", progress_and_advance_clock)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", lambda: clock["t"])
    await sconn.execute("INSERT INTO t VALUES (1, 'peer-open')")  # implicit txn open
    with pytest.raises(sqlite3.OperationalError, match="contention, not a wedge"):
        async with sconn.transaction():
            pass  # pragma: no cover — must not be reached
    assert not sconn._lock.locked()  # released on this exhaustion path too


async def test_txn_control_sql_refused_inside_transaction(sconn):
    """Raw transaction-control SQL through execute()/executemany() inside an
    owned transaction() block bypassed _refuse_inside_txn — an early COMMIT
    breaks the all-or-nothing unit (a later exception can't roll back what was
    already committed), and a raw ROLLBACK discards the unit while the context
    manager still reports success. Refuse the verbs, same as commit()/rollback()."""
    async with sconn.transaction():
        await sconn.execute("INSERT INTO t VALUES (1, 'x')")
        for bad in (
            "COMMIT",
            "commit",
            "  ROLLBACK",
            "BEGIN IMMEDIATE",
            "END",
            "-- sneaky\nCOMMIT",
            "/* c */ COMMIT",
            "SAVEPOINT sp1",
            "RELEASE sp1",
        ):
            with pytest.raises(RuntimeError, match="not allowed inside transaction"):
                await sconn.execute(bad)
        with pytest.raises(RuntimeError, match="not allowed inside transaction"):
            await sconn.executemany("COMMIT", [()])
    # the refused statements didn't break the unit — it committed intact
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 1


async def test_txn_control_sql_allowed_outside_transaction(sconn):
    """Control: OUTSIDE a transaction() block the refusal must not fire — the
    migration runner and precompact legitimately issue BEGIN IMMEDIATE/COMMIT/
    ROLLBACK through execute() on connections with no owned transaction."""
    await sconn.execute("BEGIN IMMEDIATE")
    await sconn.execute("INSERT INTO t VALUES (1, 'manual')")
    await sconn.execute("COMMIT")
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 1


async def test_commit_rollback_close_refused_inside_transaction(sconn):
    """commit/rollback/close/executescript are refused inside the block — the
    context manager owns the single commit/rollback, so a body cannot silently
    early-commit or discard the transaction. (F-B)"""
    async with sconn.transaction():
        await sconn.execute("INSERT INTO t VALUES (1, 'x')")
        for bad in ("commit", "rollback", "close"):
            with pytest.raises(RuntimeError, match="not allowed inside transaction"):
                await getattr(sconn, bad)()
        with pytest.raises(RuntimeError, match="not allowed inside transaction"):
            await sconn.executescript("SELECT 1")
    # the block still committed normally despite the refused calls
    cur = await sconn.execute("SELECT count(*) as cnt FROM t")
    assert (await cur.fetchone())["cnt"] == 1
