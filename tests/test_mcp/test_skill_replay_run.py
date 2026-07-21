"""Tests for the skill_replay_run MCP tool (_impl) — gate/validation branches +
happy path with the heavy runner patched. Asserts recommend-only, never mutates."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.eval.skill_replay.types import SkillReplayReport, SkillReplayVerdict
from genesis.mcp.health.skill_replay_run import _impl_skill_replay_run

_FIXTURE = (
    Path(__file__).parent.parent
    / "test_eval"
    / "skill_golden_fixtures"
    / "voice_master_fixture.jsonl"
)


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    # Default: env kill off, replay mode shadow (the gate permits a run).
    monkeypatch.setattr("genesis.env.skill_gate_off", lambda: False)
    monkeypatch.setattr(
        "genesis.learning.skills.skill_gate_config.skill_replay_mode", lambda: "shadow"
    )


async def test_gate_off_returns_skipped(monkeypatch):
    monkeypatch.setattr(
        "genesis.learning.skills.skill_gate_config.skill_replay_mode", lambda: "off"
    )
    res = await _impl_skill_replay_run(skill_name="voice-master")
    assert res["status"] == "skipped"
    assert res["autonomous_action"] is False


async def test_env_kill_returns_skipped(monkeypatch):
    monkeypatch.setattr("genesis.env.skill_gate_off", lambda: True)
    res = await _impl_skill_replay_run(skill_name="voice-master")
    assert res["status"] == "skipped"


async def test_invalid_model_errors():
    res = await _impl_skill_replay_run(skill_name="voice-master", model="bogus")
    assert res["status"] == "error"
    assert "invalid model" in res["message"]
    assert res["autonomous_action"] is False


async def test_unknown_skill_errors():
    res = await _impl_skill_replay_run(skill_name="no-such-skill-xyz", old_content="x")
    assert res["status"] == "error"
    assert "skill not found" in res["message"]


async def test_missing_old_content_errors(monkeypatch):
    # No explicit old_content and no runtime db → no ledger pre-image → error.
    res = await _impl_skill_replay_run(skill_name="voice-master", new_content="NEW")
    assert res["status"] == "error"
    assert "OLD content" in res["message"]


async def test_load_skill_raising_returns_error_not_crash(monkeypatch):
    # SHOULD-FIX 2: an unreadable SKILL.md (bad encoding / TOCTOU) must become a
    # structured error, not an exception out of the tool.
    def boom(_name):
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad")

    monkeypatch.setattr("genesis.learning.skills.wiring.load_skill", boom)
    res = await _impl_skill_replay_run(skill_name="voice-master")
    assert res["status"] == "error"
    assert "could not read current SKILL.md" in res["message"]


async def test_missing_suite_errors():
    res = await _impl_skill_replay_run(
        skill_name="voice-master",
        old_content="OLD",
        new_content="NEW",
        suite_path="/nonexistent/suite.jsonl",
    )
    assert res["status"] == "error"
    assert "golden suite not found" in res["message"]


async def test_happy_path_returns_recommend_only_verdict(monkeypatch):
    fake_report = SkillReplayReport(
        run_id="r1",
        skill_name="voice-master",
        model="sonnet",
        effort="medium",
        task_set_version="fixture_v1",
        task_file_sha256="sha",
        rubric_name="bench_task_success",
        rubric_version="1.0",
        verdict=SkillReplayVerdict(
            verdict="net_positive", n_complete=5, n_regressions=0, n_improvements=2, note="ok"
        ),
    )

    async def fake_run(**kwargs):
        return fake_report

    monkeypatch.setattr("genesis.eval.skill_replay.runner.run_skill_replay", fake_run)

    res = await _impl_skill_replay_run(
        skill_name="voice-master",
        old_content="OLD",
        new_content="NEW",
        suite_path=str(_FIXTURE),
    )
    assert res["status"] == "ok"
    assert res["verdict"] == "net_positive"
    assert res["n_improvements"] == 2
    assert res["autonomous_action"] is False
    assert res["recommend_only"] is True
