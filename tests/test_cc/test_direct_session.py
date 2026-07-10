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
        runner._build_invocation = lambda _req, _sid: CCInvocation(prompt="x")
        sess = await sm.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=CCModel.SONNET, effort=EffortLevel.MEDIUM,
        )
        result = await runner._run_session(DirectSessionRequest(prompt="t"), sess["id"])
        assert result.success is True
        state = fallback_state.read()
        fallback_state.clear()
        return state

    @pytest.mark.asyncio
    async def test_home_claude_success_clears(self, db, tmp_path, monkeypatch):
        state = await self._run(db, tmp_path, monkeypatch, home="claude", roster_model="claude")
        assert state.is_fallback is False  # home (claude) success → cleared

    @pytest.mark.asyncio
    async def test_peer_success_with_claude_home_does_not_clear(self, db, tmp_path, monkeypatch):
        state = await self._run(db, tmp_path, monkeypatch, home="claude", roster_model="glm-5.2")
        assert state.is_fallback is True  # glm run doesn't prove claude back

    @pytest.mark.asyncio
    async def test_home_peer_success_clears(self, db, tmp_path, monkeypatch):
        # default=peer: home is glm-5.2; a successful glm run is the recovery signal.
        state = await self._run(db, tmp_path, monkeypatch, home="glm-5.2", roster_model="glm-5.2")
        assert state.is_fallback is False

    @pytest.mark.asyncio
    async def test_native_claude_success_with_peer_home_does_not_clear(self, db, tmp_path, monkeypatch):
        # The bug case: home is glm-5.2 (down); an intentional native-Claude run
        # succeeds but must NOT clear the glm fallback (Claude is ~always up).
        state = await self._run(db, tmp_path, monkeypatch, home="glm-5.2", roster_model="claude")
        assert state.is_fallback is True


@pytest.mark.asyncio
async def test_run_session_isolates_and_cleans_sandbox(db, tmp_path, monkeypatch):
    """E2E lifecycle: _run_session creates the per-session CC sandbox OFF the
    watchgod-policed cc-tmp BEFORE invoking CC, and removes it in the finally
    afterward. The stub run_streaming asserts the dir exists at invocation time,
    proving the mkdir-before-run ordering; the post-return check proves cleanup.
    """
    from pathlib import Path
    from types import SimpleNamespace

    from genesis.cc.direct_session import _bg_session_root, _bg_session_sandbox
    from genesis.cc.types import CCInvocation, CCModel, CCOutput, EffortLevel, SessionType

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    captured: dict = {}

    async def _check_sandbox_live(inv, on_event=None):
        # Called by _run_session — the sandbox must already exist here.
        p = Path(inv.claude_code_tmpdir)
        captured["existed_at_run"] = p.exists()
        captured["path"] = str(p)
        return CCOutput(
            session_id="cc-bg", text="done", model_used="sonnet",
            cost_usd=0.0, input_tokens=1, output_tokens=1, duration_ms=1,
            exit_code=0, is_error=False, roster_model=None,
        )

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    invoker = AsyncMock()
    invoker.run_streaming = _check_sandbox_live
    runner = DirectSessionRunner(
        invoker=invoker, session_manager=sm, config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db),
    )
    # Mirror the real _build_invocation: wire the per-session sandbox tmpdir.
    runner._build_invocation = lambda _req, sid: CCInvocation(
        prompt="x", claude_code_tmpdir=_bg_session_sandbox(sid),
    )
    sess = await sm.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET, effort=EffortLevel.MEDIUM,
    )
    result = await runner._run_session(DirectSessionRequest(prompt="t"), sess["id"])

    assert result.success is True
    # mkdir ran before the CC invocation, in a dir OFF cc-tmp
    assert captured["existed_at_run"] is True
    assert ".genesis/cc-tmp" not in captured["path"]
    assert "bg-cc-sessions" in captured["path"]
    # finally cleanup removed the whole per-session tree
    assert not _bg_session_root(sess["id"]).exists()


@pytest.mark.asyncio
async def test_run_session_cancelled_marks_failed(db):
    """T2-B: a cancelled session must not linger 'active'.

    CancelledError is a BaseException, so the ``except Exception`` failure
    path never saw it — the row stayed 'active' until the stale reaper
    swept it (historically relabeling it 'completed', i.e. a crash
    masquerading as success in J-9's success rates).
    """
    from types import SimpleNamespace

    from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    invoker = AsyncMock()
    # Cancellation delivered at the await point inside _run_session's try
    invoker.run_streaming = AsyncMock(side_effect=asyncio.CancelledError())
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=sm,
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db),
    )
    runner._build_invocation = lambda _req, _sid: CCInvocation(prompt="x")
    sess = await sm.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
    )

    with pytest.raises(asyncio.CancelledError):
        await runner._run_session(DirectSessionRequest(prompt="t"), sess["id"])

    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["status"] == "failed", (
        f"cancelled session left status={row['status']!r} — must be terminal"
    )


@pytest.mark.asyncio
async def test_runner_shutdown_cancels_and_persists_failed(db):
    """Review P2: runtime shutdown must cancel-and-await in-flight session
    tasks BEFORE the DB closes, so the CancelledError handler can persist a
    terminal status. Without this, `systemctl restart` tears the loop down
    after the DB is gone and rows stay 'active'."""
    from types import SimpleNamespace

    from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    invoker = AsyncMock()

    async def _blocks_forever(inv, on_event=None):
        await asyncio.Event().wait()  # never set — a wedged CC child

    invoker.run_streaming = _blocks_forever
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=sm,
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db),
    )
    runner._build_invocation = lambda _req, _sid: CCInvocation(prompt="x")
    sess = await sm.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
    )
    task = asyncio.create_task(
        runner._run_session(DirectSessionRequest(prompt="t"), sess["id"]),
    )
    runner._active[sess["id"]] = task
    await asyncio.sleep(0.05)  # let the task enter run_streaming

    stopped = await runner.shutdown()

    assert stopped == 1
    assert task.done()
    row = await cc_sessions.get_by_id(db, sess["id"])
    assert row["status"] == "failed", (
        f"in-flight session left status={row['status']!r} after shutdown"
    )


@pytest.mark.asyncio
async def test_run_session_cancelled_records_proposal_outcome(db):
    """Review P3: the CancelledError path must feed the outcome back to an
    ego proposal, matching the generic failure path."""
    from types import SimpleNamespace

    from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    invoker = AsyncMock()
    invoker.run_streaming = AsyncMock(side_effect=asyncio.CancelledError())
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=sm,
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db),
    )
    runner._build_invocation = lambda _req, _sid: CCInvocation(prompt="x")
    runner._record_proposal_outcome = AsyncMock()
    sess = await sm.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
    )

    with pytest.raises(asyncio.CancelledError):
        await runner._run_session(DirectSessionRequest(prompt="t"), sess["id"])

    runner._record_proposal_outcome.assert_awaited_once()
    result = runner._record_proposal_outcome.await_args.args[1]
    assert result.success is False
