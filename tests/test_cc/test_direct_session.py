"""Tests for DirectSessionRequest and planning instruction behavior."""

from __future__ import annotations

import pytest

from genesis.cc.direct_session import DirectSessionRequest


class TestDirectSessionRequest:
    """Unit tests for the DirectSessionRequest dataclass."""

    def test_planning_instruction_default_none(self):
        """planning_instruction defaults to None."""
        r = DirectSessionRequest(prompt="do the thing")
        assert r.planning_instruction is None

    def test_planning_instruction_set(self):
        """planning_instruction can be set explicitly."""
        r = DirectSessionRequest(
            prompt="do the thing",
            planning_instruction="Plan your approach first.",
        )
        assert r.planning_instruction == "Plan your approach first."

    def test_invalid_profile_raises(self):
        """Invalid profile raises ValueError."""
        with pytest.raises(ValueError, match="Invalid profile"):
            DirectSessionRequest(prompt="test", profile="admin")
