"""Tests for skill evolution: types, effectiveness, refiner, applicator."""

from __future__ import annotations

import json
import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.learning.skills.applicator import SkillApplicator
from genesis.learning.skills.effectiveness import SkillEffectivenessAnalyzer
from genesis.learning.skills.refiner import SkillRefiner
from genesis.learning.skills.types import (
    ChangeSize,
    SkillProposal,
    SkillReport,
    SkillTrend,
    SkillType,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db():
    """In-memory SQLite with all tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    from genesis.db.schema import create_all_tables, seed_data
    await create_all_tables(conn)
    await seed_data(conn)
    yield conn
    await conn.close()


def _insert_session(
    db,
    *,
    status: str = "completed",
    skill_name: str = "test-skill",
    session_type: str = "background_reflection",
    started_at: str | None = None,
    metadata_extra: dict | None = None,
):
    """Helper to insert a cc_session row."""
    sid = str(uuid.uuid4())
    now = started_at or datetime.now(UTC).isoformat()
    meta = {"skill_tags": [skill_name]}
    if metadata_extra:
        meta.update(metadata_extra)
    return db.execute(
        """INSERT INTO cc_sessions
           (id, session_type, model, effort, status, started_at,
            last_activity_at, source_tag, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, session_type, "test-model", "medium", status, now, now,
         "background", json.dumps(meta)),
    )


# ---------------------------------------------------------------------------
# Type tests
# ---------------------------------------------------------------------------

class TestTypes:
    def test_change_size_values(self):
        assert ChangeSize.MINOR == "minor"
        assert ChangeSize.MODERATE == "moderate"
        assert ChangeSize.MAJOR == "major"

    def test_skill_type_values(self):
        assert SkillType.UPLIFT == "uplift"
        assert SkillType.WORKFLOW == "workflow"
        assert SkillType.HYBRID == "hybrid"

    def test_skill_trend_values(self):
        assert SkillTrend.IMPROVING == "improving"
        assert SkillTrend.STABLE == "stable"
        assert SkillTrend.DECLINING == "declining"

    def test_skill_report_frozen(self):
        report = SkillReport(
            skill_name="x", usage_count=1, success_count=1,
            failure_count=0, success_rate=1.0,
        )
        with pytest.raises(FrozenInstanceError):
            report.skill_name = "y"  # type: ignore[misc]

    def test_skill_proposal_frozen(self):
        proposal = SkillProposal(
            skill_name="x", proposed_content="c",
            rationale="r", change_size=ChangeSize.MINOR,
        )
        with pytest.raises(FrozenInstanceError):
            proposal.skill_name = "y"  # type: ignore[misc]

    def test_skill_report_defaults(self):
        report = SkillReport(
            skill_name="x", usage_count=0, success_count=0,
            failure_count=0, success_rate=0.0,
        )
        assert report.baseline_success_rate is None
        assert report.failure_patterns == []
        assert report.trend == SkillTrend.STABLE
        assert report.tools_used == []
        assert report.tools_declared == []
        assert report.sessions_since_last_refined == 0

    def test_skill_proposal_defaults(self):
        p = SkillProposal(
            skill_name="x", proposed_content="c",
            rationale="r", change_size=ChangeSize.MINOR,
        )
        assert p.confidence == 0.7
        assert p.failure_patterns_addressed == []


# ---------------------------------------------------------------------------
# Effectiveness Analyzer tests
# ---------------------------------------------------------------------------

