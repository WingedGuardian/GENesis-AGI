"""Tests for the j9_regression resolution handler + its safety wiring.

The handler is a no-op-on-approval (mark executed); the safety wiring (NOTIFY_USER
domain + the never-dispatch blocklist) is what guarantees an approved j9_regression
proposal can never be auto-dispatched as a background session.
"""

import aiosqlite
import pytest

from genesis.autonomy.classification import ACTION_TYPE_DOMAIN_MAP, classify_domain
from genesis.autonomy.types import ActionDomain
from genesis.db.crud import ego as ego_crud
from genesis.db.schema import create_all_tables
from genesis.ego.j9_regression_actions import handle_j9_regression_resolution
from genesis.ego.session import _NEVER_DISPATCH_ACTION_TYPES


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


# ── Safety wiring (the two layers that prevent auto-dispatch) ────────────────

def test_j9_regression_maps_to_notify_user():
    assert ACTION_TYPE_DOMAIN_MAP["j9_regression"] == ActionDomain.NOTIFY_USER
    # And the classifier resolves it (not falling through to EXTERNAL_READ).
    assert classify_domain("j9_regression") == ActionDomain.NOTIFY_USER


def test_j9_regression_blocklisted_from_dispatch():
    assert "j9_regression" in _NEVER_DISPATCH_ACTION_TYPES


# ── Handler behaviour ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_marks_executed_on_approval(db):
    # The resolution flow sets a proposal to 'approved' BEFORE the hooks fire;
    # execute_proposal only transitions an 'approved' proposal → 'executed'.
    await ego_crud.create_proposal(
        db, id="p1", action_type="j9_regression", content="regression x",
        status="approved",
    )
    ok = await handle_j9_regression_resolution(
        db, {"id": "p1", "action_type": "j9_regression"}, "approved",
    )
    assert ok is True
    prop = await ego_crud.get_proposal(db, "p1")
    assert prop["status"] == "executed"  # left the board; sweep can't dispatch it


@pytest.mark.asyncio
async def test_handler_noop_on_other_action_type(db):
    ok = await handle_j9_regression_resolution(
        db, {"id": "p2", "action_type": "goal_status_change"}, "approved",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_handler_noop_on_rejection(db):
    await ego_crud.create_proposal(
        db, id="p3", action_type="j9_regression", content="regression x",
        status="pending",
    )
    ok = await handle_j9_regression_resolution(
        db, {"id": "p3", "action_type": "j9_regression"}, "rejected",
    )
    assert ok is False
    prop = await ego_crud.get_proposal(db, "p3")
    assert prop["status"] == "pending"  # declining changes nothing
