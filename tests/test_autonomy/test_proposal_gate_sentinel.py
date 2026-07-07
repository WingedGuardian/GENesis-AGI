"""Fail-closed dispatch-gate fallback (Stage-1 autonomy-gate hardening).

Covers the degraded-mode primitives that replace the previous silent
fail-open when the real ``ProposalDispatchGate`` cannot be built (init
failure) or raises while evaluating a proposal.
"""

from __future__ import annotations

import pytest

from genesis.autonomy.proposal_gate import (
    HIGH_RISK_DOMAINS,
    DenyHighRiskSentinel,
    DispatchDecision,
    gate_failure_is_blocking,
    is_high_risk_domain,
)
from genesis.autonomy.types import ActionDomain

# action_types drawn from ACTION_TYPE_DOMAIN_MAP (classification.py).
_HIGH_RISK_ACTION_TYPES = [
    "outreach", "email", "apply",          # REPRESENT_USER
    "publish", "content", "post",          # EXTERNAL_WRITE
    "purchase", "payment",                 # FINANCIAL
    "code_change", "refactor",             # SELF_MODIFY
    "cognitive_variant_promotion",         # SELF_MODIFY
    "autonomous_build",                    # AUTONOMOUS_BUILD
]
_BENIGN_ACTION_TYPES = [
    "investigate", "research", "analyze",  # EXTERNAL_READ
    "diagnose", "monitor",                 # OBSERVE
    "maintenance", "config", "optimize",   # INTERNAL_WRITE
    "notification", "alert", "j9_regression",  # NOTIFY_USER
]


def test_high_risk_domains_is_the_external_and_self_modify_set():
    expected = {
        ActionDomain.EXTERNAL_WRITE,
        ActionDomain.REPRESENT_USER,
        ActionDomain.FINANCIAL,
        ActionDomain.SELF_MODIFY,
        ActionDomain.AUTONOMOUS_BUILD,
    }
    assert set(HIGH_RISK_DOMAINS) == expected


def test_high_risk_matches_min_level_contract():
    """HIGH_RISK_DOMAINS must be exactly the domains whose
    ACTION_DOMAIN_MIN_LEVEL is >= 2 or None (the KEEP IN SYNC rule stated
    at its definition). A new ActionDomain that raises the bar but is not
    added to HIGH_RISK_DOMAINS would be ALLOWED under a degraded gate."""
    from genesis.autonomy.types import ACTION_DOMAIN_MIN_LEVEL

    derived = {
        domain
        for domain, level in ACTION_DOMAIN_MIN_LEVEL.items()
        if level is None or level >= 2
    }
    assert set(HIGH_RISK_DOMAINS) == derived


@pytest.mark.parametrize("domain", sorted(HIGH_RISK_DOMAINS, key=str))
def test_is_high_risk_true(domain):
    assert is_high_risk_domain(domain) is True


@pytest.mark.parametrize("domain", [
    ActionDomain.OBSERVE,
    ActionDomain.EXTERNAL_READ,
    ActionDomain.INTERNAL_WRITE,
    ActionDomain.NOTIFY_USER,
])
def test_is_high_risk_false_for_benign(domain):
    assert is_high_risk_domain(domain) is False


@pytest.mark.parametrize("action_type", _HIGH_RISK_ACTION_TYPES)
def test_gate_failure_blocks_high_risk(action_type):
    assert gate_failure_is_blocking({"action_type": action_type}) is True


@pytest.mark.parametrize("action_type", _BENIGN_ACTION_TYPES)
def test_gate_failure_allows_benign(action_type):
    assert gate_failure_is_blocking({"action_type": action_type}) is False


def test_gate_failure_blocks_unmapped_type_with_high_risk_plan_hint():
    # Unknown action_type, but the execution_plan mentions publishing ->
    # classify_domain heuristic returns EXTERNAL_WRITE -> block.
    prop = {"action_type": "mystery", "execution_plan": "publish the post to medium"}
    assert gate_failure_is_blocking(prop) is True


def test_gate_failure_allows_fully_unmapped():
    # classify_domain's fallback is EXTERNAL_READ (benign) -> allow, so a
    # transient gate error can't wedge routine (non-external) autonomy.
    prop = {"action_type": "totally_unknown_type", "execution_plan": ""}
    assert gate_failure_is_blocking(prop) is False


def test_gate_failure_null_execution_plan_does_not_raise():
    # execution_plan can be NULL in the DB -> None; helper must coerce to "".
    prop = {"action_type": "outreach", "execution_plan": None}
    assert gate_failure_is_blocking(prop) is True


def test_gate_failure_blocks_on_undeterminable_proposal():
    # A None / malformed proposal (e.g. get_proposal raised, leaving the row
    # unresolved) is undeterminable -> classify raises -> fail CLOSED. Backs the
    # `prop_row is None or ...` guard in _process_execution_briefs.
    assert gate_failure_is_blocking(None) is True


async def test_sentinel_blocks_high_risk_and_reports_domain():
    decision = await DenyHighRiskSentinel().evaluate({"action_type": "publish"})
    assert isinstance(decision, DispatchDecision)
    assert decision.allowed is False
    assert decision.action_domain == ActionDomain.EXTERNAL_WRITE
    assert decision.rule_id  # non-empty so the block event carries a rule id


async def test_sentinel_allows_benign_and_reports_domain():
    decision = await DenyHighRiskSentinel().evaluate({"action_type": "investigate"})
    assert decision.allowed is True
    assert decision.action_domain == ActionDomain.EXTERNAL_READ
