"""Regression tests for ego_proposal_resolve targeting.

Guards the fix for the wrong-batch resolution bug: the resolver used to act
only on the most-recent pending batch, so resolving "proposal 1" (or "all")
could silently hit proposals in a batch the user was not looking at. The fix:

- ``proposal_ids`` resolves exactly the named proposals, batch-independent.
- ``proposal_numbers`` now index the same pending board the user sees (all
  pending, newest first), not a single batch.
- Both paths share one helper so neither skips the post-resolution hooks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.mcp.health import ego_tools

# The MCP tool is a FunctionTool; .fn is the underlying coroutine function.
RESOLVE = ego_tools.ego_proposal_resolve.fn


@pytest.fixture
async def db(tmp_path):
    """File-backed DB with the ego_proposals table (the tool opens its own
    connection to this path via a patched _get_db_path)."""
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        await conn.execute(TABLES["ego_directives"])
        await conn.commit()
        yield conn, db_path


async def _mk(
    conn,
    *,
    id: str,
    content: str,
    batch: str,
    created_at: str,
    action_type: str = "investigate",
    status: str = "pending",
) -> None:
    await ego_crud.create_proposal(
        conn,
        id=id,
        action_type=action_type,
        content=content,
        batch_id=batch,
        created_at=created_at,
        status=status,
    )


async def _status(db_path, pid: str) -> str | None:
    async with aiosqlite.connect(str(db_path)) as c:
        c.row_factory = aiosqlite.Row
        cur = await c.execute(
            "SELECT status FROM ego_proposals WHERE id = ?",
            (pid,),
        )
        row = await cur.fetchone()
        return row["status"] if row else None


def _patch_path(db_path):
    return patch(
        "genesis.mcp.health.ego_tools._get_db_path",
        return_value=db_path,
    )


class TestByIdTargeting:
    async def test_by_id_resolves_exact_proposal_across_batches(self, db):
        """The core regression: an id in an OLDER batch resolves precisely,
        leaving the newer batch's proposals untouched."""
        conn, db_path = db
        await _mk(
            conn,
            id="a1",
            content="older pause proposal",
            batch="batchA",
            created_at="2026-07-14T00:00:00+00:00",
        )
        await _mk(
            conn,
            id="b1",
            content="newer health diag",
            batch="batchB",
            created_at="2026-07-15T00:00:00+00:00",
        )
        await _mk(
            conn,
            id="b2",
            content="newer career dispatch",
            batch="batchB",
            created_at="2026-07-15T01:00:00+00:00",
        )

        with _patch_path(db_path):
            res = await RESOLVE(
                action="reject",
                proposal_ids="a1",
                reason="keep active",
            )

        assert res["status"] == "ok"
        assert res["mode"] == "by_id"
        assert res["resolved"] == 1
        assert res["details"]["a1"] == "rejected"
        assert await _status(db_path, "a1") == "rejected"
        # The newer batch must be completely untouched — the old bug would
        # have hit these instead.
        assert await _status(db_path, "b1") == "pending"
        assert await _status(db_path, "b2") == "pending"

    async def test_by_id_multiple(self, db):
        conn, db_path = db
        await _mk(conn, id="a1", content="one", batch="b", created_at="2026-07-15T00:00:00+00:00")
        await _mk(conn, id="a2", content="two", batch="b", created_at="2026-07-15T01:00:00+00:00")
        await _mk(conn, id="a3", content="three", batch="b", created_at="2026-07-15T02:00:00+00:00")

        with _patch_path(db_path):
            res = await RESOLVE(action="approve", proposal_ids="a1, a3")

        assert res["resolved"] == 2
        assert await _status(db_path, "a1") == "approved"
        assert await _status(db_path, "a3") == "approved"
        assert await _status(db_path, "a2") == "pending"

    async def test_by_id_not_found(self, db):
        conn, db_path = db
        await _mk(conn, id="a1", content="x", batch="b", created_at="2026-07-15T00:00:00+00:00")
        with _patch_path(db_path):
            res = await RESOLVE(action="reject", proposal_ids="nope")
        assert res["details"]["nope"] == "not found"
        assert res["resolved"] == 0
        assert await _status(db_path, "a1") == "pending"

    async def test_by_id_takes_precedence_over_numbers(self, db):
        conn, db_path = db
        await _mk(
            conn, id="a1", content="target", batch="b", created_at="2026-07-15T00:00:00+00:00"
        )
        await _mk(conn, id="a2", content="other", batch="b", created_at="2026-07-15T01:00:00+00:00")
        with _patch_path(db_path):
            # proposal_numbers should be ignored when ids are given
            res = await RESOLVE(
                action="reject",
                proposal_ids="a1",
                proposal_numbers="1,2",
            )
        assert res["mode"] == "by_id"
        assert await _status(db_path, "a1") == "rejected"
        assert await _status(db_path, "a2") == "pending"


