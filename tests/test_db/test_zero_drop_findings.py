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
