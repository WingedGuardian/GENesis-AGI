"""Tests for genesis.autonomy.disagreement.DisagreementGate."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from genesis.autonomy.disagreement import DisagreementGate


@pytest.fixture
def gate():
    return DisagreementGate()


@pytest.mark.asyncio
async def test_review_returns_agreed(gate):
    result = await gate.review(
        action_description="do something",
        primary_assessment="looks good",
        action_class="reversible",
    )
    assert result.agreed is True


def test_record_disagreement(gate):
    gate.record_disagreement(domain="routing", primary="A", secondary="B")
    assert gate.disagreement_rate("routing") > 0


def test_disagreement_rate_empty(gate):
    assert gate.disagreement_rate("nonexistent") == 0.0


def test_disagreement_rate_calculation(gate):
    # 3 agreements, 2 disagreements = 0.4
    gate._disagreements["test"] = [False, False, False]  # agreements
    gate.record_disagreement(domain="test", primary="A", secondary="B")
    gate.record_disagreement(domain="test", primary="C", secondary="D")
    # Now: [False, False, False, True, True] → 2/5 = 0.4
    assert abs(gate.disagreement_rate("test") - 0.4) < 1e-9


def test_is_calibration_concern_false(gate):
    gate._disagreements["test"] = [False] * 10  # all agreements
    assert gate.is_calibration_concern("test") is False


def test_is_calibration_concern_true(gate):
    gate._disagreements["test"] = [True] * 10  # all disagreements
    assert gate.is_calibration_concern("test") is True


def test_rolling_window_limit(gate):
    # Record 120 disagreements — should be capped to 100
    for _ in range(120):
        gate.record_disagreement(domain="overflow", primary="X", secondary="Y")
    assert len(gate._disagreements["overflow"]) == 100


def test_event_emitted_on_disagreement():
    event_bus = MagicMock()
    gate = DisagreementGate(event_bus=event_bus)
    gate.record_disagreement(domain="test", primary="A", secondary="B")
    event_bus.emit.assert_called_once()
