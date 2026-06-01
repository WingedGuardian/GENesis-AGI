"""Proposal dispatch gate — enforces autonomy rules before ego proposals execute.

The ego proposes freely. The user approves via Telegram. But before a
session is actually spawned, this gate verifies the action is permitted
given the current autonomy level and action domain.

Design principles:
- The ego NEVER knows this gate exists (opacity).
- User approval satisfies PROPOSE decisions (user sovereignty).
- Only BLOCK overrides user approval (hard safety invariants).
- Blocked proposals stay 'approved' — the ego sees nothing.
- The user is notified separately when a block occurs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from genesis.autonomy.classification import classify_domain
from genesis.autonomy.rules import RuleContext, RuleEngine
from genesis.autonomy.state_machine import AutonomyManager
from genesis.autonomy.types import (
    ACTION_DOMAIN_MIN_LEVEL,
    ActionDomain,
    ApprovalDecision,
    AutonomyCategory,
    ProtectionLevel,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchDecision:
    """Result of evaluating a proposal against the autonomy gate."""

    allowed: bool
    reason: str = ""
    rule_id: str = ""
    action_domain: ActionDomain | None = None


class ProposalDispatchGate:
    """Evaluates whether an approved ego proposal may be dispatched.

    Stateless — all state comes from the AutonomyManager and RuleEngine.
    The gate is checked in ``sweep_approved_proposals()`` before a
    DirectSessionRequest is built.
    """

    def __init__(
        self,
        *,
        autonomy_manager: AutonomyManager,
        rule_engine: RuleEngine,
        protected_paths: object | None = None,
    ) -> None:
        self._autonomy_manager = autonomy_manager
        self._rule_engine = rule_engine
        self._protected_paths = protected_paths

    async def evaluate(self, proposal: dict) -> DispatchDecision:
        """Evaluate whether *proposal* may be dispatched to a background session.

        Returns a DispatchDecision indicating whether the action is allowed.
        Blocked proposals are NOT modified — they stay in 'approved' status
        so the ego remains blind to the gate.
        """
        # 1. Derive action domain from proposal's action_type
        action_type = proposal.get("action_type", "")
        execution_plan = proposal.get("execution_plan", "") or ""
        domain = classify_domain(action_type, execution_plan)

        # 2. Quick check: is this domain blocked outright?
        min_level = ACTION_DOMAIN_MIN_LEVEL.get(domain)
        if min_level is None:
            # None means blocked from background entirely (e.g. SELF_MODIFY)
            return DispatchDecision(
                allowed=False,
                reason=f"Action domain '{domain.value}' is not permitted from background sessions",
                action_domain=domain,
            )

        # 3. Get current autonomy level for background cognitive
        state = await self._autonomy_manager.get_state(
            AutonomyCategory.BACKGROUND_COGNITIVE.value
        )
        current_level = state.current_level if state else 1

        # 4. Check minimum level for domain
        if current_level < min_level:
            return DispatchDecision(
                allowed=False,
                reason=(
                    f"Action domain '{domain.value}' requires L{min_level}, "
                    f"current level is L{current_level}"
                ),
                action_domain=domain,
            )

        # 5. Check against full rule engine for additional constraints
        # (protection levels, context-specific rules, etc.)
        protection_level = self._classify_protection(execution_plan)
        ctx = RuleContext(
            action_domain=domain,
            protection_level=protection_level,
            autonomy_level=current_level,
            context_category="background",
            description=proposal.get("content", "")[:200],
        )
        result = self._rule_engine.evaluate(ctx)

        if result.decision == ApprovalDecision.BLOCK:
            return DispatchDecision(
                allowed=False,
                reason=result.description or f"Blocked by rule '{result.rule_id}'",
                rule_id=result.rule_id,
                action_domain=domain,
            )

        # ACT or PROPOSE both pass — user already approved via Telegram
        return DispatchDecision(
            allowed=True,
            rule_id=result.rule_id,
            action_domain=domain,
        )

    def _classify_protection(self, execution_plan: str) -> ProtectionLevel | None:
        """Check if execution_plan references any protected paths."""
        if not self._protected_paths or not execution_plan:
            return None

        # Extract potential file paths from execution_plan text
        # Look for patterns like src/genesis/..., config/..., .claude/...
        import re
        path_pattern = re.compile(
            r"(?:src/genesis/\S+|config/\S+|\.claude/\S+|\.env\b|\bsecrets\.env\b)"
        )
        paths = path_pattern.findall(execution_plan)

        if not paths:
            return None

        # Classify each path, return highest protection level found
        classify = getattr(self._protected_paths, "classify", None)
        if classify is None:
            return None

        highest = ProtectionLevel.NORMAL
        for path in paths:
            level = classify(path)
            if level == ProtectionLevel.CRITICAL:
                return ProtectionLevel.CRITICAL  # Short-circuit
            if level == ProtectionLevel.SENSITIVE:
                highest = ProtectionLevel.SENSITIVE

        return highest if highest != ProtectionLevel.NORMAL else None
