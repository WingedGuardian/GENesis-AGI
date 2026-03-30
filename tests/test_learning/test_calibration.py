"""Tests for triage calibration cycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.learning.triage.calibration import TriageCalibrator
from genesis.learning.types import CalibrationRules


@dataclass
class FakeRoutingResult:
    success: bool = True
    content: str = ""


def _make_router(content: str, success: bool = True) -> MagicMock:
    router = MagicMock()
    router.route_call = AsyncMock(return_value=FakeRoutingResult(success=success, content=content))
    return router


def _make_db_with_observations(rows: list[dict] | None = None) -> AsyncMock:
    """DB mock that returns rows from async execute + fetchall."""
    resolved = rows or []
    # Convert dicts to tuples matching SELECT content, created_at
    tuples = [(r["content"], r["created_at"]) for r in resolved]
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=tuples)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=cursor)
    return db


VALID_LLM_OUTPUT_FENCED = """```json
{
  "examples": [
    {"scenario": "a", "depth": 0, "rationale": "trivial"},
    {"scenario": "b", "depth": 1, "rationale": "simple"},
    {"scenario": "c", "depth": 2, "rationale": "moderate"},
    {"scenario": "d", "depth": 3, "rationale": "complex"},
    {"scenario": "e", "depth": 4, "rationale": "obstacle"}
  ],
  "rules": ["rule1", "rule2"],
  "source_model": "test-model"
}
```"""

VALID_LLM_OUTPUT = """{
  "examples": [
    {"scenario": "a", "depth": 0, "rationale": "trivial"},
    {"scenario": "b", "depth": 1, "rationale": "simple"},
    {"scenario": "c", "depth": 2, "rationale": "moderate"},
    {"scenario": "d", "depth": 3, "rationale": "complex"},
    {"scenario": "e", "depth": 4, "rationale": "obstacle"}
  ],
  "rules": ["rule1", "rule2"],
  "source_model": "test-model"
}"""

TOO_FEW_EXAMPLES = """{
  "examples": [
    {"scenario": "a", "depth": 0, "rationale": "trivial"},
    {"scenario": "b", "depth": 1, "rationale": "simple"}
  ],
  "rules": ["rule1"],
  "source_model": "test-model"
}"""


class TestTriageCalibrator:
    def test_init_defaults(self, tmp_path: Path) -> None:
        cal = TriageCalibrator(
            router=MagicMock(), db=MagicMock(), calibration_path=tmp_path / "cal.md"
        )
        assert cal is not None

    @pytest.mark.asyncio
    async def test_successful_calibration(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(VALID_LLM_OUTPUT)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is not None
        assert isinstance(result, CalibrationRules)
        assert len(result.examples) >= 5
        assert len(result.rules) >= 1
        assert cal_path.exists()

    @pytest.mark.asyncio
    async def test_validation_rejects_too_few_examples(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(TOO_FEW_EXAMPLES)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is None
        assert not cal_path.exists()

    @pytest.mark.asyncio
    async def test_router_failure_returns_none(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router("", success=False)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is None

    @pytest.mark.asyncio
    async def test_no_observations_returns_none(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(VALID_LLM_OUTPUT)
        db = _make_db_with_observations([])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is None

    @pytest.mark.asyncio
    async def test_atomic_write(self, tmp_path: Path) -> None:
        """File should exist only after successful calibration, not during."""
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(VALID_LLM_OUTPUT)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is not None
        assert cal_path.exists()
        content = cal_path.read_text()
        assert "trivial" in content

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router("not valid json {{{")
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is None

    @pytest.mark.asyncio
    async def test_event_bus_emits_on_success(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(VALID_LLM_OUTPUT)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        event_bus = AsyncMock()
        cal = TriageCalibrator(
            router=router, db=db, calibration_path=cal_path, event_bus=event_bus
        )
        await cal.run_daily_calibration()
        event_bus.emit.assert_called_once()
        call_kwargs = event_bus.emit.call_args
        assert call_kwargs[1].get("event_type", call_kwargs[0][2] if len(call_kwargs[0]) > 2 else "") == "calibration.completed"

    @pytest.mark.asyncio
    async def test_event_bus_emits_on_failure(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router("", success=False)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        event_bus = AsyncMock()
        cal = TriageCalibrator(
            router=router, db=db, calibration_path=cal_path, event_bus=event_bus
        )
        await cal.run_daily_calibration()
        event_bus.emit.assert_called_once()
        call_kwargs = event_bus.emit.call_args
        assert call_kwargs[1].get("event_type", call_kwargs[0][2] if len(call_kwargs[0]) > 2 else "") == "calibration.failed"

    @pytest.mark.asyncio
    async def test_json_with_markdown_fences_parses(self, tmp_path: Path) -> None:
        """LLMs often wrap JSON in ```json fences — calibrator should handle this."""
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(VALID_LLM_OUTPUT_FENCED)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is not None
        assert isinstance(result, CalibrationRules)
        assert len(result.examples) == 5
        assert cal_path.exists()
