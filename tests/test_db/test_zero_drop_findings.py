"""The zero-drop findings store must count RECURRENCE, not sightings.

The detector's whole value is that "what fell through the cracks?" is answered
by enumeration. That only holds if the same standing condition seen on two
sweeps is the SAME ROW twice — otherwise every commit restarts the counter, no
finding ever reaches the escalation threshold, and an acknowledgement can never
expire because there is nothing stable to hang it on.

So these pin the lifecycle, not the SQL: identity survives a moving tip, an ack
dies with the tip it was granted against, a reappearing finding is a NEW
episode rather than a continuation, and a class the sweep did not complete is
never reconciled at all.
"""

import pytest

from genesis.db.crud import zero_drop as zd

CLS = "unpushed_branch"


async def _sweep(db, present, *, run="r1", now=None, k=3, held=None):
    return await zd.apply_sweep(
        db, class_=CLS, present=present, run_id=run, now=now, escalation_k=k, held=held
    )


def _f(branch="feat/x", tip="aaa111", **over):
    return {"branch": branch, "tip_sha": tip, "ahead_count": 3, **over}


async def test_first_sighting_opens_a_row(db):
    counts = await _sweep(db, [_f()])
    assert counts["new"] == 1
    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert row["status"] == "open"
    assert row["consecutive_runs"] == 1
    assert row["escalated_at"] is None
    assert row["tip_sha"] == "aaa111"


async def test_identity_survives_a_moving_tip(db):
    """The reason identity is (class, branch) and NOT the SHA.

    A SHA-keyed row would be a NEW row on every commit — consecutive_runs
    stuck at 1 forever, so nothing ever escalates and no ack could expire
    (there would be no prior row to compare a tip against).
    """
    await _sweep(db, [_f(tip="aaa111")])
    await _sweep(db, [_f(tip="bbb222")], run="r2")

    rows = await zd.list_findings(db, statuses=("open",))
    assert len(rows) == 1, f"a moved tip forked the identity: {rows}"
    assert rows[0]["consecutive_runs"] == 2
    assert rows[0]["tip_sha"] == "bbb222", "the tip is refreshed evidence"


async def test_escalates_only_at_the_threshold_and_only_once(db):
    for i in range(1, 4):
        await _sweep(db, [_f()], run=f"r{i}", now=f"2026-01-0{i}T00:00:00+00:00", k=3)
        row = await zd.get(db, class_=CLS, branch="feat/x")
        assert row["consecutive_runs"] == i
        assert (row["escalated_at"] is not None) == (i >= 3), f"run {i}"

    stamped = (await zd.get(db, class_=CLS, branch="feat/x"))["escalated_at"]
    await _sweep(db, [_f()], run="r4", now="2026-01-09T00:00:00+00:00", k=3)
    assert (await zd.get(db, class_=CLS, branch="feat/x"))["escalated_at"] == stamped, (
        "escalated_at is the FIRST time it crossed the line, not the latest sweep"
    )


async def test_absent_finding_resolves(db):
    await _sweep(db, [_f()])
    await _sweep(db, [], run="r2")
    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert row["status"] == "resolved"
    assert row["resolved_at"] is not None
    assert await zd.list_findings(db, statuses=("open",)) == []


async def test_reappearing_finding_is_a_new_episode(db):
    """Not a continuation: a resolved-then-back finding restarts at 1.

    Continuing the old count would escalate a just-reappeared branch on sight,
    which is exactly the alarm nobody would trust.
    """
    await _sweep(db, [_f()], run="r1")
    await _sweep(db, [_f()], run="r2")
    await _sweep(db, [], run="r3")
    await _sweep(db, [_f()], run="r4")

    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert row["status"] == "open"
    assert row["consecutive_runs"] == 1, "a new episode starts its own count"
    assert row["reopen_count"] == 1
    assert row["resolved_at"] is None
    assert row["escalated_at"] is None


