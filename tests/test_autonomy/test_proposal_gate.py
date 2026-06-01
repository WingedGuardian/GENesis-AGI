"""Tests for ProposalDispatchGate — autonomy enforcement at dispatch boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.autonomy.proposal_gate import ProposalDispatchGate
from genesis.autonomy.rules import RuleEngine
from genesis.autonomy.state_machine import AutonomyManager
from genesis.autonomy.types import ActionDomain


def _make_gate(current_level: int = 2) -> ProposalDispatchGate:
    """Create a gate with a mock autonomy manager at the given level."""
    mgr = MagicMock(spec=AutonomyManager)
    mock_state = MagicMock()
    mock_state.current_level = current_level
    mgr.get_state = AsyncMock(return_value=mock_state)

    engine = RuleEngine()

    return ProposalDispatchGate(
        autonomy_manager=mgr,
        rule_engine=engine,
    )


class TestProposalDispatchGate:
    """Core dispatch gate tests."""

    @pytest.mark.asyncio
    async def test_research_allowed_at_l1(self) -> None:
        gate = _make_gate(current_level=1)
        result = await gate.evaluate({"action_type": "research"})
        assert result.allowed
        assert result.action_domain == ActionDomain.EXTERNAL_READ

    @pytest.mark.asyncio
    async def test_maintenance_allowed_at_l1(self) -> None:
        gate = _make_gate(current_level=1)
        result = await gate.evaluate({"action_type": "maintenance"})
        assert result.allowed
        assert result.action_domain == ActionDomain.INTERNAL_WRITE

    @pytest.mark.asyncio
    async def test_outreach_blocked_below_l3(self) -> None:
        gate = _make_gate(current_level=2)
        result = await gate.evaluate({"action_type": "outreach"})
        assert not result.allowed
        assert "L3" in result.reason
        assert result.action_domain == ActionDomain.REPRESENT_USER

    @pytest.mark.asyncio
    async def test_outreach_allowed_at_l3(self) -> None:
        gate = _make_gate(current_level=3)
        result = await gate.evaluate({"action_type": "outreach"})
        assert result.allowed

    @pytest.mark.asyncio
    async def test_self_modify_always_blocked(self) -> None:
        gate = _make_gate(current_level=4)
        result = await gate.evaluate({"action_type": "code_change"})
        assert not result.allowed
        assert "not permitted" in result.reason
        assert result.action_domain == ActionDomain.SELF_MODIFY

    @pytest.mark.asyncio
    async def test_financial_blocked_below_l4(self) -> None:
        gate = _make_gate(current_level=3)
        result = await gate.evaluate({"action_type": "purchase"})
        assert not result.allowed
        assert "L4" in result.reason

    @pytest.mark.asyncio
    async def test_financial_allowed_at_l4(self) -> None:
        gate = _make_gate(current_level=4)
        result = await gate.evaluate({"action_type": "purchase"})
        assert result.allowed

    @pytest.mark.asyncio
    async def test_unknown_action_type_defaults_to_external_read(self) -> None:
        gate = _make_gate(current_level=1)
        result = await gate.evaluate({"action_type": "some_new_type"})
        assert result.allowed
        assert result.action_domain == ActionDomain.EXTERNAL_READ

    @pytest.mark.asyncio
    async def test_execution_plan_overrides_unknown_type(self) -> None:
        gate = _make_gate(current_level=1)
        result = await gate.evaluate({
            "action_type": "unknown",
            "execution_plan": "Use browser_fill to submit the application form",
        })
        assert not result.allowed  # REPRESENT_USER requires L3
        assert result.action_domain == ActionDomain.REPRESENT_USER

    @pytest.mark.asyncio
    async def test_critical_path_blocked_by_rule(self) -> None:
        """Critical protection level blocked by existing autonomy rules."""
        from genesis.autonomy.protection import ProtectedPathRegistry

        protected = ProtectedPathRegistry.from_yaml()
        gate = ProposalDispatchGate(
            autonomy_manager=_make_gate(current_level=4)._autonomy_manager,
            rule_engine=RuleEngine(),
            protected_paths=protected,
        )
        result = await gate.evaluate({
            "action_type": "maintenance",
            "execution_plan": "Edit src/genesis/channels/telegram/adapter.py to fix bug",
        })
        # CRITICAL path from background context → blocked by rule
        assert not result.allowed

    @pytest.mark.asyncio
    async def test_no_state_defaults_to_l1(self) -> None:
        """If AutonomyManager returns no state, default to L1."""
        mgr = MagicMock(spec=AutonomyManager)
        mgr.get_state = AsyncMock(return_value=None)
        gate = ProposalDispatchGate(
            autonomy_manager=mgr,
            rule_engine=RuleEngine(),
        )
        result = await gate.evaluate({"action_type": "research"})
        assert result.allowed  # EXTERNAL_READ needs L1, default is L1
