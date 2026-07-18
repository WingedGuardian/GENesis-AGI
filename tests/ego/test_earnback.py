"""Tests for autonomy earn-back (#18): the shared resolution helper and the
cadence detection/anti-spam path."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.crud import autonomy as autonomy_crud
from genesis.db.crud import ego as ego_crud
from genesis.db.schema import create_all_tables
from genesis.ego.cadence import EgoCadenceManager
from genesis.ego.earnback import (
    _parse_target_level,
    _regression_after,
    handle_earnback_resolution,
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _make_earnback_proposal(db, *, category="direct_session", target=4, status="approved"):
    pid = f"eb_{category}"
    await ego_crud.create_proposal(
        db,
        id=pid,
        action_type="autonomy_earnback",
        action_category=category,
        content=f"Restore {category} to L{target}",
        status="pending",
        created_at="2026-06-20T00:00:00+00:00",
        expected_outputs=json.dumps({"target_level": target}),
    )
    await ego_crud.resolve_proposal(db, pid, status=status)
    return await ego_crud.get_proposal(db, pid)


# ── _parse_target_level ──────────────────────────────────────────────────


def test_parse_target_level():
    assert _parse_target_level('{"target_level": 4}') == 4
    assert _parse_target_level({"target_level": 3}) == 3
    assert _parse_target_level("not json") is None
    assert _parse_target_level(None) is None
    assert _parse_target_level('{"other": 1}') is None


def test_regression_after():
    # regression strictly postdates proposal
    assert _regression_after("2026-06-21T00:00:00+00:00", "2026-06-20T00:00:00+00:00") is True
    # regression predates proposal
    assert _regression_after("2026-06-20T00:00:00+00:00", "2026-06-21T00:00:00+00:00") is False
    # missing values
    assert _regression_after(None, "2026-06-20T00:00:00+00:00") is False
    assert _regression_after("2026-06-20T00:00:00+00:00", None) is False
    # unparseable → False (don't block a user-approved promotion on a parse error)
    assert _regression_after("garbage", "2026-06-20T00:00:00+00:00") is False
    # naive timestamp on one side is coerced to UTC, comparison still works
    assert _regression_after("2026-06-21T00:00:00", "2026-06-20T00:00:00+00:00") is True


# ── handle_earnback_resolution ───────────────────────────────────────────


async def test_approved_promotes_and_marks_executed(db):
    prop = await _make_earnback_proposal(db, category="direct_session", target=4)
    mgr = AsyncMock()
    mgr.promote = AsyncMock(return_value=True)

    applied = await handle_earnback_resolution(db, prop, "approved", mgr)

    assert applied is True
    mgr.promote.assert_awaited_once_with(
        "direct_session", 4, reason="user_approved_earnback",
    )
    row = await ego_crud.get_proposal(db, prop["id"])
    assert row["status"] == "executed"


async def test_rejected_sets_cooldown_no_promote(db):
    prop = await _make_earnback_proposal(db, category="direct_session", status="rejected")
    mgr = AsyncMock()
    mgr.promote = AsyncMock(return_value=True)

    applied = await handle_earnback_resolution(db, prop, "rejected", mgr)

    assert applied is False
    mgr.promote.assert_not_awaited()
    assert await ego_crud.get_state(db, "earnback_reject:direct_session")


async def test_non_earnback_proposal_is_noop(db):
    await ego_crud.create_proposal(
        db, id="p_inv", action_type="investigate", content="x", status="pending",
    )
    await ego_crud.resolve_proposal(db, "p_inv", status="approved")
    prop = await ego_crud.get_proposal(db, "p_inv")
    mgr = AsyncMock()
    mgr.promote = AsyncMock(return_value=True)

    applied = await handle_earnback_resolution(db, prop, "approved", mgr)

    assert applied is False
    mgr.promote.assert_not_awaited()


async def test_skips_promote_when_regression_postdates_proposal(db):
    await autonomy_crud.create(
        db, id="a1", category="direct_session",
        current_level=3, earned_level=4, updated_at="2026-06-20T00:00:00+00:00",
    )
    # Regression AFTER the proposal's created_at (2026-06-20T00:00:00).
    await db.execute(
        "UPDATE autonomy_state SET last_regression_at=? WHERE id=?",
        ("2026-06-21T00:00:00+00:00", "a1"),
    )
    await db.commit()
    prop = await _make_earnback_proposal(db, category="direct_session", target=4)
    mgr = AsyncMock()
    mgr.promote = AsyncMock(return_value=True)

    applied = await handle_earnback_resolution(db, prop, "approved", mgr)

    assert applied is False
    mgr.promote.assert_not_awaited()
    # Still marked executed so the dispatch sweep never picks it up.
    row = await ego_crud.get_proposal(db, prop["id"])
    assert row["status"] == "executed"


async def test_none_manager_does_not_crash(db):
    prop = await _make_earnback_proposal(db, category="direct_session")
    applied = await handle_earnback_resolution(db, prop, "approved", None)
    assert applied is False


async def test_promote_declined_still_marks_executed(db):
    """If promote() declines (e.g. already at target), the proposal must not
    linger as 'approved' — it's marked executed so it can't block re-proposing."""
    prop = await _make_earnback_proposal(db, category="direct_session", target=4)
    mgr = AsyncMock()
    mgr.promote = AsyncMock(return_value=False)

    applied = await handle_earnback_resolution(db, prop, "approved", mgr)

    assert applied is False
    row = await ego_crud.get_proposal(db, prop["id"])
    assert row["status"] == "executed"


# ── cadence _check_earnback_opportunities ────────────────────────────────


_CANDIDATE = {
    "category": "direct_session", "current_level": 3, "target_level": 4,
    "total_successes": 50, "total_corrections": 2, "posterior": 0.94,
    "last_regression_at": "2026-05-25T00:00:00+00:00",
}


def _make_cadence(db, *, source_tag="user_ego_cycle", autonomy_manager=None):
    session = MagicMock()
    session._source_tag = source_tag
    session._db = db
    session._proposals = AsyncMock()
    session._proposals.create_batch = AsyncMock(return_value=("batch1", ["pid1"], []))
    session._proposals.send_digest = AsyncMock(return_value="delivery1")
    config = MagicMock()
    config.cadence_minutes = 30
    cadence = EgoCadenceManager(
        session=session, config=config, db=db,
        event_bus=None, autonomy_manager=autonomy_manager,
    )
    return cadence, session


async def test_cadence_creates_earnback_proposal(db):
    mgr = AsyncMock()
    mgr.detect_earnback_candidates = AsyncMock(return_value=[dict(_CANDIDATE)])
    cadence, session = _make_cadence(db, autonomy_manager=mgr)

    await cadence._check_earnback_opportunities()

    session._proposals.create_batch.assert_awaited_once()
    proposals = session._proposals.create_batch.call_args.args[0]
    assert proposals[0]["action_type"] == "autonomy_earnback"
    assert proposals[0]["action_category"] == "direct_session"
    assert proposals[0]["expected_outputs"] == {"target_level": 4}
    session._proposals.send_digest.assert_awaited_once()


async def test_cadence_genesis_ego_is_noop(db):
    mgr = AsyncMock()
    mgr.detect_earnback_candidates = AsyncMock(return_value=[dict(_CANDIDATE)])
    cadence, session = _make_cadence(db, source_tag="genesis_ego_cycle", autonomy_manager=mgr)

    await cadence._check_earnback_opportunities()

    mgr.detect_earnback_candidates.assert_not_awaited()
    session._proposals.create_batch.assert_not_awaited()


async def test_cadence_skips_when_pending_exists(db):
    await ego_crud.create_proposal(
        db, id="pend1", action_type="autonomy_earnback",
        action_category="direct_session", content="already pending",
        status="pending", created_at="2026-06-20T00:00:00+00:00",
    )
    mgr = AsyncMock()
    mgr.detect_earnback_candidates = AsyncMock(return_value=[dict(_CANDIDATE)])
    cadence, session = _make_cadence(db, autonomy_manager=mgr)

    await cadence._check_earnback_opportunities()

    session._proposals.create_batch.assert_not_awaited()


# ── end-to-end through the real resolution path ──────────────────────────


async def test_e2e_earnback_promotes_through_resolution(db):
    """detect → proposal → approve via the real Telegram resolution path → real
    AutonomyManager.promote → level restored. Runs entirely against an in-memory
    test DB; no live autonomy state is touched.
    """
    from genesis.autonomy.state_machine import AutonomyManager
    from genesis.ego.proposals import ProposalWorkflow

    # Demoted-but-recovered: current L2, earned L4, evidence (50S/2C → 0.94) → L4.
    await autonomy_crud.create(
        db, id="ds", category="direct_session",
        current_level=2, earned_level=4, updated_at="2026-06-20T00:00:00+00:00",
    )
    await db.execute(
        "UPDATE autonomy_state SET total_successes=50, total_corrections=2 WHERE id='ds'",
    )
    # Windowed gate (migration 0067): eligibility reads recent autonomy_events,
    # not lifetime counters — seed matching in-window evidence.
    occurred = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    for i, kind in enumerate(["success"] * 50 + ["correction"] * 2):
        await db.execute(
            "INSERT INTO autonomy_events (id, category, kind, occurred_at) "
            "VALUES (?, 'direct_session', ?, ?)",
            (f"ev-{i}", kind, occurred),
        )
    await db.commit()

    mgr = AutonomyManager(db=db)
    workflow = ProposalWorkflow(db=db, autonomy_manager=mgr)

    candidates = await mgr.detect_earnback_candidates()
    assert len(candidates) == 1
    cand = candidates[0]
    proposal = {
        "action_type": "autonomy_earnback",
        "action_category": cand["category"],
        "content": f"Restore {cand['category']} to L{cand['target_level']}",
        "rationale": "evidence recovered enough to support the earned level",
        "confidence": 0.9,
        "urgency": "low",
        "expected_outputs": {"target_level": cand["target_level"]},
    }
    batch_id, ids, _ = await workflow.create_batch(
        [proposal], ego_source="user_ego_cycle",
    )
    assert len(ids) == 1

    # Approve via the real resolution path (the Telegram entry point).
    await workflow.resolve_proposals(batch_id, {1: ("approved", None)})

    # The real promote fired: current_level restored to the earned level...
    state = await mgr.get_state("direct_session")
    assert int(state.current_level) == 4
    # ...and the proposal was marked executed (never dispatched as a session).
    row = await ego_crud.get_proposal(db, ids[0])
    assert row["status"] == "executed"


async def test_cadence_skips_on_cooldown(db):
    await ego_crud.set_state(
        db, key="earnback_reject:direct_session", value=datetime.now(UTC).isoformat(),
    )
    mgr = AsyncMock()
    mgr.detect_earnback_candidates = AsyncMock(return_value=[dict(_CANDIDATE)])
    cadence, session = _make_cadence(db, autonomy_manager=mgr)

    await cadence._check_earnback_opportunities()

    session._proposals.create_batch.assert_not_awaited()