async def test_ack_suppresses_until_the_branch_moves(db):
    await _sweep(db, [_f(tip="aaa111")])
    acked = await zd.ack(db, class_=CLS, branch="feat/x", reason="backup branch, keeping it")
    assert acked["status"] == "acked"
    assert acked["acked_tip_sha"] == "aaa111", "the ack keys on the tip it was granted at"

    # Same tip: the ack holds, but the CONDITION is still counted.
    await _sweep(db, [_f(tip="aaa111")], run="r2")
    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert row["status"] == "acked"
    assert row["consecutive_runs"] == 2

    # Tip moved: the ack described work that no longer exists → it expires.
    counts = await _sweep(db, [_f(tip="ccc333")], run="r3")
    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert counts["expired_acks"] == 1
    assert row["status"] == "open"
    assert (row["ack_reason"], row["acked_at"], row["acked_tip_sha"]) == (None, None, None), (
        "a lingering ack_reason on an open row reads as 'still suppressed'"
    )


async def test_ack_is_rejected_for_a_resolved_finding(db):
    await _sweep(db, [_f()])
    await _sweep(db, [], run="r2")
    assert await zd.ack(db, class_=CLS, branch="feat/x", reason="n/a") is None
    assert await zd.ack(db, class_="dirty_worktree", branch="nope", reason="n/a") is None


async def test_an_acked_finding_never_escalates(db):
    """Escalation is a call to ACT. A finding somebody already dispositioned
    with a reason must not start shouting on the third sweep."""
    await _sweep(db, [_f()], k=2)
    await zd.ack(db, class_=CLS, branch="feat/x", reason="deliberate")
    await _sweep(db, [_f()], run="r2", k=2)
    await _sweep(db, [_f()], run="r3", k=2)
    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert row["consecutive_runs"] == 3
    assert row["escalated_at"] is None


async def test_classes_are_reconciled_independently(db):
    """A sweep of one class must never resolve another's findings — the
    property the worker relies on to FREEZE a degraded leg while still
    reconciling the legs that completed."""
    await zd.apply_sweep(db, class_="dirty_worktree", present=[_f(branch="w1")], run_id="r1")
    await _sweep(db, [_f(branch="b1")], run="r1")

    await _sweep(db, [], run="r2")  # branch class sweeps clean

    assert (await zd.get(db, class_=CLS, branch="b1"))["status"] == "resolved"
    assert (await zd.get(db, class_="dirty_worktree", branch="w1"))["status"] == "open", (
        "a class the sweep never looked at must be left ALONE"
    )


async def test_nameless_finding_is_dropped_not_invented(db):
    counts = await _sweep(db, [{"tip_sha": "aaa"}, _f()])
    assert counts["new"] == 1
    assert len(await zd.list_findings(db, statuses=("open",))) == 1


async def test_counts_by_status_reports_every_status(db):
    await _sweep(db, [_f(branch="a"), _f(branch="b"), _f(branch="c")])
    await zd.ack(db, class_=CLS, branch="b", reason="ok")
    await _sweep(db, [_f(branch="a"), _f(branch="b")], run="r2")
    assert await zd.counts_by_status(db) == {"open": 1, "acked": 1, "resolved": 1}


async def test_listing_puts_escalated_first(db):
    await _sweep(db, [_f(branch="quiet")], run="r1", k=2)
    await _sweep(db, [_f(branch="quiet"), _f(branch="loud")], run="r2", k=2)
    await _sweep(db, [_f(branch="quiet"), _f(branch="loud")], run="r3", k=2)
    # 'quiet' has 3 runs, 'loud' has 2 — both escalated at k=2, quiet ranks first
    # on run count. Now a fresh, never-escalated finding must rank BELOW both.
    await _sweep(db, [_f(branch="quiet"), _f(branch="loud"), _f(branch="new")], run="r4", k=2)
    order = [r["branch"] for r in await zd.list_findings(db, statuses=("open",))]
    assert order[-1] == "new", f"an un-escalated finding must not outrank one that is: {order}"