class TestEffectivenessAnalyzer:
    @pytest.mark.asyncio
    async def test_analyze_empty(self, db):
        analyzer = SkillEffectivenessAnalyzer()
        report = await analyzer.analyze(db, "nonexistent-skill")
        assert report.usage_count == 0
        assert report.success_rate == 0.0
        assert report.trend == SkillTrend.STABLE

    @pytest.mark.asyncio
    async def test_analyze_with_sessions(self, db):
        for _ in range(3):
            await _insert_session(db, status="completed", skill_name="my-skill")
        await _insert_session(
            db, status="failed", skill_name="my-skill",
            metadata_extra={"failure_reason": "timeout"},
        )
        await db.commit()

        analyzer = SkillEffectivenessAnalyzer()
        report = await analyzer.analyze(db, "my-skill")
        assert report.usage_count == 4
        assert report.success_count == 3
        assert report.failure_count == 1
        assert report.success_rate == pytest.approx(0.75)
        assert "timeout" in report.failure_patterns

    @pytest.mark.asyncio
    async def test_analyze_tools_extraction(self, db):
        await _insert_session(
            db, skill_name="tool-skill",
            metadata_extra={"tools_used": ["Read", "Bash"], "tools_declared": ["Read"]},
        )
        await db.commit()

        analyzer = SkillEffectivenessAnalyzer()
        report = await analyzer.analyze(db, "tool-skill")
        assert "Bash" in report.tools_used
        assert "Read" in report.tools_used
        assert "Read" in report.tools_declared

    @pytest.mark.asyncio
    async def test_needs_review_below_baseline(self):
        analyzer = SkillEffectivenessAnalyzer()
        report = SkillReport(
            skill_name="x", usage_count=10, success_count=5,
            failure_count=5, success_rate=0.5,
            baseline_success_rate=0.8,
        )
        assert analyzer.needs_review(report) is True

    @pytest.mark.asyncio
    async def test_needs_review_declining(self):
        analyzer = SkillEffectivenessAnalyzer()
        report = SkillReport(
            skill_name="x", usage_count=10, success_count=5,
            failure_count=5, success_rate=0.5,
            trend=SkillTrend.DECLINING,
        )
        assert analyzer.needs_review(report) is True

    @pytest.mark.asyncio
    async def test_needs_review_tools_mismatch(self):
        analyzer = SkillEffectivenessAnalyzer()
        report = SkillReport(
            skill_name="x", usage_count=10, success_count=8,
            failure_count=2, success_rate=0.8,
            tools_used=["Read", "Bash", "Write"],
            tools_declared=["Read", "Bash"],
        )
        assert analyzer.needs_review(report) is True

    @pytest.mark.asyncio
    async def test_needs_review_healthy(self):
        analyzer = SkillEffectivenessAnalyzer()
        report = SkillReport(
            skill_name="x", usage_count=10, success_count=9,
            failure_count=1, success_rate=0.9,
            baseline_success_rate=0.8,
            trend=SkillTrend.STABLE,
            tools_used=["Read"],
            tools_declared=["Read", "Bash"],
        )
        assert analyzer.needs_review(report) is False

    @pytest.mark.asyncio
    async def test_trend_improving(self, db):
        analyzer = SkillEffectivenessAnalyzer()
        # Older sessions: all failed, newer: all succeeded
        # Sessions ordered DESC by started_at, so newer first
        for i in range(5):
            await _insert_session(
                db, skill_name="trend-skill", status="completed",
                started_at=f"2026-03-10T10:0{i}:00",
            )
        for i in range(5):
            await _insert_session(
                db, skill_name="trend-skill", status="failed",
                started_at=f"2026-03-09T10:0{i}:00",
            )
        await db.commit()

        report = await analyzer.analyze(db, "trend-skill")
        assert report.trend == SkillTrend.IMPROVING

    @pytest.mark.asyncio
    async def test_trend_declining(self, db):
        analyzer = SkillEffectivenessAnalyzer()
        # Newer sessions: all failed, older: all succeeded
        for i in range(5):
            await _insert_session(
                db, skill_name="decline-skill", status="failed",
                started_at=f"2026-03-10T10:0{i}:00",
            )
        for i in range(5):
            await _insert_session(
                db, skill_name="decline-skill", status="completed",
                started_at=f"2026-03-09T10:0{i}:00",
            )
        await db.commit()

        report = await analyzer.analyze(db, "decline-skill")
        assert report.trend == SkillTrend.DECLINING

    @pytest.mark.asyncio
    async def test_baseline_computation(self, db):
        # Sessions WITH skill
        for _ in range(3):
            await _insert_session(
                db, skill_name="baseline-skill", status="completed",
                session_type="background_reflection",
            )
        # Sessions WITHOUT skill (same session_type)
        for _ in range(4):
            sid = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            await db.execute(
                """INSERT INTO cc_sessions
                   (id, session_type, model, effort, status, started_at,
                    last_activity_at, source_tag, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, "background_reflection", "m", "medium", "completed", now, now,
                 "bg", json.dumps({"other": True})),
            )
        for _ in range(6):
            sid = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            await db.execute(
                """INSERT INTO cc_sessions
                   (id, session_type, model, effort, status, started_at,
                    last_activity_at, source_tag, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, "background_reflection", "m", "medium", "failed", now, now,
                 "bg", json.dumps({"other": True})),
            )
        await db.commit()

        analyzer = SkillEffectivenessAnalyzer()
        report = await analyzer.analyze(db, "baseline-skill")
        # Baseline: 4 completed / 10 total = 0.4
        assert report.baseline_success_rate == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Refiner tests
