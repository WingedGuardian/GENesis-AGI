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


# ---------------------------------------------------------------------------
# Fail-closed fallback (Stage-1 hardening)
# ---------------------------------------------------------------------------
# The dispatch gate is the last safety check before an already-user-approved
# proposal runs autonomously in the background.  Previously, if the real gate
# could not be built (init failure) or raised while evaluating, the caller
# SILENTLY dispatched — a fail-OPEN hole.  These primitives give a
# deterministic, dependency-free risk tier so a degraded gate fails CLOSED for
# high-consequence domains while staying resilient (allow) for benign ones —
# the latter avoids wedging the whole ego proposal cycle on a transient error.

#: Domains that act on outside parties or on Genesis's own code/identity.  This
#: is exactly the set whose ``ACTION_DOMAIN_MIN_LEVEL`` is >= 2 (or ``None``),
#: i.e. the domains the real gate would block below the earned autonomy level.
#: KEEP IN SYNC when adding an ``ActionDomain``: ``classify_domain``'s fallback
#: is ``EXTERNAL_READ`` (benign), so an UNMAPPED action is ALLOWED under a
#: degraded gate — any new external/self-modify domain MUST be added here.
HIGH_RISK_DOMAINS: frozenset[ActionDomain] = frozenset({
    ActionDomain.EXTERNAL_WRITE,
    ActionDomain.REPRESENT_USER,
    ActionDomain.FINANCIAL,
    ActionDomain.SELF_MODIFY,
})


def is_high_risk_domain(domain: ActionDomain) -> bool:
    """True if *domain* touches external parties or Genesis's own code/identity."""
    return domain in HIGH_RISK_DOMAINS


def gate_failure_is_blocking(proposal: dict | None) -> bool:
    """Decide fail-closed (True) vs resilient-allow (False) when the dispatch
    gate is unavailable or raised while evaluating *proposal*.

    Re-derives the action domain from the proposal via the PURE
    :func:`classify_domain` (no I/O).  Any error deriving it — including
    ``classify_domain`` itself having been the failure — returns ``True``
    (block): an undeterminable action is treated as high-risk.  Only
    clearly-benign domains (OBSERVE / EXTERNAL_READ / INTERNAL_WRITE /
    NOTIFY_USER) are allowed through, so a transient gate error can't wedge
    routine (non-external) autonomy.
    """
    try:
        domain = classify_domain(
            proposal.get("action_type", ""),
            proposal.get("execution_plan") or "",
        )
    except Exception:  # noqa: BLE001 — undeterminable domain => fail closed
        return True
    return is_high_risk_domain(domain)


class DenyHighRiskSentinel:
    """Fail-closed stand-in for :class:`ProposalDispatchGate`.

    Installed by ``runtime/init`` when the real gate cannot be constructed, so
    ``_proposal_dispatch_gate`` is NEVER left unset — which the ego would read
    as "no gate" and dispatch every approved proposal ungated.  Blocks
    HIGH_RISK_DOMAINS, allows benign domains.  Stateless; satisfies the same
    ``evaluate(proposal) -> DispatchDecision`` contract the ego consumes.
    """

    async def evaluate(self, proposal: dict) -> DispatchDecision:
        domain = classify_domain(
            proposal.get("action_type", ""),
            proposal.get("execution_plan") or "",
        )
        if is_high_risk_domain(domain):
            return DispatchDecision(
                allowed=False,
                reason="degraded mode: real dispatch gate unavailable",
                rule_id="deny_high_risk_sentinel",
                action_domain=domain,
            )
        return DispatchDecision(
            allowed=True,
            reason="degraded mode: benign domain allowed",
            rule_id="deny_high_risk_sentinel",
            action_domain=domain,
        )
