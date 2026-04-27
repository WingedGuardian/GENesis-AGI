"""Tests for genesis.autonomy.executor.step_dispatcher.StepDispatcher.

Focuses on the approval-gate pre-check logic in ``dispatch_step`` —
specifically the interaction between ``find_site_pending`` and
``find_recently_approved`` that prevents approved requests from being
permanently blocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchDecision
from genesis.autonomy.executor.step_dispatcher import StepDispatcher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(idx: int = 0, step_type: str = "code") -> dict:
    return {
        "idx": idx,
        "type": step_type,
        "description": "test step",
    }


def _make_dispatcher(
    *,
    autonomous_dispatcher: AsyncMock | None = None,
) -> StepDispatcher:
    return StepDispatcher(
        db=AsyncMock(),
        invoker=AsyncMock(),
        autonomous_dispatcher=autonomous_dispatcher,
    )


def _mock_autonomous_dispatcher(
    *,
    pending: dict | None = None,
    approved: dict | None = None,
    route_decision: AutonomousDispatchDecision | None = None,
    find_pending_exc: Exception | None = None,
    find_approved_exc: Exception | None = None,
) -> AsyncMock:
    """Build a mock AutonomousDispatchRouter with configurable gate behavior."""
    gate = AsyncMock()
    if find_pending_exc:
        gate.find_site_pending = AsyncMock(side_effect=find_pending_exc)
    else:
        gate.find_site_pending = AsyncMock(return_value=pending)

    if find_approved_exc:
        gate.find_recently_approved = AsyncMock(side_effect=find_approved_exc)
    else:
        gate.find_recently_approved = AsyncMock(return_value=approved)

    router = AsyncMock()
    router.approval_gate = gate
    if route_decision is not None:
        router.route = AsyncMock(return_value=route_decision)
    else:
        router.route = AsyncMock(
            return_value=AutonomousDispatchDecision(
                mode="cli_approved",
                reason="approved for CLI",
            ),
        )
    return router


# ---------------------------------------------------------------------------
# Approval-gate pre-check tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestApprovalGatePreCheck:
    """Test the pre-check logic that gates step dispatch on approval state."""

    async def test_pending_no_approval_blocks(self) -> None:
        """When there is a pending request and no approval, step is blocked."""
        pending_row = {"id": "req-1", "status": "pending"}
        ad = _mock_autonomous_dispatcher(pending=pending_row, approved=None)
        sd = _make_dispatcher(autonomous_dispatcher=ad)

        result = await sd.dispatch_step("t-1", _make_step(), [])

        assert result.status == "blocked"
        assert "awaiting approval req-1" in result.result
        ad.approval_gate.find_site_pending.assert_awaited_once()
        ad.approval_gate.find_recently_approved.assert_awaited_once()
        # route() should NOT have been called — we blocked early.
        ad.route.assert_not_awaited()

    async def test_pending_with_approval_falls_through_to_route(self) -> None:
        """When pending exists but approval is granted, fall through to route()."""
        pending_row = {"id": "req-1", "status": "pending"}
        approved_row = {"id": "req-1", "status": "approved"}
        decision = AutonomousDispatchDecision(
            mode="cli_approved",
            reason="CLI fallback approved",
        )
        ad = _mock_autonomous_dispatcher(
            pending=pending_row,
            approved=approved_row,
            route_decision=decision,
        )
        sd = _make_dispatcher(autonomous_dispatcher=ad)

        result = await sd.dispatch_step("t-1", _make_step(), [])

        # Should NOT be blocked — route() should have been called.
        ad.route.assert_awaited_once()
        # Result depends on route() returning cli_approved, which
        # means the invoker runs. Since our invoker is a mock, we
        # just verify we didn't get "blocked".
        # (cli_approved without invoker mock output will go to the
        #  invoker.run path, which is mocked.)
        assert result.status != "blocked" or "awaiting approval" not in result.result

    async def test_no_pending_goes_straight_to_route(self) -> None:
        """When there is no pending approval, go straight to route()."""
        decision = AutonomousDispatchDecision(
            mode="cli_approved",
            reason="approved",
        )
        ad = _mock_autonomous_dispatcher(pending=None, route_decision=decision)
        sd = _make_dispatcher(autonomous_dispatcher=ad)

        await sd.dispatch_step("t-1", _make_step(), [])

        ad.approval_gate.find_site_pending.assert_awaited_once()
        # find_recently_approved should NOT be called when there's no pending.
        ad.approval_gate.find_recently_approved.assert_not_awaited()
        ad.route.assert_awaited_once()

    async def test_find_pending_exception_proceeds_to_route(self) -> None:
        """If find_site_pending raises, proceed to route() (fail-open)."""
        decision = AutonomousDispatchDecision(
            mode="cli_approved",
            reason="approved",
        )
        ad = _mock_autonomous_dispatcher(
            find_pending_exc=RuntimeError("db error"),
            route_decision=decision,
        )
        sd = _make_dispatcher(autonomous_dispatcher=ad)

        await sd.dispatch_step("t-1", _make_step(), [])

        ad.route.assert_awaited_once()

    async def test_find_approved_exception_treats_as_pending(self) -> None:
        """If find_recently_approved raises, treat as still pending (fail-safe)."""
        pending_row = {"id": "req-2", "status": "pending"}
        ad = _mock_autonomous_dispatcher(
            pending=pending_row,
            find_approved_exc=RuntimeError("db error"),
        )
        sd = _make_dispatcher(autonomous_dispatcher=ad)

        step_result = await sd.dispatch_step("t-1", _make_step(), [])

        assert step_result.status == "blocked"
        assert "awaiting approval req-2" in step_result.result
        ad.route.assert_not_awaited()

    async def test_route_blocked_returns_blocked(self) -> None:
        """When route() returns blocked, the step result is blocked."""
        decision = AutonomousDispatchDecision(
            mode="blocked",
            reason="CLI fallback disabled",
        )
        ad = _mock_autonomous_dispatcher(pending=None, route_decision=decision)
        sd = _make_dispatcher(autonomous_dispatcher=ad)

        result = await sd.dispatch_step("t-1", _make_step(), [])

        assert result.status == "blocked"
        assert result.blocker_description == "CLI fallback disabled"

    async def test_policy_id_uses_step_type(self) -> None:
        """Verify policy_id is constructed from step type."""
        pending_row = {"id": "req-3", "status": "pending"}
        ad = _mock_autonomous_dispatcher(pending=pending_row, approved=None)
        sd = _make_dispatcher(autonomous_dispatcher=ad)

        await sd.dispatch_step("t-1", _make_step(step_type="research"), [])

        # Should have queried with policy_id "executor_research"
        ad.approval_gate.find_site_pending.assert_awaited_once_with(
            subsystem="task_executor",
            policy_id="executor_research",
        )
        ad.approval_gate.find_recently_approved.assert_awaited_once_with(
            subsystem="task_executor",
            policy_id="executor_research",
        )


# ---------------------------------------------------------------------------
# No autonomous dispatcher (direct invoker path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestNoAutonomousDispatcher:
    """When autonomous_dispatcher is None, steps go directly to invoker."""

    async def test_direct_invoker_success(self) -> None:
        invoker = AsyncMock()
        invoker.run = AsyncMock(return_value=MagicMock(
            is_error=False,
            text='{"status": "completed", "result": "done"}',
            cost_usd=0.01,
            session_id="sess-1",
            model_used="sonnet",
        ))
        sd = StepDispatcher(
            db=AsyncMock(),
            invoker=invoker,
            autonomous_dispatcher=None,
        )

        result = await sd.dispatch_step("t-1", _make_step(), [])

        assert result.status == "completed"
        invoker.run.assert_awaited_once()

    async def test_direct_invoker_error(self) -> None:
        invoker = AsyncMock()
        invoker.run = AsyncMock(return_value=MagicMock(
            is_error=True,
            error_message="session crashed",
            text="",
            cost_usd=0.0,
            session_id="sess-2",
            model_used="sonnet",
        ))
        sd = StepDispatcher(
            db=AsyncMock(),
            invoker=invoker,
            autonomous_dispatcher=None,
        )

        result = await sd.dispatch_step("t-1", _make_step(), [])

        assert result.status == "failed"
        assert "session crashed" in result.result