@pytest.mark.parametrize("status", ["open", "acked"])
async def test_prune_only_deletes_resolved_rows(db, status):
    """Pruning an ACKED row would silently UN-SUPPRESS it on the next sweep —
    the ack would have to be granted again by whoever happened to notice."""
    await _sweep(db, [_f()], now="2020-01-01T00:00:00+00:00")
    if status == "acked":
        await zd.ack(
            db,
            class_=CLS,
            branch="feat/x",
            reason="old but deliberate",
            now="2020-01-01T00:00:00+00:00",
        )

    deleted = await zd.prune_zero_drop(db, older_than_days=45, now="2026-01-01T00:00:00+00:00")
    assert deleted == 0
    assert await zd.get(db, class_=CLS, branch="feat/x") is not None


async def test_prune_deletes_old_resolved_rows_only(db):
    await _sweep(db, [_f(branch="old"), _f(branch="recent")], now="2020-01-01T00:00:00+00:00")
    await _sweep(db, [], run="r2", now="2020-01-02T00:00:00+00:00")  # both resolve, long ago
    await _sweep(db, [_f(branch="recent")], run="r3", now="2026-01-01T00:00:00+00:00")
    await _sweep(db, [], run="r4", now="2026-01-01T12:00:00+00:00")  # 'recent' resolves today

    deleted = await zd.prune_zero_drop(db, older_than_days=45, now="2026-01-02T00:00:00+00:00")
    assert deleted == 1
    assert await zd.get(db, class_=CLS, branch="old") is None
    assert await zd.get(db, class_=CLS, branch="recent") is not None


# ---------------------------------------------------------------------------
# HELD: absence-from-`present` means three different things, and only one of
# them is "gone". These pin the two that are not.
# ---------------------------------------------------------------------------


async def test_a_held_finding_is_not_resolved(db):
    """A branch the sweep SAW but did not report (an age gate filtered it) is
    still there. Resolving it would restart its episode on the next sweep,
    resetting a recurrence count while nothing about the condition changed."""
    await _sweep(db, [_f()], run="r1")
    await _sweep(db, [_f()], run="r2")

    counts = await _sweep(db, [], run="r3", held={"feat/x"})
    row = await zd.get(db, class_=CLS, branch="feat/x")

    assert counts["held"] == 1 and counts["resolved"] == 0
    assert row["status"] == "open"
    assert row["consecutive_runs"] == 2, "a held run neither advances nor resets the count"
    assert row["reopen_count"] == 0


async def test_a_held_ACKED_finding_keeps_its_acknowledgement(db):
    """MEASURED against the first build: one edit inside an acknowledged
    worktree moved its newest_mtime under the 6h age gate for a single sweep,
    which resolved the row and threw away a written acknowledgement that the
    branch had never invalidated. Ordinary typing must not revoke a judgement."""
    await _sweep(db, [_f(branch="w1")], run="r1")
    await zd.ack(db, class_=CLS, branch="w1", reason="deliberate: long-lived scratch")

    await _sweep(db, [], run="r2", held={"w1"})

    row = await zd.get(db, class_=CLS, branch="w1")
    assert row["status"] == "acked", "a gate flicker destroyed the ack"
    assert row["ack_reason"] == "deliberate: long-lived scratch"
    assert row["acked_tip_sha"] == "aaa111"


async def test_a_genuinely_absent_finding_still_resolves_when_others_are_held(db):
    await _sweep(db, [_f(branch="held"), _f(branch="gone")], run="r1")
    counts = await _sweep(db, [], run="r2", held={"held"})
    assert (counts["held"], counts["resolved"]) == (1, 1)
    assert (await zd.get(db, class_=CLS, branch="held"))["status"] == "open"
    assert (await zd.get(db, class_=CLS, branch="gone"))["status"] == "resolved"


