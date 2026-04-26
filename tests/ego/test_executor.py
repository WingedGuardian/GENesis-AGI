"""Tests for genesis.ego.executor — EgoProposalExecutor."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import follow_ups as follow_up_crud
from genesis.db.schema import TABLES
from genesis.ego.executor import EgoProposalExecutor

# Patch GenesisRuntime.instance() → None so _on_tick never skips due to
# pause state.  The import lives inside a try/except so the module must
# exist, but instance() returning None is the neutral path.
_RUNTIME_PATCH = patch(
    "genesis.runtime.GenesisRuntime.instance", return_value=None,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory DB with executor-relevant tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for table in ("ego_cycles", "ego_proposals", "ego_state", "follow_ups"):
            await conn.execute(TABLES[table])
        yield conn


@pytest.fixture
def mock_runner():
    """Mock DirectSessionRunner with spawn returning a session ID."""
    runner = AsyncMock()
    runner.spawn = AsyncMock(return_value="sess-123")
    return runner


@pytest.fixture
def mock_surplus_queue():
    """Mock SurplusQueue with enqueue returning a task ID."""
    sq = AsyncMock()
    sq.enqueue = AsyncMock(return_value="task-456")
    return sq


@pytest.fixture
def mock_outreach_pipeline():
    """Mock OutreachPipeline with submit returning a result with outreach_id."""
    op = AsyncMock()
    result = AsyncMock()
    result.outreach_id = "outreach-789"
    op.submit = AsyncMock(return_value=result)
    return op


@pytest.fixture
def executor(db, mock_runner, mock_surplus_queue, mock_outreach_pipeline):
    """Fully-wired executor with all dependencies mocked."""
    return EgoProposalExecutor(
        db=db,
        direct_session_runner=mock_runner,
        surplus_queue=mock_surplus_queue,
        outreach_pipeline=mock_outreach_pipeline,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_proposal(
    db: aiosqlite.Connection,
    *,
    id: str = "p1",
    action_type: str = "investigate",
    status: str = "approved",
    content: str = "Check backlog growth",
    rationale: str = "Backlog is growing",
    urgency: str = "normal",
) -> str:
    """Insert a proposal and return its id."""
    return await ego_crud.create_proposal(
        db,
        id=id,
        action_type=action_type,
        content=content,
        rationale=rationale,
        urgency=urgency,
        status=status,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecutorTick:
    """Tests for _on_tick and dispatch behaviour."""

    @pytest.mark.asyncio
    async def test_tick_no_approved(self, executor, db):
        """Empty DB — tick does nothing, no errors."""
        # Patch out GenesisRuntime import
        with _RUNTIME_PATCH:
            await executor._on_tick()

        # No proposals processed, no follow-ups created
        pending = await follow_up_crud.get_pending(db)
        assert pending == []

    @pytest.mark.asyncio
    async def test_dispatch_investigate(self, executor, db, mock_runner):
        """investigate → spawns session with profile='observe'."""
        await _seed_proposal(db, id="p-inv", action_type="investigate")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        mock_runner.spawn.assert_called_once()
        request = mock_runner.spawn.call_args[0][0]
        assert request.profile == "observe"

        proposal = await ego_crud.get_proposal(db, "p-inv")
        assert proposal["status"] == "executed"
        assert proposal["user_response"] == "session:sess-123"

    @pytest.mark.asyncio
    async def test_dispatch_dispatch_type(self, executor, db, mock_runner):
        """dispatch → spawns session with profile='research'."""
        await _seed_proposal(db, id="p-disp", action_type="dispatch")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        mock_runner.spawn.assert_called_once()
        request = mock_runner.spawn.call_args[0][0]
        assert request.profile == "research"

        proposal = await ego_crud.get_proposal(db, "p-disp")
        assert proposal["status"] == "executed"
        assert proposal["user_response"] == "session:sess-123"

    @pytest.mark.asyncio
    async def test_dispatch_maintenance_surplus(
        self, executor, db, mock_surplus_queue,
    ):
        """maintenance with surplus_queue → enqueues task."""
        await _seed_proposal(db, id="p-maint", action_type="maintenance")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        mock_surplus_queue.enqueue.assert_called_once()

        proposal = await ego_crud.get_proposal(db, "p-maint")
        assert proposal["status"] == "executed"
        assert proposal["user_response"] == "surplus:task-456"

    @pytest.mark.asyncio
    async def test_dispatch_maintenance_fallback(self, db, mock_runner):
        """maintenance with surplus_queue=None → creates follow-up instead."""
        exec_no_surplus = EgoProposalExecutor(
            db=db,
            direct_session_runner=mock_runner,
            surplus_queue=None,
            outreach_pipeline=None,
        )
        await _seed_proposal(db, id="p-mfb", action_type="maintenance")

        with _RUNTIME_PATCH:
            await exec_no_surplus._on_tick()

        proposal = await ego_crud.get_proposal(db, "p-mfb")
        assert proposal["status"] == "executed"
        assert proposal["user_response"].startswith("follow_up:")

        # A follow-up should exist from the fallback path (plus the
        # tracking follow-up created in _dispatch_one).
        pending = await follow_up_crud.get_pending(db)
        assert len(pending) >= 1

    @pytest.mark.asyncio
    async def test_dispatch_outreach(
        self, executor, db, mock_outreach_pipeline,
    ):
        """outreach → calls outreach pipeline submit."""
        await _seed_proposal(db, id="p-out", action_type="outreach")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        mock_outreach_pipeline.submit.assert_called_once()

        proposal = await ego_crud.get_proposal(db, "p-out")
        assert proposal["status"] == "executed"
        assert proposal["user_response"] == "outreach:outreach-789"

    @pytest.mark.asyncio
    async def test_dispatch_config(self, executor, db):
        """config → creates follow-up with strategy='user_input_needed'."""
        await _seed_proposal(db, id="p-cfg", action_type="config")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        proposal = await ego_crud.get_proposal(db, "p-cfg")
        assert proposal["status"] == "executed"
        assert proposal["user_response"].startswith("follow_up:")

        pending = await follow_up_crud.get_pending(db)
        strategies = [fu["strategy"] for fu in pending]
        assert "user_input_needed" in strategies

    @pytest.mark.asyncio
    async def test_dispatch_unknown_type(self, executor, db):
        """Unknown action_type → creates follow-up."""
        await _seed_proposal(db, id="p-unk", action_type="foobar")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        proposal = await ego_crud.get_proposal(db, "p-unk")
        assert proposal["status"] == "executed"
        assert proposal["user_response"].startswith("follow_up:")

        pending = await follow_up_crud.get_pending(db)
        contents = " ".join(fu["content"] for fu in pending)
        assert "foobar" in contents

    @pytest.mark.asyncio
    async def test_max_per_tick(self, executor, db, mock_runner):
        """Only MAX_PER_TICK (3) proposals processed per tick."""
        for i in range(5):
            await _seed_proposal(db, id=f"p-batch-{i}", action_type="investigate")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        assert mock_runner.spawn.call_count == 3

        # 3 executed, 2 still approved
        executed = await ego_crud.list_proposals(db, status="executed")
        still_approved = await ego_crud.list_proposals(db, status="approved")
        assert len(executed) == 3
        assert len(still_approved) == 2


class TestExecutorErrorHandling:
    """Tests for error paths and failure marking."""

    @pytest.mark.asyncio
    async def test_error_marks_failed(self, executor, db, mock_runner):
        """runner.spawn raises → proposal marked 'failed'."""
        mock_runner.spawn.side_effect = RuntimeError("session unavailable")
        await _seed_proposal(db, id="p-err", action_type="investigate")

        with _RUNTIME_PATCH:
            await executor._on_tick()

        proposal = await ego_crud.get_proposal(db, "p-err")
        assert proposal["status"] == "failed"
        assert "RuntimeError" in proposal["user_response"]

    @pytest.mark.asyncio
    async def test_error_isolation(self, executor, db, mock_runner):
        """First proposal fails, second still executes."""
        # list_proposals returns newest first (ORDER BY created_at DESC),
        # so give p-fail a later timestamp so it's dispatched first.
        await ego_crud.create_proposal(
            db, id="p-fail", action_type="investigate",
            content="will fail", status="approved",
            created_at="2026-01-01T00:00:02",
        )
        await ego_crud.create_proposal(
            db, id="p-ok", action_type="investigate",
            content="will succeed", status="approved",
            created_at="2026-01-01T00:00:01",
        )

        call_count = 0

        async def side_effect_fn(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first fails")
            return "sess-ok"

        mock_runner.spawn.side_effect = side_effect_fn

        with _RUNTIME_PATCH:
            await executor._on_tick()

        p_fail = await ego_crud.get_proposal(db, "p-fail")
        p_ok = await ego_crud.get_proposal(db, "p-ok")

        assert p_fail["status"] == "failed"
        assert p_ok["status"] == "executed"
        assert p_ok["user_response"] == "session:sess-ok"

    @pytest.mark.asyncio
    async def test_no_runner_investigate_fails(self, db):
        """runner=None → investigate proposal marked 'failed' with RuntimeError."""
        exec_no_runner = EgoProposalExecutor(
            db=db,
            direct_session_runner=None,
            surplus_queue=None,
            outreach_pipeline=None,
        )
        await _seed_proposal(db, id="p-no-runner", action_type="investigate")

        with _RUNTIME_PATCH:
            await exec_no_runner._on_tick()

        proposal = await ego_crud.get_proposal(db, "p-no-runner")
        assert proposal["status"] == "failed"
        assert "RuntimeError" in proposal["user_response"]
        assert "not available" in proposal["user_response"]


class TestExecutorLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_stop(self, executor):
        """Verify scheduler lifecycle flags."""
        assert not executor.is_running

        await executor.start()
        assert executor.is_running

        await executor.stop()
        assert not executor.is_running


class TestExecuteProposalCrud:
    """Direct tests for the execute_proposal CRUD function."""

    @pytest.mark.asyncio
    async def test_transitions_approved_to_executed(self, db):
        """Approved proposal transitions to executed."""
        await _seed_proposal(db, id="crud-1", status="approved")

        result = await ego_crud.execute_proposal(
            db, "crud-1", status="executed", user_response="session:s1",
        )
        assert result is True

        proposal = await ego_crud.get_proposal(db, "crud-1")
        assert proposal["status"] == "executed"
        assert proposal["user_response"] == "session:s1"
        assert proposal["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_transitions_approved_to_failed(self, db):
        """Approved proposal transitions to failed."""
        await _seed_proposal(db, id="crud-2", status="approved")

        result = await ego_crud.execute_proposal(
            db, "crud-2", status="failed", user_response="RuntimeError: boom",
        )
        assert result is True

        proposal = await ego_crud.get_proposal(db, "crud-2")
        assert proposal["status"] == "failed"

    @pytest.mark.asyncio
    async def test_does_not_affect_pending(self, db):
        """execute_proposal does NOT transition pending proposals."""
        await _seed_proposal(db, id="crud-3", status="pending")

        result = await ego_crud.execute_proposal(
            db, "crud-3", status="executed", user_response="session:s2",
        )
        assert result is False

        proposal = await ego_crud.get_proposal(db, "crud-3")
        assert proposal["status"] == "pending"

    @pytest.mark.asyncio
    async def test_rejects_invalid_status(self, db):
        """execute_proposal raises ValueError for invalid target status."""
        await _seed_proposal(db, id="crud-4", status="approved")

        with pytest.raises(ValueError, match="executed.*failed"):
            await ego_crud.execute_proposal(
                db, "crud-4", status="approved",
            )

    @pytest.mark.asyncio
    async def test_nonexistent_proposal(self, db):
        """execute_proposal returns False for nonexistent id."""
        result = await ego_crud.execute_proposal(
            db, "does-not-exist", status="executed",
        )
        assert result is False