# ---------------------------------------------------------------------------

class TestRefiner:
    def test_parse_response_valid_json(self):
        refiner = SkillRefiner()
        text = json.dumps({
            "proposed_content": "new content",
            "rationale": "better",
            "change_size": "minor",
            "confidence": 0.9,
            "failure_patterns_addressed": ["timeout"],
        })
        result = refiner._parse_response("test", text)
        assert result is not None
        assert result.proposed_content == "new content"
        assert result.change_size == ChangeSize.MINOR
        assert result.confidence == 0.9

    def test_parse_response_code_block(self):
        refiner = SkillRefiner()
        text = '```json\n{"proposed_content": "x", "rationale": "y", "change_size": "moderate"}\n```'
        result = refiner._parse_response("test", text)
        assert result is not None
        assert result.change_size == ChangeSize.MODERATE

    def test_parse_response_malformed(self):
        refiner = SkillRefiner()
        result = refiner._parse_response("test", "not json at all")
        assert result is None

    def test_parse_response_empty_content(self):
        refiner = SkillRefiner()
        text = json.dumps({"proposed_content": "", "rationale": "x", "change_size": "minor"})
        result = refiner._parse_response("test", text)
        assert result is None

    def test_parse_response_invalid_change_size(self):
        refiner = SkillRefiner()
        text = json.dumps({
            "proposed_content": "x", "rationale": "y", "change_size": "huge",
        })
        result = refiner._parse_response("test", text)
        assert result is not None
        assert result.change_size == ChangeSize.MINOR  # fallback

    def test_build_prompt_progressive_disclosure(self):
        refiner = SkillRefiner()
        report = SkillReport(
            skill_name="big", usage_count=10, success_count=8,
            failure_count=2, success_rate=0.8,
        )
        long_content = "line\n" * 600
        prompt = refiner._build_prompt(report, long_content)
        assert "over 500 lines" in prompt

    def test_build_prompt_short_no_warning(self):
        refiner = SkillRefiner()
        report = SkillReport(
            skill_name="small", usage_count=5, success_count=5,
            failure_count=0, success_rate=1.0,
        )
        prompt = refiner._build_prompt(report, "short content")
        assert "over 500 lines" not in prompt

    def test_build_prompt_with_baseline(self):
        refiner = SkillRefiner()
        report = SkillReport(
            skill_name="x", usage_count=10, success_count=8,
            failure_count=2, success_rate=0.8,
            baseline_success_rate=0.6,
        )
        prompt = refiner._build_prompt(report, "content")
        assert "Baseline: 60.0%" in prompt

    @pytest.mark.asyncio
    async def test_propose_no_router(self):
        refiner = SkillRefiner()
        report = SkillReport(
            skill_name="x", usage_count=1, success_count=1,
            failure_count=0, success_rate=1.0,
        )
        result = await refiner.propose(report, "content", router=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_propose_with_router(self):
        refiner = SkillRefiner()
        report = SkillReport(
            skill_name="x", usage_count=5, success_count=3,
            failure_count=2, success_rate=0.6,
        )

        mock_result = MagicMock()
        mock_result.text = json.dumps({
            "proposed_content": "improved",
            "rationale": "fixed issues",
            "change_size": "minor",
            "confidence": 0.85,
        })
        router = MagicMock()
        router.route_call = AsyncMock(return_value=mock_result)

        result = await refiner.propose(report, "old content", router=router)
        assert result is not None
        assert result.proposed_content == "improved"
        router.route_call.assert_awaited_once()


# ---------------------------------------------------------------------------
# Applicator tests
# ---------------------------------------------------------------------------

class TestApplicator:
    @pytest.mark.asyncio
    async def test_apply_minor_auto_applies(self, db, tmp_path):
        # Create a skill directory
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

        # Mock validator to pass (this test is about applicator logic, not validation)
        from genesis.learning.skills.types import ValidationResult
        applicator._validator.validate = lambda *a, **kw: ValidationResult(
            passed=True, test_results={}, blocking_failures=[], warnings=[],
        )

        # Patch get_skill_path to use tmp_path
        import genesis.learning.skills.wiring as wiring_mod
        original = wiring_mod.get_skill_path

        def mock_get_path(name):
            p = skill_dir / "SKILL.md"
            return p if p.exists() else None

        wiring_mod.get_skill_path = mock_get_path
        try:
            result = await applicator.apply(proposal, db)
        finally:
            wiring_mod.get_skill_path = original

        assert result["action"] == "applied"
        assert (skill_dir / "SKILL.md").read_text() == "new content"

    @pytest.mark.asyncio
    async def test_apply_minor_skill_not_found(self, db):
        proposal = SkillProposal(
            skill_name="nonexistent",
            proposed_content="x",
            rationale="r",
            change_size=ChangeSize.MINOR,
        )

        applicator = SkillApplicator(autonomy_level=2)

        # Mock validator to pass so we test the path-not-found logic
        from genesis.learning.skills.types import ValidationResult
        applicator._validator.validate = lambda *a, **kw: ValidationResult(
            passed=True, test_results={}, blocking_failures=[], warnings=[],
        )

        import genesis.learning.skills.wiring as wiring_mod
        original = wiring_mod.get_skill_path
        wiring_mod.get_skill_path = lambda _: None
        try:
            result = await applicator.apply(proposal, db)
        finally:
            wiring_mod.get_skill_path = original

        assert result["action"] == "failed"

    @pytest.mark.asyncio
    async def test_apply_moderate_staged(self, db):
        proposal = SkillProposal(
            skill_name="test-skill",
            proposed_content="big changes",
            rationale="overhaul",
            change_size=ChangeSize.MODERATE,
        )

        applicator = SkillApplicator(autonomy_level=2)
        result = await applicator.apply(proposal, db)

        assert result["action"] == "staged"
        assert result["validated"] is False  # no router means unvalidated

    @pytest.mark.asyncio
    async def test_apply_moderate_with_router_validation(self, db):
        proposal = SkillProposal(
            skill_name="test-skill",
            proposed_content="big changes",
            rationale="overhaul",
            change_size=ChangeSize.MODERATE,
        )

        mock_result = MagicMock()
        mock_result.text = json.dumps({"approved": True, "reason": "looks good"})
        router = MagicMock()
        router.route_call = AsyncMock(return_value=mock_result)

        applicator = SkillApplicator(autonomy_level=2)
        result = await applicator.apply(proposal, db, router=router)

        assert result["action"] == "staged"
        assert result["validated"] is True

    @pytest.mark.asyncio
    async def test_apply_major_high_priority(self, db):
        proposal = SkillProposal(
            skill_name="test-skill",
            proposed_content="major rewrite",
            rationale="total rework",
            change_size=ChangeSize.MAJOR,
        )

        applicator = SkillApplicator(autonomy_level=2)
        result = await applicator.apply(proposal, db)
        assert result["action"] == "staged"

        # Check observation was created with high priority
        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'skill_proposal' ORDER BY created_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert dict(row)["priority"] == "high"

    @pytest.mark.asyncio
    async def test_apply_minor_low_autonomy_stages(self, db):
        """MINOR changes are staged when autonomy < 2."""
        proposal = SkillProposal(
            skill_name="test-skill",
            proposed_content="x",
            rationale="r",
            change_size=ChangeSize.MINOR,
        )

        applicator = SkillApplicator(autonomy_level=1)
        result = await applicator.apply(proposal, db)
        assert result["action"] == "staged"

    @pytest.mark.asyncio
    async def test_validate_approved(self):
        mock_result = MagicMock()
        mock_result.text = json.dumps({"approved": True, "reason": "ok"})
        router = MagicMock()
        router.route_call = AsyncMock(return_value=mock_result)

        applicator = SkillApplicator()
        proposal = SkillProposal(
            skill_name="x", proposed_content="c",
            rationale="r", change_size=ChangeSize.MODERATE,
        )
        assert await applicator._llm_validate(proposal, router=router) is True

    @pytest.mark.asyncio
    async def test_validate_rejected(self):
        mock_result = MagicMock()
        mock_result.text = json.dumps({"approved": False, "reason": "bad"})
        router = MagicMock()
        router.route_call = AsyncMock(return_value=mock_result)

        applicator = SkillApplicator()
        proposal = SkillProposal(
            skill_name="x", proposed_content="c",
            rationale="r", change_size=ChangeSize.MODERATE,
        )
        assert await applicator._llm_validate(proposal, router=router) is False

    @pytest.mark.asyncio
    async def test_validate_error_returns_false(self):
        router = MagicMock()
        router.route_call = AsyncMock(side_effect=RuntimeError("fail"))

        applicator = SkillApplicator()
        proposal = SkillProposal(
            skill_name="x", proposed_content="c",
            rationale="r", change_size=ChangeSize.MODERATE,
        )
        assert await applicator._llm_validate(proposal, router=router) is False


# ---------------------------------------------------------------------------
# Wiring function tests
# ---------------------------------------------------------------------------

class TestWiringFunctions:
    def test_write_skill(self, tmp_path):
        from genesis.learning.skills import wiring as wiring_mod

        original_dir = wiring_mod._GENESIS_SKILLS_DIR
        wiring_mod._GENESIS_SKILLS_DIR = tmp_path

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()

        try:
            assert wiring_mod.write_skill("my-skill", "hello") is True
            assert (skill_dir / "SKILL.md").read_text() == "hello"
        finally:
            wiring_mod._GENESIS_SKILLS_DIR = original_dir

    def test_write_skill_missing_dir(self, tmp_path):
        from genesis.learning.skills import wiring as wiring_mod

        original_dir = wiring_mod._GENESIS_SKILLS_DIR
        wiring_mod._GENESIS_SKILLS_DIR = tmp_path

        try:
            assert wiring_mod.write_skill("nonexistent", "hello") is False
        finally:
            wiring_mod._GENESIS_SKILLS_DIR = original_dir

    def test_read_skill(self, tmp_path):
        from genesis.learning.skills import wiring as wiring_mod

        original_dir = wiring_mod._GENESIS_SKILLS_DIR
        wiring_mod._GENESIS_SKILLS_DIR = tmp_path

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("content here")

        try:
            assert wiring_mod.read_skill("my-skill") == "content here"
            assert wiring_mod.read_skill("missing") is None
        finally:
            wiring_mod._GENESIS_SKILLS_DIR = original_dir