async def test_reopen_clears_the_ack_fields(db):
    """The invariant its sibling path already asserts: a stale ack_reason on an
    open row reads as 'somebody already decided about this'. A reopened row is
    a fresh condition and nobody has decided anything about it yet."""
    await _sweep(db, [_f()], run="r1")
    await zd.ack(db, class_=CLS, branch="feat/x", reason="was deliberate")
    await _sweep(db, [], run="r2")  # genuinely gone -> resolved
    await _sweep(db, [_f()], run="r3")  # back -> reopened

    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert row["status"] == "open"
    assert (row["ack_reason"], row["acked_at"], row["acked_tip_sha"]) == (None, None, None)


async def test_a_duplicate_identity_does_not_abort_the_sweep(db):
    """Two entries for one branch in one sweep hit UNIQUE(class, branch). An
    IntegrityError out of here aborts everything — no heartbeat, no run record,
    no observation — and the only symptom is an overdue pulse two days later.
    First sighting wins; the collision is counted, never silent."""
    counts = await _sweep(db, [_f(tip="aaa111"), _f(tip="bbb222"), _f(branch="other")])

    assert counts["new"] == 2
    assert counts["duplicate_identities"] == 1
    assert (await zd.get(db, class_=CLS, branch="feat/x"))["tip_sha"] == "aaa111"


async def test_a_class_that_FAILS_midway_leaves_no_partial_writes(db, monkeypatch):
    """The docstring promises "no half-applied middle state — one commit at the
    end", and that was true only when nothing raised (Codex P2, PR #1794).

    These are many statements with ONE commit. A failure partway left them
    PENDING on a connection the worker keeps using, so the next class's
    reconcile, the alert write, or the always-runs heartbeat commit would flush
    the failed class's partial updates — while the run record reported that
    class as NOT applied. A frozen class that silently half-applied is the exact
    failure this subsystem exists to prevent, arriving through the caller's own
    error handling.

    The later unrelated write is the load-bearing part of this test: without it
    an open transaction is merely open, and the defect is invisible.
    """
    await _sweep(db, [_f("feat/one"), _f("feat/two")])
    before = {r["branch"]: r["consecutive_runs"] for r in await zd.list_findings(db)}
    assert before == {"feat/one": 1, "feat/two": 1}

    # Fail partway through the SECOND sweep, after the first row's write landed.
    #
    # The seam is `_details_json`, a module-level function called per row inside
    # the loop and BEFORE that row's DML — so raising on the second call leaves
    # row one written and pending, which is the state under test.
    #
    # NOT `db.execute`: the `db` fixture yields a SerializedConnection whose
    # __setattr__ forwards unknown names to the wrapped connection, so patching
    # it puts the wrapper UNDER the proxy while the captured original is the
    # proxy's own method — the proxy takes its lock, calls the wrapper, the
    # wrapper calls the proxy, and it waits forever on a lock it already holds.
    real_details = zd._details_json
    seen = {"n": 0}

    def _flaky(details):
        seen["n"] += 1
        if seen["n"] == 2:
            raise RuntimeError("the store failed midway through a class")
        return real_details(details)

    monkeypatch.setattr(zd, "_details_json", _flaky)
    with pytest.raises(RuntimeError):
        await _sweep(db, [_f("feat/one"), _f("feat/two")], run="r2")
    monkeypatch.undo()
    assert seen["n"] >= 2, "the fixture never reached the failure it was built to inject"

    # The worker goes on to write its heartbeat on this same connection. That
    # commit must not carry the failed class's half-finished work.
    await db.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
    await db.execute("INSERT INTO _probe VALUES (1)")
    await db.commit()

    after = {r["branch"]: r["consecutive_runs"] for r in await zd.list_findings(db)}
    assert after == before, f"a failed class half-applied: {before} -> {after}"


