"""Wiring tests: the live cognitive write paths record into the ledger.

Proves end-to-end (real DB, real file) that the skill applicator and the triage
calibration regen now route their overwrite through the cognitive self-mod ledger,
producing a recoverable ``cognitive_file_modifications`` row with the right actor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.db.crud import cognitive_file_modifications as cfm

_VALID_CALIBRATION_JSON = """{
  "examples": [
    {"scenario": "a", "depth": 0, "rationale": "trivial"},
    {"scenario": "b", "depth": 1, "rationale": "simple"},
    {"scenario": "c", "depth": 2, "rationale": "moderate"},
    {"scenario": "d", "depth": 3, "rationale": "complex"},
    {"scenario": "e", "depth": 4, "rationale": "obstacle"}
  ],
  "rules": ["rule1", "BIAS RULE: corrections/frustration are depth 2+, praise alone is not"],
  "source_model": "test-model"
}"""


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        from genesis.db.schema import create_all_tables

        await create_all_tables(conn)
        await conn.commit()
        yield conn


@pytest.mark.asyncio
async def test_skill_applicator_records_ledger_row(db, tmp_path, monkeypatch):
    from genesis.learning.skills.applicator import SkillApplicator
    from genesis.learning.skills.types import (
        ChangeSize,
        SkillProposal,
        ValidationResult,
    )

    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("old content")

    proposal = SkillProposal(
        skill_name="test-skill",
        proposed_content="new content",
        rationale="improvement",
        change_size=ChangeSize.MINOR,
    )
    applicator = SkillApplicator(autonomy_level=2)
    applicator._validator.validate = lambda *a, **kw: ValidationResult(
        passed=True, test_results={}, blocking_failures=[], warnings=[],
    )
    import genesis.learning.skills.wiring as wiring_mod

    monkeypatch.setattr(
        wiring_mod, "get_skill_path", lambda name: skill_dir / "SKILL.md",
    )

    result = await applicator.apply(proposal, db)
    assert result["action"] == "applied"
    assert (skill_dir / "SKILL.md").read_text() == "new content"

    rows = await cfm.recent(db, limit=10, actor="skill_evolution")
    assert len(rows) == 1
    assert rows[0]["prior_content"] == "old content"
    assert rows[0]["applied_content"] == "new content"
    assert rows[0]["metadata"]["skill_name"] == "test-skill"


@pytest.mark.asyncio
async def test_triage_calibration_records_ledger_row(db, tmp_path):
    from genesis.db.crud import observations
    from genesis.learning.triage.calibration import TriageCalibrator

    # Seed a recent triage observation so calibration has something to chew on.
    await observations.create(
        db, id="o1", source="retrospective", type="triage_decision",
        content="triage depth=2 correction", priority="medium",
        created_at=datetime.now(UTC).isoformat(),
    )

    router = SimpleNamespace()

    async def _route_call(_site, _messages):
        return SimpleNamespace(success=True, content=_VALID_CALIBRATION_JSON)

    router.route_call = _route_call

    cal_path = tmp_path / "TRIAGE_CALIBRATION.md"
    cal = TriageCalibrator(router=router, db=db, calibration_path=cal_path)
    result = await cal.run_daily_calibration()

    assert result is not None
    assert cal_path.exists()  # the file was written via the ledger

    rows = await cfm.recent(db, limit=10, actor="triage_calibration_daily")
    assert len(rows) == 1
    assert rows[0]["target_path"] == str(cal_path)
    assert "Few-Shot Examples" in rows[0]["applied_content"]
