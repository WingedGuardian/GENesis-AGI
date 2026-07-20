"""Tests for the shadow skill-edit Critic (WS1).

Two layers:
  * run_critic() unit behavior — verdict mapping, degrade-to-NULL, gating.
  * applicator wiring — the SHADOW INVARIANT: the Critic logs an observation
    but NEVER changes whether a MINOR edit applies, even when it raises.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite
import pytest

from genesis.learning.skills import skill_edit_critic as sec
from genesis.learning.skills.applicator import SkillApplicator
from genesis.learning.skills.types import ChangeSize, SkillProposal, ValidationResult
from genesis.routing.types import RoutingResult


@dataclass
class StubRouter:
    """Canned RoutingResult for the judge call. Set ``raises=True`` to make
    route_call itself throw (simulates infra exploding inside score_async)."""

    response_content: str = '{"score": 0.9, "rationale": "clean", "pathologies": []}'
    success: bool = True
    error: str | None = None
    raises: bool = False

    async def route_call(self, call_site_id, messages, **kwargs):
        if self.raises:
            raise RuntimeError("judge infra down")
        return RoutingResult(
            success=self.success,
            call_site_id=call_site_id,
            content=self.response_content if self.success else None,
            model_id="m" if self.success else None,
            provider_used="p" if self.success else None,
            error=self.error,
        )


def _proposal(proposed: str = "new body", rationale: str = "r") -> SkillProposal:
    return SkillProposal(
        skill_name="voice-master",
        proposed_content=proposed,
        rationale=rationale,
        change_size=ChangeSize.MINOR,
    )


@pytest.fixture(autouse=True)
def _gate_shadow_on(monkeypatch):
    """Default every test to gate=shadow, env-kill off, so the judge path runs.
    Individual gating tests override these."""
    monkeypatch.setattr(sec, "skill_gate_off", lambda: False)
    monkeypatch.setattr(sec, "skill_gate_mode", lambda: "shadow")


# ── run_critic verdict mapping ──────────────────────────────────────────


async def test_clean_verdict():
    critic = await sec.run_critic(
        current_content="current body",
        proposal=_proposal(),
        router=StubRouter(response_content='{"score": 0.9, "rationale": "ok", "pathologies": []}'),
    )
    assert critic["verdict"] == "clean"
    assert critic["score"] == 0.9
    assert critic["pathologies"] == []
    assert critic["rubric_version"] == "1.0.0"


async def test_flagged_verdict_parses_pathologies():
    critic = await sec.run_critic(
        current_content="line one\nremoved guard: do NOT use when X",
        proposal=_proposal(proposed="line one"),
        router=StubRouter(
            response_content=(
                '{"score": 0.1, "rationale": "strips a guard", '
                '"pathologies": ["constraint_stripping", "made_up_label"]}'
            ),
        ),
    )
    assert critic["verdict"] == "flagged"
    assert critic["score"] == 0.1
    # Unknown labels dropped; known one kept.
    assert critic["pathologies"] == ["constraint_stripping"]


async def test_unavailable_on_call_failure():
    critic = await sec.run_critic(
        current_content="current",
        proposal=_proposal(),
        router=StubRouter(success=False, error="all providers exhausted"),
    )
    assert critic["verdict"] == "unavailable"
    assert critic["error"] == "judge_call_fail"
    # No confident score/pathologies on an outage.
    assert "score" not in critic


async def test_unavailable_on_parse_failure():
    critic = await sec.run_critic(
        current_content="current",
        proposal=_proposal(),
        router=StubRouter(response_content="not json at all"),
    )
    assert critic["verdict"] == "unavailable"
    assert critic["error"] == "judge_parse_fail"


async def test_unavailable_on_scorer_exception():
    """route_call raising propagates through score_async — run_critic must
    catch it and record unavailable, never re-raise."""
    critic = await sec.run_critic(
        current_content="current",
        proposal=_proposal(),
        router=StubRouter(raises=True),
    )
    assert critic["verdict"] == "unavailable"
    assert critic["error"] == "scorer_exception"


# ── gating: nothing to log ──────────────────────────────────────────────


async def test_env_kill_returns_none(monkeypatch):
    monkeypatch.setattr(sec, "skill_gate_off", lambda: True)
    critic = await sec.run_critic(
        current_content="current",
        proposal=_proposal(),
        router=StubRouter(),
    )
    assert critic is None


async def test_mode_off_returns_none(monkeypatch):
    monkeypatch.setattr(sec, "skill_gate_mode", lambda: "off")
    critic = await sec.run_critic(
        current_content="current",
        proposal=_proposal(),
        router=StubRouter(),
    )
    assert critic is None


async def test_no_router_returns_none():
    critic = await sec.run_critic(
        current_content="current",
        proposal=_proposal(),
        router=None,
    )
    assert critic is None


@pytest.mark.parametrize("content", [None, ""])
async def test_no_current_content_returns_none(content):
    critic = await sec.run_critic(
        current_content=content,
        proposal=_proposal(),
        router=StubRouter(),
    )
    assert critic is None


# ── helpers ─────────────────────────────────────────────────────────────


def test_removed_lines_detects_deletions():
    removed = sec._removed_lines(
        "keep this\ndrop this guard\nkeep that",
        "keep this\nkeep that",
    )
    assert "drop this guard" in removed
    assert "keep this" not in removed


def test_removed_lines_empty_on_pure_addition():
    assert sec._removed_lines("a\nb", "a\nb\nc") == ""


def test_cap_truncates_with_marker():
    text = "x" * 20000
    capped = sec._cap(text, limit=8000)
    assert len(capped) < len(text)
    assert "elided" in capped


def test_cap_passthrough_when_small():
    assert sec._cap("short", limit=8000) == "short"


def test_parse_pathologies_filters_unknown():
    raw = '{"score": 0.2, "pathologies": ["reward_hacking", "bogus"]}'
    assert sec._parse_pathologies(raw) == ["reward_hacking"]


def test_parse_pathologies_tolerates_garbage():
    assert sec._parse_pathologies("not json") == []
    assert sec._parse_pathologies('{"pathologies": "not a list"}') == []


# ── applicator wiring: the SHADOW INVARIANT ─────────────────────────────


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    from genesis.db.schema import create_all_tables, seed_data

    await create_all_tables(conn)
    await seed_data(conn)
    yield conn
    await conn.close()


def _minor_applicator_setup(tmp_path):
    """A MINOR proposal + an applicator whose validator passes and whose
    get_skill_path points at a tmp SKILL.md. Returns (applicator, skill_md,
    proposal, restore_fn)."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("old content\nguard: do NOT use when X")

    proposal = SkillProposal(
        skill_name="test-skill",
        proposed_content="new content",
        rationale="improvement",
        change_size=ChangeSize.MINOR,
    )
    applicator = SkillApplicator(autonomy_level=2)
    applicator._validator.validate = lambda *a, **kw: ValidationResult(
        passed=True,
        test_results={},
        blocking_failures=[],
        warnings=[],
    )

    import genesis.learning.skills.wiring as wiring_mod

    original = wiring_mod.get_skill_path
    wiring_mod.get_skill_path = lambda name: skill_md if skill_md.exists() else None

    def restore():
        wiring_mod.get_skill_path = original

    return applicator, skill_md, proposal, restore