async def test_a_FAILED_unwind_falls_back_to_a_full_rollback(tmp_path):
    """The fix's own recovery path could reproduce the defect it was written for.

    If `ROLLBACK TO` raises, `RELEASE` never runs (one exception exits the whole
    block), so the savepoint stays on the stack and the transaction stays open
    carrying this class's half-finished rows. The caller then reconciles the
    NEXT class on the same connection, whose savepoint is now NESTED — its
    RELEASE commits nothing, and its `db.commit()` issues a real COMMIT that
    flushes BOTH classes while the run record reports this one as not applied.

    Uses a RAW aiosqlite connection deliberately. The shared `db` fixture yields
    a SerializedConnection whose `__setattr__` forwards to the wrapped
    connection while the captured original is the proxy's own method, so
    patching `execute` there re-enters a non-reentrant lock and hangs forever.
    """
    import aiosqlite

    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(str(tmp_path / "zd.db"))
    conn.row_factory = aiosqlite.Row
    try:
        await create_all_tables(conn)
        await conn.commit()
        await zd.apply_sweep(
            conn, class_=CLS, present=[_f("feat/one"), _f("feat/two")], run_id="r1"
        )
        before = {r["branch"]: r["consecutive_runs"] for r in await zd.list_findings(conn)}

        real_execute = conn.execute
        real_details = zd._details_json
        calls = {"details": 0, "rollback_to": 0}

        async def _no_unwind(sql, *a, **kw):
            if sql.strip().upper().startswith("ROLLBACK TO"):
                calls["rollback_to"] += 1
                raise aiosqlite.OperationalError("no such savepoint (simulated)")
            return await real_execute(sql, *a, **kw)

        def _flaky_details(details):
            calls["details"] += 1
            if calls["details"] == 2:
                raise RuntimeError("the store failed midway through a class")
            return real_details(details)

        conn.execute = _no_unwind
        zd._details_json = _flaky_details
        try:
            with pytest.raises(RuntimeError):
                await zd.apply_sweep(
                    conn, class_=CLS, present=[_f("feat/one"), _f("feat/two")], run_id="r2"
                )
        finally:
            conn.execute = real_execute
            zd._details_json = real_details

        assert calls["rollback_to"] == 1, "the fixture never reached the unwind it breaks"

        # The worker continues on this connection and eventually commits.
        await conn.execute("CREATE TABLE _probe (x INTEGER)")
        await conn.execute("INSERT INTO _probe VALUES (1)")
        await conn.commit()

        after = {r["branch"]: r["consecutive_runs"] for r in await zd.list_findings(conn)}
        assert after == before, (
            f"a failed unwind left partial writes for a later commit to flush: "
            f"{before} -> {after}"
        )
    finally:
        await conn.close()


async def test_an_ack_is_REFUSED_when_the_row_moves_between_read_and_write(db, monkeypatch):
    """The ack runs from an MCP tool while a DETACHED sweep may be reconciling
    the same row on another connection. Between the read and the write the sweep
    can advance the tip, and an unconditional `WHERE id = ?` would acknowledge
    that stale row anyway — suppressing work that has CHANGED since the operator
    looked at it, against a tip that is no longer current (Codex P2, PR #1794).

    The window has to be opened deliberately: `ack` resolves `get` at call time,
    so patching it lets the sweep land in exactly the gap the guard covers. An
    earlier version of this test simply acked after the move, which the re-read
    absorbed — it passed with the conditional deleted.
    """
    await _sweep(db, [_f(tip="aaa111")])

    real_get = zd.get
    calls = {"n": 0}

    async def _racing_get(conn, *, class_, branch):
        row = await real_get(conn, class_=class_, branch=branch)
        calls["n"] += 1
        if calls["n"] == 1 and row is not None:
            # The sweep advances the tip after the operator read the board.
            await conn.execute(
                "UPDATE zero_drop_findings SET tip_sha = ? WHERE id = ?",
                ("ccc333", row["id"]),
            )
            await conn.commit()
        return row

    monkeypatch.setattr(zd, "get", _racing_get)
    refused = await zd.ack(db, class_=CLS, branch="feat/x", reason="stale", now=None)
    monkeypatch.undo()

    assert calls["n"] >= 1, "the fixture never opened the window it exists to test"
    assert refused is None, "an ack landed on a row that had moved underneath it"
    # And the row is untouched: not acknowledged, still carrying the NEW tip.
    row = await zd.get(db, class_=CLS, branch="feat/x")
    assert row["status"] == "open"
    assert row["tip_sha"] == "ccc333"
    assert row["ack_reason"] is None


