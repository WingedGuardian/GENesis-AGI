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
  "rules": ["rule1", "BIAS RULE: praise alone does not raise depth; corrections/frustration are depth 2+"],
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
  "rules": ["rule1", "BIAS RULE: praise alone does not raise depth; corrections/frustration are depth 2+"],
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

# Full depth coverage but rules lack a "BIAS RULE:" entry -> canonical rule injected.
MISSING_BIAS_RULE = """{
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

# JSON-valid but rules is null (non-list) -> must fail gracefully (None), not raise.
NULL_RULES = """{
  "examples": [
    {"scenario": "a", "depth": 0, "rationale": "trivial"},
    {"scenario": "b", "depth": 1, "rationale": "simple"},
    {"scenario": "c", "depth": 2, "rationale": "moderate"},
    {"scenario": "d", "depth": 3, "rationale": "complex"},
    {"scenario": "e", "depth": 4, "rationale": "obstacle"}
  ],
  "rules": null,
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
    async def test_injects_canonical_bias_rule_when_missing(self, tmp_path: Path) -> None:
        """Enforcement of A (inject, not reject): a regen with full depth coverage but
        no 'BIAS RULE:' entry still SUCCEEDS (adaptation lands) and has the canonical
        bias rule injected, so the principle is always present in the written file."""
        from genesis.learning.triage.calibration import _CANONICAL_BIAS_RULE

        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(MISSING_BIAS_RULE)
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is not None  # injected, not rejected
        assert _CANONICAL_BIAS_RULE in result.rules
        assert cal_path.exists()
        assert "BIAS RULE" in cal_path.read_text()

    @pytest.mark.asyncio
    async def test_keeps_model_provided_bias_rule(self, tmp_path: Path) -> None:
        """If the model already supplied a BIAS RULE, keep exactly it (no duplicate
        canonical injection)."""
        from genesis.learning.triage.calibration import _CANONICAL_BIAS_RULE

        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(VALID_LLM_OUTPUT)  # already contains a BIAS RULE
        db = _make_db_with_observations([
            {"content": "triage depth=2", "created_at": "2026-03-09T00:00:00"},
        ])
        cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
        result = await cal.run_daily_calibration()

        assert result is not None
        bias_rules = [
            r for r in result.rules if r.strip().lower().startswith("bias rule")
        ]
        assert len(bias_rules) == 1  # the model's, not duplicated
        # NOTE: VALID_LLM_OUTPUT's BIAS RULE text is intentionally distinct from
        # _CANONICAL_BIAS_RULE — keep them different so this guards "model rule kept".
        assert _CANONICAL_BIAS_RULE not in result.rules

    @pytest.mark.asyncio
    async def test_non_list_rules_returns_none(self, tmp_path: Path) -> None:
        """Defensive: a JSON-valid response with non-list rules (e.g. null) must fail
        gracefully (return None, keep the current file) rather than raise into the
        daily job."""
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(NULL_RULES)
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
    async def test_no_observations_emits_skipped_event(self, tmp_path: Path) -> None:
        cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
        router = _make_router(VALID_LLM_OUTPUT)
        db = _make_db_with_observations([])
        event_bus = AsyncMock()
        cal = TriageCalibrator(
            router=router, db=db, calibration_path=cal_path, event_bus=event_bus,
        )
        result = await cal.run_daily_calibration()

        assert result is None
        event_bus.emit.assert_called_once()
        call_kwargs = event_bus.emit.call_args
        assert call_kwargs.kwargs["event_type"] == "calibration.skipped"

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


def test_calibration_prompt_enforces_bias_correction() -> None:
    """A (bias-aware regen): the regeneration prompt must carry the bias-correction
    directive so the daily loop produces examples that REINFORCE the classifier's
    hardcoded bias rule (praise != depth) rather than drifting to positive-valence
    inflation. The classifier hardcodes the rule regardless; this keeps the few-shot
    examples consistent with it."""
    from genesis.learning.triage.calibration import _CALIBRATION_PROMPT

    prompt = _CALIBRATION_PROMPT.lower()
    assert "bias correction" in prompt
    assert "praise" in prompt
    assert "frustration" in prompt or "correction" in prompt
    assert "bias rule" in prompt


def test_example_seed_is_bias_aware() -> None:
    """B (seed refresh): the committed .example seed — used by bootstrap.sh on fresh
    installs and any re-seed — must carry the curated BIAS RULE, not a stale generic
    snapshot. Guards against regression to a bias-blind seed."""
    from genesis.learning.triage.calibration import _DEFAULT_CALIBRATION_PATH

    seed = _DEFAULT_CALIBRATION_PATH.with_name(
        _DEFAULT_CALIBRATION_PATH.name + ".example"
    )
    assert seed.exists(), f"calibration seed missing: {seed}"
    text = seed.read_text()
    assert "BIAS RULE" in text
    assert "praise" in text.lower()