async def _fetch_gate_obs(db):
    cur = await db.execute(
        "SELECT priority, content FROM observations "
        "WHERE source='skill_evolution_gate' AND type='skill_edit_critic'"
    )
    return await cur.fetchall()


async def test_wiring_flagged_verdict_logs_and_still_applies(db, tmp_path, monkeypatch):
    applicator, skill_md, proposal, restore = _minor_applicator_setup(tmp_path)

    async def fake_run_critic(**kwargs):
        return {
            "verdict": "flagged",
            "score": 0.1,
            "pathologies": ["constraint_stripping"],
            "change_size": "minor",
            "rubric_version": "1.0.0",
        }

    monkeypatch.setattr(sec, "run_critic", fake_run_critic)
    try:
        result = await applicator.apply(
            proposal, db, router=StubRouter(), current_content="old content"
        )
    finally:
        restore()

    # Shadow invariant: the edit applied unchanged.
    assert result["action"] == "applied"
    assert skill_md.read_text() == "new content"
    # And a flagged verdict was logged at high priority (visible during bake).
    rows = await _fetch_gate_obs(db)
    assert len(rows) == 1
    assert rows[0]["priority"] == "high"
    assert "constraint_stripping" in rows[0]["content"]


async def test_wiring_clean_verdict_logged_low_priority(db, tmp_path, monkeypatch):
    applicator, skill_md, proposal, restore = _minor_applicator_setup(tmp_path)

    async def fake_run_critic(**kwargs):
        return {"verdict": "clean", "score": 0.95, "pathologies": [], "rubric_version": "1.0.0"}

    monkeypatch.setattr(sec, "run_critic", fake_run_critic)
    try:
        result = await applicator.apply(
            proposal, db, router=StubRouter(), current_content="old content"
        )
    finally:
        restore()

    assert result["action"] == "applied"
    rows = await _fetch_gate_obs(db)
    assert len(rows) == 1
    assert rows[0]["priority"] == "low"


async def test_wiring_critic_raise_does_not_block_edit(db, tmp_path, monkeypatch):
    """B1 regression: a raise from the Critic block must NOT prevent the edit
    (it runs after the file write) and must NOT propagate to abort the batch."""
    applicator, skill_md, proposal, restore = _minor_applicator_setup(tmp_path)

    async def boom(**kwargs):
        raise RuntimeError("judge infra down")

    monkeypatch.setattr(sec, "run_critic", boom)
    try:
        result = await applicator.apply(
            proposal, db, router=StubRouter(), current_content="old content"
        )
    finally:
        restore()

    assert result["action"] == "applied"
    assert skill_md.read_text() == "new content"
    # Critic raised → no gate observation, but the edit still landed.
    assert await _fetch_gate_obs(db) == []


async def test_wiring_none_verdict_logs_nothing(db, tmp_path, monkeypatch):
    """When run_critic returns None (gated off / no router), no observation."""
    applicator, skill_md, proposal, restore = _minor_applicator_setup(tmp_path)

    async def gated(**kwargs):
        return None

    monkeypatch.setattr(sec, "run_critic", gated)
    try:
        result = await applicator.apply(
            proposal, db, router=StubRouter(), current_content="old content"
        )
    finally:
        restore()

    assert result["action"] == "applied"
    assert await _fetch_gate_obs(db) == []
