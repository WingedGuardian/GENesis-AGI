"""Tests for DirectSessionRequest and planning instruction behavior."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
from genesis.cc.session_manager import SessionManager
from genesis.db.crud import cc_sessions


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


class TestSpawnRecordsSkillSignal:
    """spawn() must record the resolved skills into session metadata so the
    skill-evolution effectiveness analyzer has usage signal. Regression guard
    for the bug where skills were injected into the prompt but never recorded
    (background_task sessions had zero skill_tags → analyzer never fired)."""

    async def test_spawn_records_resolved_skills_in_metadata(self, db):
        sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
        runner = DirectSessionRunner(
            invoker=AsyncMock(),
            session_manager=sm,
            config_builder=AsyncMock(),
            runtime=object(),  # no _autonomy_manager attr -> ceiling check skipped
        )
        # Neutralize the fire-and-forget run so no real CC invocation happens.
        runner._run_session = lambda _req, _sid: asyncio.sleep(0)

        req = DirectSessionRequest(
            prompt="draft a post",
            profile="research",
            skills=["voice-master", "research"],
        )
        sid = await runner.spawn(req)

        try:
            row = await cc_sessions.get_by_id(db, sid)
            assert row is not None
            meta = json.loads(row["metadata"])
            assert meta["skill_tags"] == ["voice-master", "research"]
            # The analyzer matches via `metadata LIKE '%"<skill>"%'` — confirm
            # the persisted JSON shape actually satisfies that query.
            assert '"voice-master"' in row["metadata"]
        finally:
            t = runner._active.get(sid)
            if t is not None:
                await asyncio.gather(t, return_exceptions=True)

    async def test_effectiveness_analyzer_sees_tagged_session(self, db):
        """E2E: a skill-tagged session is visible to the skill-evolution
        effectiveness analyzer (closes the Phase A loop — tagging produces
        non-zero usage signal where before there was none)."""
        from genesis.cc.types import CCModel, EffortLevel, SessionType
        from genesis.learning.skills.effectiveness import SkillEffectivenessAnalyzer

        sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
        sess = await sm.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            skill_tags=["voice-master"],
        )
        await cc_sessions.update_status(db, sess["id"], status="completed")

        report = await SkillEffectivenessAnalyzer().analyze(db, "voice-master")
        assert report.usage_count >= 1
        assert report.success_count >= 1

    async def test_skill_tags_survive_store_result_merge(self, db):
        """_store_result is read-merge-write; skill_tags set at creation must
        survive session completion (the analyzer reads completed sessions).
        Guards against a refactor that replaces metadata wholesale."""
        from types import SimpleNamespace

        from genesis.cc.direct_session import DirectSessionResult
        from genesis.cc.types import CCModel, EffortLevel, SessionType

        sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
        runner = DirectSessionRunner(
            invoker=AsyncMock(),
            session_manager=sm,
            config_builder=AsyncMock(),
            runtime=SimpleNamespace(_db=db),  # _store_result reads rt._db
        )
        sess = await sm.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            skill_tags=["voice-master"],
        )
        req = DirectSessionRequest(
            prompt="x", profile="research", skills=["voice-master"],
        )
        result = DirectSessionResult(
            session_id=sess["id"], success=True, output_text="done",
        )
        await runner._store_result(sess["id"], req, result)

        row = await cc_sessions.get_by_id(db, sess["id"])
        meta = json.loads(row["metadata"])
        assert meta["skill_tags"] == ["voice-master"]  # survived the merge
        assert meta["output_text"] == "done"  # merge actually ran

    async def test_spawn_records_auto_resolved_profile_skills(self, db):
        """Auto-resolved (profile-bound) skills are recorded too, not just
        explicit request.skills. The campaign profile injects voice-master."""
        sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
        runner = DirectSessionRunner(
            invoker=AsyncMock(),
            session_manager=sm,
            config_builder=AsyncMock(),
            runtime=object(),
        )
        runner._run_session = lambda _req, _sid: asyncio.sleep(0)
        req = DirectSessionRequest(prompt="x", profile="campaign")  # no explicit skills
        sid = await runner.spawn(req)
        try:
            row = await cc_sessions.get_by_id(db, sid)
            meta = json.loads(row["metadata"])
            assert "voice-master" in meta.get("skill_tags", [])
        finally:
            t = runner._active.get(sid)
            if t is not None:
                await asyncio.gather(t, return_exceptions=True)

    async def test_profile_name_collision_not_counted_as_skill(self, db):
        """Regression guard: 'research' is both a profile and a skill. A
        research-PROFILE session (which injects no skills) must NOT be counted
        as research-SKILL usage by the effectiveness analyzer — only skill_tags
        membership counts, not a loose metadata substring match."""
        from genesis.cc.types import CCModel, EffortLevel, SessionType
        from genesis.learning.skills.effectiveness import SkillEffectivenessAnalyzer

        sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
        sess = await sm.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            profile="research",  # writes {"profile": "research"} to metadata
            skill_tags=[],  # research profile injects no skills
        )
        await cc_sessions.update_status(db, sess["id"], status="completed")

        report = await SkillEffectivenessAnalyzer().analyze(db, "research")
        assert report.usage_count == 0  # profile match must not count as skill usage


class TestBackgroundFallbackRecovery:
    """A successful background run clears account-wide CC fallback only when it ran
    on the HOME model (the rate-limited one recorded at failover, state.original) —
    which may be a roster PEER when the default is non-Claude. A success on any
    OTHER model must NOT clear. GENESIS_HOME is redirected so the real state file is
    untouched."""

    async def _run(self, db, tmp_path, monkeypatch, *, home: str, roster_model: str):
        from types import SimpleNamespace

        from genesis.cc import fallback_state
        from genesis.cc.types import CCInvocation, CCModel, CCOutput, EffortLevel, SessionType

        monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
        fallback_state.clear()
        peer = "glm-5.2" if home == "claude" else "claude"
        fallback_state.enter(home, peer, "rate_limit")  # original=home

        sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
        invoker = AsyncMock()
        invoker.run_streaming = AsyncMock(return_value=CCOutput(
            session_id="cc-bg", text="done", model_used=roster_model or "claude",
            cost_usd=0.0, input_tokens=1, output_tokens=1, duration_ms=1, exit_code=0,
            is_error=False, roster_model=roster_model,
        ))
        runner = DirectSessionRunner(
            invoker=invoker, session_manager=sm, config_builder=AsyncMock(),
            runtime=SimpleNamespace(_db=db),
        )
        runner._build_invocation = lambda _req: CCInvocation(prompt="x")
        sess = await sm.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=CCModel.SONNET, effort=EffortLevel.MEDIUM,
        )
        result = await runner._run_session(DirectSessionRequest(prompt="t"), sess["id"])
        assert result.success is True
        state = fallback_state.read()
        fallback_state.clear()
        return state

    async def test_home_claude_success_clears(self, db, tmp_path, monkeypatch):
        state = await self._run(db, tmp_path, monkeypatch, home="claude", roster_model="claude")
        assert state.is_fallback is False  # home (claude) success → cleared

    async def test_peer_success_with_claude_home_does_not_clear(self, db, tmp_path, monkeypatch):
        state = await self._run(db, tmp_path, monkeypatch, home="claude", roster_model="glm-5.2")
        assert state.is_fallback is True  # glm run doesn't prove claude back

    async def test_home_peer_success_clears(self, db, tmp_path, monkeypatch):
        # default=peer: home is glm-5.2; a successful glm run is the recovery signal.
        state = await self._run(db, tmp_path, monkeypatch, home="glm-5.2", roster_model="glm-5.2")
        assert state.is_fallback is False

    async def test_native_claude_success_with_peer_home_does_not_clear(self, db, tmp_path, monkeypatch):
        # The bug case: home is glm-5.2 (down); an intentional native-Claude run
        # succeeds but must NOT clear the glm fallback (Claude is ~always up).
        state = await self._run(db, tmp_path, monkeypatch, home="glm-5.2", roster_model="claude")
        assert state.is_fallback is True