class TestBoardAlignedNumbers:
    async def test_numbers_index_full_board_newest_first(self, db):
        """Positional numbers span ALL pending (newest first), not one batch:
        position 3 reaches the oldest proposal in a different batch."""
        conn, db_path = db
        await _mk(
            conn, id="old", content="oldest", batch="batchA", created_at="2026-07-13T00:00:00+00:00"
        )
        await _mk(
            conn, id="mid", content="middle", batch="batchB", created_at="2026-07-14T00:00:00+00:00"
        )
        await _mk(
            conn, id="new", content="newest", batch="batchB", created_at="2026-07-15T00:00:00+00:00"
        )

        with _patch_path(db_path):
            res = await RESOLVE(action="approve", proposal_numbers="3")

        assert res["mode"] == "by_number"
        assert res["board_size"] == 3
        assert res["resolved"] == 1
        # Position 3 == oldest, proving the board spans all pending.
        assert await _status(db_path, "old") == "approved"
        assert await _status(db_path, "new") == "pending"
        assert await _status(db_path, "mid") == "pending"

    async def test_all_resolves_entire_board(self, db):
        conn, db_path = db
        await _mk(
            conn, id="a1", content="a", batch="batchA", created_at="2026-07-14T00:00:00+00:00"
        )
        await _mk(
            conn, id="b1", content="b", batch="batchB", created_at="2026-07-15T00:00:00+00:00"
        )
        with _patch_path(db_path):
            res = await RESOLVE(
                action="reject",
                proposal_numbers="all",
                reason="cleanup",
            )
        assert res["resolved"] == 2
        assert await _status(db_path, "a1") == "rejected"
        assert await _status(db_path, "b1") == "rejected"

    async def test_out_of_range_number(self, db):
        conn, db_path = db
        await _mk(conn, id="a1", content="a", batch="b", created_at="2026-07-15T00:00:00+00:00")
        with _patch_path(db_path):
            res = await RESOLVE(action="approve", proposal_numbers="5")
        assert res["details"]["#5"] == "out of range"
        assert res["resolved"] == 0
        assert await _status(db_path, "a1") == "pending"

    async def test_duplicate_numbers_resolve_once(self, db):
        """A repeated position must not double-run the post-resolution hooks."""
        conn, db_path = db
        await _mk(
            conn,
            id="a1",
            content="only",
            batch="b",
            created_at="2026-07-15T00:00:00+00:00",
            action_type="goal_status_change",
        )
        spy = AsyncMock()
        with (
            _patch_path(db_path),
            patch(
                "genesis.ego.goal_actions.handle_goal_status_change_resolution",
                spy,
            ),
        ):
            res = await RESOLVE(action="approve", proposal_numbers="1,1")
        assert res["resolved"] == 1
        spy.assert_awaited_once()
        assert await _status(db_path, "a1") == "approved"

    async def test_no_pending_returns_error(self, db):
        _conn, db_path = db
        with _patch_path(db_path):
            res = await RESOLVE(action="approve", proposal_numbers="all")
        assert res["status"] == "error"


class TestHookParity:
    """Both targeting paths must run the shared post-resolution hook cascade."""

    async def test_goal_hook_runs_on_id_path(self, db):
        conn, db_path = db
        await _mk(
            conn,
            id="g1",
            content="pause goal X",
            batch="b",
            created_at="2026-07-15T00:00:00+00:00",
            action_type="goal_status_change",
        )
        spy = AsyncMock()
        with (
            _patch_path(db_path),
            patch(
                "genesis.ego.goal_actions.handle_goal_status_change_resolution",
                spy,
            ),
        ):
            res = await RESOLVE(action="approve", proposal_ids="g1")
        assert res["details"]["g1"] == "approved"
        spy.assert_awaited_once()
        args = spy.await_args.args
        assert args[1]["id"] == "g1"
        assert args[2] == "approved"

    async def test_goal_hook_runs_on_number_path(self, db):
        conn, db_path = db
        await _mk(
            conn,
            id="g1",
            content="pause goal X",
            batch="b",
            created_at="2026-07-15T00:00:00+00:00",
            action_type="goal_status_change",
        )
        spy = AsyncMock()
        with (
            _patch_path(db_path),
            patch(
                "genesis.ego.goal_actions.handle_goal_status_change_resolution",
                spy,
            ),
        ):
            await RESOLVE(action="reject", proposal_numbers="1", reason="no")
        spy.assert_awaited_once()
        assert spy.await_args.args[2] == "rejected"


class TestWithdrawnHandling:
    async def test_reject_withdrawn_does_not_create_approved_directive(self, db):
        """Rejecting a withdrawn proposal must not spawn an 'approved
        withdrawn' re-propose directive (action-aware handling)."""
        conn, db_path = db
        await _mk(
            conn,
            id="w1",
            content="withdrawn one",
            batch="b",
            created_at="2026-07-15T00:00:00+00:00",
            status="withdrawn",
        )
        with _patch_path(db_path):
            res = await RESOLVE(action="reject", proposal_ids="w1")
        assert res["details"]["w1"] == "already withdrawn"

    async def test_approve_withdrawn_creates_directive(self, db):
        conn, db_path = db
        await _mk(
            conn,
            id="w1",
            content="withdrawn one",
            batch="b",
            created_at="2026-07-15T00:00:00+00:00",
            status="withdrawn",
        )
        with _patch_path(db_path):
            res = await RESOLVE(action="approve", proposal_ids="w1")
        assert "directive" in res["details"]["w1"]
