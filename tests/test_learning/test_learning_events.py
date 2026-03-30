"""Tests for LEARNING subsystem enum and event constants."""

from __future__ import annotations

from genesis.learning.events import LEARNING_EVENTS
from genesis.observability.types import Subsystem


class TestLearningSubsystem:
    def test_learning_in_subsystem(self) -> None:
        assert Subsystem.LEARNING == "learning"

    def test_learning_enum_value(self) -> None:
        assert Subsystem("learning") == Subsystem.LEARNING


class TestLearningEvents:
    def test_all_expected_events_defined(self) -> None:
        expected = {
            "triage.classified",
            "classification.completed",
            "calibration.completed",
            "calibration.failed",
            "harvesting.completed",
            "capability_gap.recorded",
        }
        assert expected == set(LEARNING_EVENTS.values())

    def test_constants_are_strings(self) -> None:
        for _key, val in LEARNING_EVENTS.items():
            assert isinstance(val, str)
