"""Tests for the WS-8 capability-cell state machine, posterior, and classifier.

Pure logic — no DB.  Covers the 4-state machine (legal transitions,
denied-permanent-only-by-user, regression, decay), the simple posterior, and
``classify_email_action``'s risk gradient (reply < cold < bulk).
"""

from __future__ import annotations

import pytest

from genesis.autonomy.capabilities import (
    GRANT_FLOOR,
    InvalidTransition,
    can_transition,
    should_regress,
    transition,
)
from genesis.autonomy.classification import (
    EmailActionClassification,
    classify_email_action,
)
from genesis.autonomy.types import (
    RISK_SEVERITY,
    ActionClass,
    CellEvent,
    CellState,
    RiskClass,
)
from genesis.db.crud.capability_grants import cell_posterior

# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


class TestTransitions:
    def test_classify_starts_the_cell(self):
        assert transition(CellState.NOT_DETERMINED, CellEvent.CLASSIFY) == CellState.ASK

    def test_approve_promotes(self):
        assert transition(CellState.ASK, CellEvent.APPROVE) == CellState.GRANTED

    def test_deny_is_permanent(self):
        assert (
            transition(CellState.ASK, CellEvent.DENY_PERMANENT)
            == CellState.DENIED_PERMANENT
        )

    def test_regress_drops_to_ask_keeping_history(self):
        # GRANTED → ASK (not all the way to NOT_DETERMINED) preserves evidence.
        assert transition(CellState.GRANTED, CellEvent.REGRESS) == CellState.ASK

    def test_decay_returns_to_not_determined(self):
        assert (
            transition(CellState.GRANTED, CellEvent.DECAY)
            == CellState.NOT_DETERMINED
        )

    def test_revoke_is_permanent(self):
        assert (
            transition(CellState.GRANTED, CellEvent.REVOKE)
            == CellState.DENIED_PERMANENT
        )

    def test_denied_permanent_is_terminal(self):
        # No event escapes DENIED_PERMANENT.
        for event in CellEvent:
            assert not can_transition(CellState.DENIED_PERMANENT, event)

    def test_denied_permanent_only_reachable_by_user_action(self):
        # Only DENY_PERMANENT / REVOKE land in DENIED_PERMANENT — never REGRESS
        # or DECAY (a competence dip or staleness must not permanently lock a
        # cell on its own).
        user_only = {CellEvent.DENY_PERMANENT, CellEvent.REVOKE}
        for state in CellState:
            for event in CellEvent:
                if not can_transition(state, event):
                    continue
                if transition(state, event) == CellState.DENIED_PERMANENT:
                    assert event in user_only

    def test_cannot_approve_a_cell_never_asked(self):
        with pytest.raises(InvalidTransition):
            transition(CellState.NOT_DETERMINED, CellEvent.APPROVE)

    def test_cannot_regress_an_unasked_cell(self):
        with pytest.raises(InvalidTransition):
            transition(CellState.ASK, CellEvent.REGRESS)


# --------------------------------------------------------------------------- #
# Posterior + regression floor
# --------------------------------------------------------------------------- #


class TestPosterior:
    def test_no_evidence_is_uninformative(self):
        assert cell_posterior(0, 0) == 0.5

    def test_mirror_of_legacy_formula(self):
        # (s + 1) / (s + c + 2)
        assert cell_posterior(3, 2) == pytest.approx(4 / 7)
        assert cell_posterior(50, 2) == pytest.approx(51 / 54)

    def test_should_regress_below_floor(self):
        assert should_regress(0.0)  # well below the floor
        assert should_regress(GRANT_FLOOR - 0.01)
        assert not should_regress(GRANT_FLOOR)
        assert not should_regress(0.9)


# --------------------------------------------------------------------------- #
# Email classification
# --------------------------------------------------------------------------- #


class TestClassifyEmailAction:
    def test_known_thread_reply_is_standard(self):
        c = classify_email_action(is_reply=True, recipient_known=True)
        assert c.risk_class == RiskClass.STANDARD
        assert c.sub_class == "reply"
        assert c.cell_key == ("email", "send", "standard")
        assert c.identity_bar is True

    def test_cold_outreach_crosses_identity_bar(self):
        c = classify_email_action(is_reply=False, recipient_known=False)
        assert c.risk_class == RiskClass.IDENTITY
        assert c.sub_class == "cold"

    def test_reply_to_unknown_recipient_is_cold(self):
        # is_reply alone isn't enough — an unknown recipient is still cold.
        c = classify_email_action(is_reply=True, recipient_known=False)
        assert c.risk_class == RiskClass.IDENTITY

    def test_bulk_send_is_bulk(self):
        c = classify_email_action(is_bulk=True, is_reply=True, recipient_known=True)
        assert c.risk_class == RiskClass.BULK
        assert c.sub_class == "bulk"

    def test_financial_email_is_hardline(self):
        c = classify_email_action(
            is_reply=True, recipient_known=True,
            subject="Invoice #42", body="Please wire transfer the balance.",
        )
        assert c.risk_class == RiskClass.FINANCIAL
        assert c.sub_class == "financial"

    def test_risk_gradient_reply_lt_cold_lt_bulk(self):
        reply = classify_email_action(is_reply=True, recipient_known=True)
        cold = classify_email_action(is_reply=False, recipient_known=False)
        bulk = classify_email_action(is_bulk=True)
        assert (
            RISK_SEVERITY[reply.risk_class]
            < RISK_SEVERITY[cold.risk_class]
            < RISK_SEVERITY[bulk.risk_class]
        )

    def test_returns_frozen_dataclass_with_action_class(self):
        c = classify_email_action(is_reply=True, recipient_known=True)
        assert isinstance(c, EmailActionClassification)
        assert isinstance(c.action_class, ActionClass)