async def test_an_UNRACED_ack_still_lands(db):
    """The control, and the one that keeps the guard from being a wall: the
    ordinary path must still acknowledge, binding the tip it actually read."""
    await _sweep(db, [_f(tip="aaa111")])
    acked = await zd.ack(db, class_=CLS, branch="feat/x", reason="deliberate", now=None)
    assert acked is not None
    assert acked["status"] == "acked"
    assert acked["acked_tip_sha"] == "aaa111"


async def test_an_ack_on_a_row_resolved_underneath_it_is_refused(db):
    """The other direction of the same race, caught one step earlier: resolving
    between read and write would let the ack RESURRECT a resolved condition."""
    await _sweep(db, [_f()])
    await _sweep(db, [], run="r2")  # the condition ended
    assert (await zd.get(db, class_=CLS, branch="feat/x"))["status"] == "resolved"

    assert await zd.ack(db, class_=CLS, branch="feat/x", reason="late", now=None) is None


async def test_a_refused_ack_does_not_DISCARD_another_writers_pending_work(tmp_path):
    """Kimi K3, and it is a defect the ack fix itself introduced an hour earlier.

    Unlike the worker, which owns a short-lived connection, `ack` is reached
    from an MCP tool running on the SERVER'S shared `SerializedConnection`. That
    proxy serializes per METHOD and its own docstring blesses interleaving at
    method boundaries, justified by "commit() flushes all pending work". The
    mirror is not safe: `rollback()` discards all pending work too — including
    another subsystem's uncommitted write.

    A zero-row UPDATE has nothing of its own worth discarding, so committing
    ends the implicit transaction just as well and takes nobody else's writes
    with it.
    """
    import aiosqlite

    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(str(tmp_path / "zd.db"))
    conn.row_factory = aiosqlite.Row
    try:
        await create_all_tables(conn)
        await conn.commit()
        await zd.apply_sweep(
            conn, class_=CLS, present=[_f(tip="aaa111")], run_id="r1"
        )

        # Another subsystem has an uncommitted write in flight on the SHARED
        # connection — exactly the interleaving SerializedConnection permits.
        await conn.execute("CREATE TABLE IF NOT EXISTS _other (x INTEGER)")
        await conn.commit()
        await conn.execute("INSERT INTO _other VALUES (42)")  # deliberately uncommitted

        real_get = zd.get
        calls = {"n": 0}

        async def _racing_get(db, *, class_, branch):
            row = await real_get(db, class_=class_, branch=branch)
            calls["n"] += 1
            if calls["n"] == 1 and row is not None:
                # Deliberately NOT committed. On one connection an uncommitted
                # UPDATE is still visible to later statements, so ack's
                # conditional sees the moved tip — and the other writer's row
                # stays PENDING, which is the state the rollback would destroy.
                # Committing here would flush that row first and the test would
                # pass either way, which is exactly how the first version of it
                # failed to pin anything.
                await db.execute(
                    "UPDATE zero_drop_findings SET tip_sha = ? WHERE id = ?",
                    ("ccc333", row["id"]),
                )
            return row

        zd.get = _racing_get
        try:
            refused = await zd.ack(conn, class_=CLS, branch="feat/x", reason="x", now=None)
        finally:
            zd.get = real_get
        assert refused is None, "the fixture must exercise the zero-row path"

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM _other")
        assert (await cursor.fetchone())["n"] == 1, (
            "the refused ack discarded another writer's pending work"
        )
    finally:
        await conn.close()
