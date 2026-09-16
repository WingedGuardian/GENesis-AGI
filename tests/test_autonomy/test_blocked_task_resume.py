"""Blocked-task resume: consume the approval, and match it semantically.

THREE correctness defects on the resume path, all verified against main at
f162b4e13 (2026-09-16). The resume path exists in TWO places —
``dispatch_cycle`` (Path 1b, the 120s poll) and ``recover_incomplete`` (boot
recovery) — and both carry all three, so every test here asserts against both
where it can.

SEVERITY, stated honestly: none of these has FIRED on this install. MEASURED
against a copy of the live database, N=1275 ``approval_requests``: 0 rows carry
a task id at ``$.task_id``, 0 at ``$.extra.task_id``, 0 would have been
mis-claimed. Nothing produces a ``task_unblock`` approval yet, and the nesting
path never requests one either (``step_dispatcher`` passes
``approval_required_for_cli=False``). These are defects in a path whose producer
does not exist — which is exactly why fixing the consumer FIRST is cheap: a
producer landing first would mint rows the buggy consumer never consumes,
making every approval permanently reusable.

1. NO CONSUMPTION. Both scans select an approved-and-unconsumed
   ``approval_requests`` row, re-dispatch the task, and never call
   ``mark_consumed``. The row stays ``approved`` with ``consumed_at IS NULL``
   forever, so a SECOND block on the same task finds the stale approval and
   auto-resumes with no new human decision. ``_dispatch_inflight`` only
   suppresses the re-fire while the first dispatch is live; once it drains, the
   approval is still there. ``mark_consumed`` already exists and is atomic
   (``WHERE status='approved' AND consumed_at IS NULL``), so the winner is
   well-defined — nothing calls it.

2. TEXTUAL MATCHING. Both scans locate the row with
   ``context LIKE '%"task_id": "<id>"%'``, which encodes ``json.dumps``'
   DEFAULT separators (``", "`` / ``": "``). ``request_approval`` takes
   ``context`` as an opaque ``str``, so a producer that serialises compactly
   writes ``{"task_id":"t1"}`` and the scan silently matches nothing — the task
   strands exactly as if no approval had been granted. This branch adds NO
   producer — the first caller to write a ``task_id`` context will be the
   separate producer PR, and the failure mode is silent, which is why the
   contract is pinned here before anything depends on it. The house
   pattern for this very table is semantic, not textual —
   ``CASE WHEN json_valid(context) THEN json_extract(context, '$.task_id') END``
   (see ``db/crud/approval_requests.find_approved_unconsumed``, whose docstring
   records a measured lexicographic-comparison bug from hand-rolling SQL here).

3. CROSS-TYPE CLAIM. A substring match has no notion of nesting, so
   ``%"task_id": "T"%`` also matched an ``autonomous_cli_fallback`` approval
   whose context carries that id at ``$.extra.task_id``
   (``executor/step_dispatcher.py`` builds it, ``approval_gate`` nests it).
   VERIFIED: the substring IS present in that serialisation, so an
   approved-but-unconsumed CLI-fallback approval could release a blocked task
   it was never granted for — and it is the id of the very task most likely to
   block next, since a step whose CLI dispatch returns ``blocked`` goes
   straight into ``_persist_blocker``.

All three are fixed by routing the lookup through one CRUD chokepoint that
matches on ``json_extract`` AND ``action_type``, and consumes before
dispatching.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.dispatcher import TaskDispatcher
from genesis.autonomy.task_unblock_config import TASK_UNBLOCK_ACTION_TYPE
from genesis.db.crud import approval_requests as ar_crud
from genesis.db.crud import task_states


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    from genesis.db.schema import create_all_tables

    await create_all_tables(db)
    await db.commit()
    return db


async def _blocked_task(db, task_id: str) -> None:
    """A task parked in BLOCKED, created through the real intake path.

    ``create_all_tables`` installs the migration-0009 ``enforce_intake_token``
    trigger, so a token is mandatory — this mirrors how a real task is born.
    """
    token = await task_states.create_intake_token(db)
    await task_states.create(
        db,
        task_id=task_id,
        description=f"blocked task {task_id}",
        current_phase="blocked",
        intake_token=token,
    )


async def _approved_resume_request(db, task_id: str, *, compact: bool) -> str:
    """An APPROVED, UNCONSUMED resume approval, via the real ApprovalManager.

    ``compact`` selects the JSON separator style. The default style is what a
    caller gets from a bare ``json.dumps``; the compact style is what a caller
    gets from ``json.dumps(..., separators=(",", ":"))``. ``context`` is an
    opaque string to ``request_approval``, so both are legal producer output.
    """
    payload = {"task_id": task_id, "goal_id": None, "resume_phase": "executing"}
    context = json.dumps(payload, separators=(",", ":")) if compact else json.dumps(payload)
    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type=TASK_UNBLOCK_ACTION_TYPE,
        action_class="reversible",
        description=f"Unblock task {task_id}",
        context=context,
    )
    assert await mgr.resolve(rid, status="approved", resolved_by="telegram:button:1")
    return rid


async def _row(db, rid: str) -> dict:
    cur = await db.execute("SELECT * FROM approval_requests WHERE id = ?", (rid,))
    row = await cur.fetchone()
    assert row is not None, "fixture did not create the approval row"
    return dict(row)


def _dispatcher(db) -> TaskDispatcher:
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=True)
    executor._semaphore_released = set()
    return TaskDispatcher(db=db, executor=executor)


async def _assert_hazard_built(db, rid: str, task_id: str) -> None:
    """Guard-the-guard: the fixture really did build the state under test.

    Without this, a typo in the seed makes every assertion below pass for the
    wrong reason — the approval simply never existed.
    """
    row = await _row(db, rid)
    assert row["status"] == "approved", f"fixture: status is {row['status']!r}"
    assert row["consumed_at"] is None, "fixture: approval was already consumed"
    cur = await db.execute("SELECT current_phase FROM task_states WHERE task_id = ?", (task_id,))
    phase = await cur.fetchone()
    assert phase is not None and phase["current_phase"] == "blocked", "fixture: task is not BLOCKED"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["dispatch_cycle", "recover_incomplete"])
async def test_resume_consumes_the_approval(entry: str):
    """DEFECT 1: resuming must consume the approval, on BOTH entry points.

    Consumption happens BEFORE the task is dispatched, so this assertion holds
    synchronously once the entry point returns — that ordering is the property
    being pinned, not an artefact of the test.
    """
    db = await _db()
    try:
        await _blocked_task(db, "t-consume")
        rid = await _approved_resume_request(db, "t-consume", compact=False)
        await _assert_hazard_built(db, rid, "t-consume")

        disp = _dispatcher(db)
        n = await getattr(disp, entry)()

        row = await _row(db, rid)
        assert row["consumed_at"] is not None, (
            f"{entry}: approval still unconsumed — a second block on this task "
            "would auto-resume on this stale approval with no new human decision"
        )
        # EFFECT, not just the token flip: a regression that claims the approval
        # and never resumes anything would satisfy the assertion above. That is
        # exactly the failure mode consume-before-dispatch newly creates, so it
        # must be pinned here.
        assert n == 1, f"{entry}: claimed the approval but dispatched nothing"
        await asyncio.sleep(0)  # let the tracked_task reach its first await
        disp._executor.execute.assert_awaited_once_with("t-consume")
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["dispatch_cycle", "recover_incomplete"])
async def test_resume_matches_compact_json_context(entry: str):
    """DEFECT 2: the lookup must be semantic, not textual.

    A compactly-serialised context is legal producer output. Under the textual
    LIKE scan it matches nothing and the task strands silently.
    """
    db = await _db()
    try:
        await _blocked_task(db, "t-compact")
        rid = await _approved_resume_request(db, "t-compact", compact=True)
        await _assert_hazard_built(db, rid, "t-compact")

        disp = _dispatcher(db)
        n = await getattr(disp, entry)()

        row = await _row(db, rid)
        assert row["consumed_at"] is not None, (
            f"{entry}: a compactly-serialised context was not matched — the "
            "lookup is coupled to json.dumps separator style"
        )
        assert n == 1, f"{entry}: claimed the approval but dispatched nothing"
        await asyncio.sleep(0)
        disp._executor.execute.assert_awaited_once_with("t-compact")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prefix_sibling_task_approval_is_not_matched():
    """Negative control: the match must be EXACT, not a substring or prefix.

    The tempting bad fix for the compact-JSON trap is to widen the match
    (``LIKE '%<id>%'``). ``t-1`` and ``t-10`` are chosen deliberately: one id is
    a strict PREFIX of the other, so a substring/prefix widening resumes the
    wrong task while every other test in this file still passes. Asserts BOTH
    directions — the decoy survives AND the target is not resumed.
    """
    db = await _db()
    try:
        await _blocked_task(db, "t-1")
        # The approval belongs to t-10, whose id CONTAINS the target's id.
        rid = await _approved_resume_request(db, "t-10", compact=False)
        row = await _row(db, rid)
        assert row["status"] == "approved" and row["consumed_at"] is None, (
            "fixture: the decoy approval is not approved-and-unconsumed"
        )

        disp = _dispatcher(db)
        n = await disp.dispatch_cycle()

        assert (await _row(db, rid))["consumed_at"] is None, (
            "an approval for t-10 was consumed while resuming t-1"
        )
        assert n == 0, "t-1 was resumed on an approval belonging to t-10"
        disp._executor.execute.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cli_fallback_approval_never_unblocks_a_task():
    """DEFECT 3: a DIFFERENT approval type carrying the same task id must not release it.

    ``$.task_id`` is not a free namespace. ``executor/step_dispatcher.py`` builds
    ``context={"task_id": …, "step_idx": …}`` for an autonomous-CLI fallback on a
    task STEP, and ``approval_gate`` nests it under ``$.extra`` — so a row for the
    very task most likely to block next already carries that id.

    MEASURED against main: the textual scan this replaces
    (``context LIKE '%"task_id": "<id>"%'``) DID match that nested context, because
    the substring is present at any nesting depth. An approved-but-unconsumed
    CLI-fallback approval could therefore release a blocked task it was never
    granted for. Two independent properties now prevent it — the match is semantic
    (``$.task_id`` is absent when nested) AND the action_type must agree — so this
    stays correct even if someone later un-nests the context.
    """
    db = await _db()
    try:
        await _blocked_task(db, "t-victim")

        mgr = ApprovalManager(db=db)
        nested = json.dumps(
            {
                "action_type": "autonomous_cli_fallback",
                "subsystem": "executor",
                "extra": {"task_id": "t-victim", "step_idx": 3, "step_type": "implement"},
            }
        )
        # Guard-the-guard: the fixture really does contain the old scan's needle.
        assert '"task_id": "t-victim"' in nested, (
            "fixture no longer reproduces the textual-match hazard"
        )

        rid = await mgr.request_approval(
            action_type="autonomous_cli_fallback",
            action_class="costly_reversible",
            description="CLI fallback for a step of t-victim",
            context=nested,
        )
        assert await mgr.resolve(rid, status="approved", resolved_by="telegram:button:1")

        claimed = await ar_crud.claim_approved_for_task(
            db,
            task_id="t-victim",
            action_type=TASK_UNBLOCK_ACTION_TYPE,
        )
        assert claimed is None, "a CLI-fallback approval was claimed to unblock t-victim"

        # And the end-to-end path agrees: nothing consumed, nothing resumed.
        disp = _dispatcher(db)
        await disp.dispatch_cycle()
        assert (await _row(db, rid))["consumed_at"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_is_atomic_under_concurrent_claimers():
    """Exactly one claimer wins; the loser gets None rather than a second dispatch.

    ``mark_consumed`` is atomic, and claiming routes through it, so a task cannot
    be dispatched twice off one human decision.
    """
    db = await _db()
    try:
        await _blocked_task(db, "t-race")
        rid = await _approved_resume_request(db, "t-race", compact=False)

        first = await ar_crud.claim_approved_for_task(
            db,
            task_id="t-race",
            action_type=TASK_UNBLOCK_ACTION_TYPE,
        )
        second = await ar_crud.claim_approved_for_task(
            db,
            task_id="t-race",
            action_type=TASK_UNBLOCK_ACTION_TYPE,
        )
        assert first == rid, "the first claim did not win the approval"
        assert second is None, "the same approval was claimed twice"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stockpiled_siblings_do_not_buy_extra_resumes():
    """One human answer releases ONE block, even if a producer double-asks.

    ``LIMIT 1`` silently ignores siblings, so N approved-unconsumed rows for one
    task would buy N free auto-resumes — defect 1's failure mode returning by
    the side door. The claim therefore retires older siblings in the same
    breath, and ``ORDER BY resolved_at DESC`` means the NEWEST answer governs
    (an answer to an earlier block must never release a later one).

    Enforced here rather than assumed of a producer that does not exist yet.
    """
    db = await _db()
    try:
        await _blocked_task(db, "t-stock")
        older = await _approved_resume_request(db, "t-stock", compact=False)
        newer = await _approved_resume_request(db, "t-stock", compact=False)
        assert older != newer

        first = await ar_crud.claim_approved_for_task(
            db, task_id="t-stock", action_type=TASK_UNBLOCK_ACTION_TYPE,
        )
        assert first == newer, "the NEWEST answer should govern, not the stalest"

        second = await ar_crud.claim_approved_for_task(
            db, task_id="t-stock", action_type=TASK_UNBLOCK_ACTION_TYPE,
        )
        assert second is None, (
            "a stockpiled sibling bought a second resume with no new human answer"
        )
        assert (await _row(db, older))["consumed_at"] is not None, (
            "the older sibling was left claimable"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unblock_approval_is_not_swept_by_approve_all():
    """"Approve all pending" must never release blocked tasks in bulk.

    ``approve_all_pending``'s exclusion set is a DENYLIST with no allowlist, so
    any UNREGISTERED action_type is swept by default. Registering the type is
    therefore part of minting it, not a follow-up — and this pins it, because
    the omission would be silent and would only surface once a producer exists.
    """
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    db = await _db()
    try:
        mgr = ApprovalManager(db=db)
        rid = await mgr.request_approval(
            action_type=TASK_UNBLOCK_ACTION_TYPE,
            action_class="reversible",
            description="Unblock task t-sweep",
            context=json.dumps({"task_id": "t-sweep"}),
        )

        # POSITIVE CONTROL: an ordinary (unregistered) type MUST be swept.
        # Without it this test also passes when approve_all_pending sweeps
        # nothing at all — e.g. if the pending lookup silently returned [].
        control = await mgr.request_approval(
            action_type="autonomous_cli_fallback",
            action_class="costly_reversible",
            description="ordinary sweepable approval",
            context=json.dumps({"subsystem": "test"}),
        )

        gate = AutonomousCliApprovalGate.__new__(AutonomousCliApprovalGate)
        gate._approval_manager = mgr
        swept = await gate.approve_all_pending(resolved_by="telegram:batch:1")

        assert (await _row(db, control))["status"] == "approved", (
            "control: approve_all_pending swept nothing, so this test proves "
            "nothing about the exclusion"
        )
        assert swept >= 1, "control: approve_all_pending reported no approvals"

        row = await _row(db, rid)
        assert row["status"] == "pending", (
            "an 'Approve all' tap released a blocked task — the unblock type is "
            "missing from approve_all_pending's exclusion set"
        )
    finally:
        await db.close()
