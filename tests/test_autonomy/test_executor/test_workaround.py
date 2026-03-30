"""Tests for genesis.autonomy.executor.workaround.WorkaroundSearcherImpl."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from genesis.autonomy.executor.workaround import WorkaroundSearcherImpl


@dataclass
class FakeProcedureMatch:
    task_type: str = "code"
    confidence: float = 0.8
    steps: list[str] | None = None
    procedure_id: str = "proc-001"
    success_count: int = 5
    failure_count: int = 1
    failure_modes: list = None
    workarounds: list = None

    def __post_init__(self):
        if self.failure_modes is None:
            self.failure_modes = []
        if self.workarounds is None:
            self.workarounds = []


@pytest.mark.asyncio
class TestWorkaroundSearcher:
    async def test_no_db_returns_none(self) -> None:
        searcher = WorkaroundSearcherImpl(db=None)
        result = await searcher.search(
            {"idx": 0, "type": "code"}, "error msg", [],
        )
        assert result is None

    async def test_procedural_match_found(self) -> None:
        match = FakeProcedureMatch(
            steps=["Try approach A", "Then do B"],
            confidence=0.9,
        )
        with patch(
            "genesis.learning.procedural.matcher.find_relevant",
            new_callable=AsyncMock,
            return_value=[match],
        ):
            searcher = WorkaroundSearcherImpl(db=AsyncMock())
            result = await searcher.search(
                {"idx": 1, "type": "code", "description": "Fix the bug"},
                "ImportError: no module named foo",
                [],
            )

        assert result is not None
        assert result.found is True
        assert "approach A" in result.approach

    async def test_procedural_no_match(self) -> None:
        with patch(
            "genesis.learning.procedural.matcher.find_relevant",
            new_callable=AsyncMock,
            return_value=[],
        ):
            searcher = WorkaroundSearcherImpl(db=AsyncMock())
            result = await searcher.search(
                {"idx": 0, "type": "research"}, "timeout", [],
            )

        assert result is not None
        assert result.found is False

    async def test_find_relevant_exception(self) -> None:
        with patch(
            "genesis.learning.procedural.matcher.find_relevant",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB gone"),
        ):
            searcher = WorkaroundSearcherImpl(db=AsyncMock())
            result = await searcher.search(
                {"idx": 0, "type": "code"}, "error", [],
            )

        assert result is not None
        assert result.found is False

    async def test_match_without_steps_uses_str(self) -> None:
        match = FakeProcedureMatch(steps=None, confidence=0.7)
        with patch(
            "genesis.learning.procedural.matcher.find_relevant",
            new_callable=AsyncMock,
            return_value=[match],
        ):
            searcher = WorkaroundSearcherImpl(db=AsyncMock())
            result = await searcher.search(
                {"idx": 0, "type": "code"}, "err", [],
            )

        assert result.found is True
        assert result.approach is not None
