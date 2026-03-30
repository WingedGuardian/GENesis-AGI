"""Tests for genesis.autonomy.trace_verification.TraceVerifier."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from genesis.autonomy.trace_verification import TraceResult, TraceVerifier


@pytest.fixture
def verifier():
    return TraceVerifier()


# ------------------------------------------------------------------
# Routing verifier
# ------------------------------------------------------------------


def test_routing_match_passes(verifier):
    result = verifier.verify(
        decision_type="routing",
        stated_reason="token count > 500",
        actual_data={"token_count": 600},
    )
    assert result.passed is True


def test_routing_mismatch_fails(verifier):
    result = verifier.verify(
        decision_type="routing",
        stated_reason="token count > 500",
        actual_data={"token_count": 300},
    )
    assert result.passed is False
    assert "token_count=300" in result.mismatch_detail


def test_routing_no_token_count_passes(verifier):
    result = verifier.verify(
        decision_type="routing",
        stated_reason="token count > 500",
        actual_data={"model": "sonnet"},
    )
    assert result.passed is True


# ------------------------------------------------------------------
# Triage verifier
# ------------------------------------------------------------------


def test_triage_match_passes(verifier):
    result = verifier.verify(
        decision_type="triage",
        stated_reason="high priority task",
        actual_data={"priority": "high"},
    )
    assert result.passed is True


def test_triage_mismatch_fails(verifier):
    result = verifier.verify(
        decision_type="triage",
        stated_reason="low priority task",
        actual_data={"priority": "high"},
    )
    assert result.passed is False


# ------------------------------------------------------------------
# Unknown / custom verifiers
# ------------------------------------------------------------------


def test_no_verifier_passes(verifier):
    result = verifier.verify(
        decision_type="unknown_type",
        stated_reason="some reason",
        actual_data={},
    )
    assert result.passed is True
    assert "no verifier" in result.mismatch_detail


def test_custom_verifier_called(verifier):
    def my_verifier(payload: dict) -> TraceResult:
        return TraceResult(
            passed=False,
            decision_type="custom",
            stated_reason=payload["stated_reason"],
            mismatch_detail="custom check failed",
        )

    verifier.register("custom", my_verifier)
    result = verifier.verify(
        decision_type="custom",
        stated_reason="reason",
        actual_data={},
    )
    assert result.passed is False
    assert result.mismatch_detail == "custom check failed"


# ------------------------------------------------------------------
# Mismatch rate and confabulation concern
# ------------------------------------------------------------------


def test_mismatch_rate_zero(verifier):
    assert verifier.mismatch_rate("routing") == 0.0


def test_mismatch_rate_calculation(verifier):
    # 2 passes, 1 fail = 1/3 mismatch rate
    verifier.verify(decision_type="routing", stated_reason="token count > 500", actual_data={"token_count": 600})
    verifier.verify(decision_type="routing", stated_reason="token count > 500", actual_data={"token_count": 600})
    verifier.verify(decision_type="routing", stated_reason="token count > 500", actual_data={"token_count": 300})
    rate = verifier.mismatch_rate("routing")
    assert abs(rate - 1.0 / 3.0) < 1e-9


def test_is_confabulation_concern(verifier):
    # Force high mismatch rate
    verifier._results["routing"] = [False] * 10  # all mismatches
    assert verifier.is_confabulation_concern("routing") is True


def test_rolling_window(verifier):
    # Insert 120 entries directly
    verifier._results["routing"] = [True] * 120
    # Trigger a verify to activate the trim
    verifier.verify(decision_type="routing", stated_reason="token count > 500", actual_data={"token_count": 600})
    assert len(verifier._results["routing"]) <= 100


def test_event_emitted_on_mismatch():
    event_bus = MagicMock()
    v = TraceVerifier(event_bus=event_bus)
    v.verify(
        decision_type="routing",
        stated_reason="token count > 500",
        actual_data={"token_count": 300},
    )
    event_bus.emit.assert_called_once()
