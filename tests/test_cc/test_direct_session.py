"""Tests for DirectSessionRequest and planning instruction behavior."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from genesis.cc.direct_session import (
    DirectSessionRequest,
    DirectSessionResult,
    DirectSessionRunner,
)
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
            prompt="x",
            profile="research",
            skills=["voice-master"],
        )
        result = DirectSessionResult(
            session_id=sess["id"],
            success=True,
            output_text="done",
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
        invoker.run_streaming = AsyncMock(
            return_value=CCOutput(
                session_id="cc-bg",
                text="done",
                model_used=roster_model or "claude",
                cost_usd=0.0,
                input_tokens=1,
                output_tokens=1,
                duration_ms=1,
                exit_code=0,
                is_error=False,
                roster_model=roster_model,
            )
        )
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
    async def test_native_claude_success_with_peer_home_does_not_clear(
        self, db, tmp_path, monkeypatch
    ):
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
            session_id="cc-bg",
            text="done",
            model_used="sonnet",
            cost_usd=0.0,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            exit_code=0,
            is_error=False,
            roster_model=None,
        )

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    invoker = AsyncMock()
    invoker.run_streaming = _check_sandbox_live
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=sm,
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db),
    )
    # Mirror the real _build_invocation: wire the per-session sandbox tmpdir.
    runner._build_invocation = lambda _req, sid: CCInvocation(
        prompt="x",
        claude_code_tmpdir=_bg_session_sandbox(sid),
    )
    sess = await sm.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
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


# --- post-dispatch verification routing (advisory vs hard-fail) ---


def _verif_runner(db):
    from types import SimpleNamespace

    return DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=AsyncMock(),
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db, _memory_store=None),
    )


@pytest.mark.asyncio
async def test_verify_proposal_outputs_content_miss_is_advisory(db, tmp_path):
    """A file that exists but misses a required string → advisory, not missing."""
    from genesis.db.crud.ego import create_proposal

    f = tmp_path / "deliverable.md"
    f.write_text("A real report body that addresses the ask.\n")
    await create_proposal(
        db,
        id="prop-adv",
        action_type="dispatch",
        content="x",
        status="executed",
        expected_outputs=json.dumps({"files": [str(f)], "required_strings": ["## Summary"]}),
    )
    vres = await _verif_runner(db)._verify_proposal_outputs(db, "prop-adv")
    assert vres is not None
    assert vres.missing_files == []
    assert vres.advisories  # a note, but not a failure


@pytest.mark.asyncio
async def test_record_outcome_advisory_keeps_executed(db, tmp_path):
    """A content-only miss keeps the proposal 'executed' (positive learning
    signal via |completed:), NOT flipped to 'failed'."""
    from genesis.db.crud.ego import create_proposal, get_proposal

    f = tmp_path / "deliverable.md"
    f.write_text("A real report body.\n")
    await create_proposal(
        db,
        id="prop-exec",
        action_type="dispatch",
        content="x",
        status="executed",
        expected_outputs=json.dumps({"files": [str(f)], "required_strings": ["## Summary"]}),
    )
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-exec")
    res = DirectSessionResult(session_id="s1", success=True, output_text="done")
    await _verif_runner(db)._record_proposal_outcome(req, res)
    prop = await get_proposal(db, "prop-exec")
    assert prop["status"] == "executed"
    assert "|completed:" in (prop["user_response"] or "")


@pytest.mark.asyncio
async def test_record_outcome_long_output_preserves_advisory(db, tmp_path):
    """A long output_text must not truncate the advisory note off the summary —
    the advisory is budgeted into the 1000-char cap."""
    from genesis.db.crud.ego import create_proposal, get_proposal

    f = tmp_path / "deliverable.md"
    f.write_text("A real report body.\n")
    await create_proposal(
        db,
        id="prop-long",
        action_type="dispatch",
        content="x",
        status="executed",
        expected_outputs=json.dumps({"files": [str(f)], "required_strings": ["## Not Present"]}),
    )
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-long")
    res = DirectSessionResult(session_id="s3", success=True, output_text="x" * 1500)
    await _verif_runner(db)._record_proposal_outcome(req, res)
    prop = await get_proposal(db, "prop-long")
    ur = prop["user_response"] or ""
    assert prop["status"] == "executed"
    assert "|completed:" in ur  # polarity preserved
    assert "verification advisories" in ur  # advisory survived the truncation


@pytest.mark.asyncio
async def test_record_outcome_missing_file_marks_failed(db, tmp_path):
    """A genuinely missing deliverable still hard-fails (regression guard)."""
    from genesis.db.crud.ego import create_proposal, get_proposal

    await create_proposal(
        db,
        id="prop-miss",
        action_type="dispatch",
        content="x",
        status="executed",
        expected_outputs=json.dumps({"files": [str(tmp_path / "never-written.md")]}),
    )
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-miss")
    res = DirectSessionResult(session_id="s2", success=True, output_text="claims done")
    await _verif_runner(db)._record_proposal_outcome(req, res)
    prop = await get_proposal(db, "prop-miss")
    assert prop["status"] == "failed"
    assert "verification_failed" in (prop["user_response"] or "")


# --- dispatch-outcome recall visibility (B2a) ---
#
# Dispatch outcomes must be RETRIEVABLE by default recall / the proactive hook so
# the ego (and CC sessions) can recall what happened to a dispatch. Two gaps this
# locks: (1) a SUCCESSFUL dispatch wrote no memory at all; (2) the failure memory
# was tagged source_subsystem="ego", which EXCLUDES it from default recall. The
# fix stores both polarities WITHOUT source_subsystem (operational history, not
# internal decisional output — classified in test_store_subsystem_coverage's
# USER_CONTEXT_ALLOWLIST).


class _RecordingStore:
    """Fake MemoryStore implementing the real .store(**kwargs) contract."""

    def __init__(self):
        self.calls: list[dict] = []

    async def store(self, **kwargs):
        self.calls.append(kwargs)
        return "mem-id"


def _recording_runner(db, store):
    from types import SimpleNamespace

    return DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=AsyncMock(),
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db, _memory_store=store),
    )


@pytest.mark.asyncio
async def test_record_outcome_success_writes_recallable_memory(db):
    """A successful dispatch must write a memory tagged dispatch_success, and it
    must be recallable (NO source_subsystem, else default recall excludes it)."""
    from genesis.db.crud.ego import create_proposal

    store = _RecordingStore()
    await create_proposal(db, id="prop-ok", action_type="dispatch", content="x", status="executed")
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-ok")
    res = DirectSessionResult(session_id="s-ok", success=True, output_text="all done")
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    succ = [c for c in store.calls if "dispatch_success" in (c.get("tags") or [])]
    assert len(succ) == 1, "a successful dispatch must write exactly one outcome memory"
    assert "prop-ok" in succ[0].get("content", "")
    assert succ[0].get("source_subsystem") is None, (
        "dispatch outcome memories must be recallable — no source_subsystem tag"
    )


@pytest.mark.asyncio
async def test_record_outcome_failure_memory_is_recallable(db):
    """A failed dispatch's memory must also be recallable (untagged), symmetric
    with success — today it is tagged source_subsystem='ego' and hidden."""
    from genesis.db.crud.ego import create_proposal

    store = _RecordingStore()
    await create_proposal(db, id="prop-bad", action_type="dispatch", content="x", status="executed")
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-bad")
    res = DirectSessionResult(session_id="s-bad", success=False, output_text="it broke")
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    fail = [c for c in store.calls if "dispatch_failure" in (c.get("tags") or [])]
    assert len(fail) == 1, "a failed dispatch must write exactly one outcome memory"
    assert fail[0].get("source_subsystem") is None, (
        "dispatch outcome memories must be recallable — no source_subsystem tag"
    )


# --- #1487 P2 polish: proposal subject in the outcome memory + cancel ordering ---


@pytest.mark.asyncio
async def test_record_outcome_content_includes_proposal_subject(db):
    """The outcome memory must carry a human subject from the proposal, not just
    an opaque UUID — else the memory has no task terms to recall on (#1487 :1212)."""
    from genesis.db.crud.ego import create_proposal

    store = _RecordingStore()
    await create_proposal(
        db,
        id="prop-subj",
        action_type="dispatch",
        content="Refactor the memory retrieval stack",
        status="executed",
    )
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-subj")
    res = DirectSessionResult(session_id="s-subj", success=True, output_text="done")
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    succ = [c for c in store.calls if "dispatch_success" in (c.get("tags") or [])]
    assert len(succ) == 1
    assert "Refactor the memory retrieval" in succ[0]["content"], (
        "outcome memory must carry the proposal subject for recall, not just the UUID"
    )


# --- park→resume proposal-lineage survival (PR-3) ---
#
# A rate-limit-parked ego dispatch resumes with caller_context rewritten to
# "rate_limit_resume:<park_id>" (needed for park-lineage), which severs the
# "ego_proposal:<id>" linkage the outcome-recording guard checks. The ORIGINAL
# context rides across on origin_caller_context; the guard must fall back to it,
# else a resumed dispatch's outcome (proposal update + recallable memory) is
# silently dropped — exactly the #1487 P2 / #1496 / 837f8b63 gap.


@pytest.mark.asyncio
async def test_record_outcome_survives_park_resume(db):
    """A resumed dispatch (caller_context='rate_limit_resume:<pid>' +
    origin_caller_context='ego_proposal:<id>') still records its outcome."""
    from genesis.db.crud.ego import create_proposal, get_proposal

    store = _RecordingStore()
    await create_proposal(
        db, id="prop-resumed", action_type="dispatch", content="x", status="executed"
    )
    req = DirectSessionRequest(
        prompt="t",
        caller_context="rate_limit_resume:park-xyz",
        origin_caller_context="ego_proposal:prop-resumed",
    )
    res = DirectSessionResult(session_id="s-res", success=True, output_text="done after resume")
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    prop = await get_proposal(db, "prop-resumed")
    assert "|completed:" in (prop["user_response"] or ""), (
        "a resumed dispatch's proposal outcome must be recorded, not dropped"
    )
    succ = [c for c in store.calls if "dispatch_success" in (c.get("tags") or [])]
    assert len(succ) == 1 and "prop-resumed" in succ[0].get("content", ""), (
        "a resumed dispatch must write its recallable outcome memory"
    )


@pytest.mark.asyncio
async def test_record_outcome_plain_resume_prefix_without_origin_is_noop(db):
    """A resume-prefixed context with NO origin (a non-ego parked job) must NOT
    be misread as a proposal — the guard still early-returns."""
    store = _RecordingStore()
    req = DirectSessionRequest(
        prompt="t", caller_context="rate_limit_resume:park-abc", origin_caller_context=None
    )
    res = DirectSessionResult(session_id="s-none", success=True, output_text="x")
    await _recording_runner(db, store)._record_proposal_outcome(req, res)
    assert store.calls == [], "a non-ego resumed job must not record a proposal outcome"


@pytest.mark.asyncio
async def test_cancel_records_terminal_status_before_embedding(db):
    """On cancel, the terminal 'failed' status must be written BEFORE the outcome
    embed — a slow vectorize during the 10s shutdown grace must not push the
    status write past DB close, leaving the row 'active' (#1487 :1217)."""
    from types import SimpleNamespace

    from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType
    from genesis.db.crud.ego import create_proposal

    order: list[str] = []

    class _OrderStore:
        async def store(self, **_kwargs):
            order.append("embed")
            return "mem-id"

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    real_fail = sm.fail

    async def _recording_fail(*a, **k):
        order.append("fail")
        return await real_fail(*a, **k)

    sm.fail = _recording_fail

    invoker = AsyncMock()
    invoker.run_streaming = AsyncMock(side_effect=asyncio.CancelledError())
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=sm,
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db, _memory_store=_OrderStore()),
    )
    runner._build_invocation = lambda _req, _sid: CCInvocation(prompt="x")

    await create_proposal(db, id="prop-cxl", action_type="dispatch", content="y", status="executed")
    sess = await sm.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
    )
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-cxl")
    with pytest.raises(asyncio.CancelledError):
        await runner._run_session(req, sess["id"])

    assert "fail" in order and "embed" in order, f"expected both events, got {order}"
    assert order.index("fail") < order.index("embed"), (
        f"terminal status must be recorded before the outcome embed, got {order}"
    )


@pytest.mark.asyncio
async def test_generic_failure_records_terminal_status_before_embedding(db):
    """The generic (non-cancel) failure path has the SAME ordering invariant:
    terminal 'failed' status before the outcome embed. Locks the whole class,
    not just the cancel instance."""
    from types import SimpleNamespace

    from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType
    from genesis.db.crud.ego import create_proposal

    order: list[str] = []

    class _OrderStore:
        async def store(self, **_kwargs):
            order.append("embed")
            return "mem-id"

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    real_fail = sm.fail

    async def _recording_fail(*a, **k):
        order.append("fail")
        return await real_fail(*a, **k)

    sm.fail = _recording_fail

    invoker = AsyncMock()
    invoker.run_streaming = AsyncMock(side_effect=ValueError("boom"))
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=sm,
        config_builder=AsyncMock(),
        runtime=SimpleNamespace(_db=db, _memory_store=_OrderStore()),
    )
    runner._build_invocation = lambda _req, _sid: CCInvocation(prompt="x")

    await create_proposal(db, id="prop-gf", action_type="dispatch", content="z", status="executed")
    sess = await sm.create_background(
        session_type=SessionType.BACKGROUND_TASK,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
    )
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-gf")
    # generic failure path records the outcome best-effort, then re-raises
    with pytest.raises(ValueError):
        await runner._run_session(req, sess["id"])

    assert "fail" in order and "embed" in order, f"expected both events, got {order}"
    assert order.index("fail") < order.index("embed"), (
        f"terminal status must precede the outcome embed on the generic path, got {order}"
    )


# --- WS-3 provenance: dispatch outcomes inherit the SESSION's origin (Codex #1487) ---
#
# A research/interact/etc. dispatch is external_untrusted — its output can echo
# web/browser content. These outcome memories are now default-recallable (the
# source_subsystem drop above), so an UNSTAMPED one defaults to first_party and
# bypasses recall-time external-content handling: a laundered indirect prompt
# injection into later proactive sessions. The outcome store must carry the SAME
# origin the session's own memories carry, and pin memory_class="fact" so a
# summary echoing MUST/NEVER isn't heuristically misfiled as a rule.


@pytest.mark.asyncio
async def test_record_outcome_stamps_external_untrusted_origin(db):
    """A research-profile dispatch outcome is stored external_untrusted, not the
    first_party default — else it launders external content into default recall."""
    from genesis.db.crud.ego import create_proposal

    store = _RecordingStore()
    await create_proposal(db, id="prop-ext", action_type="dispatch", content="x", status="executed")
    req = DirectSessionRequest(
        prompt="t", profile="research", caller_context="ego_proposal:prop-ext"
    )
    res = DirectSessionResult(
        session_id="s-ext", success=True, output_text="web-sourced findings summary"
    )
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    out = [c for c in store.calls if c.get("source") == "ego_dispatch_outcome"]
    assert len(out) == 1
    assert out[0].get("origin_class") == "external_untrusted", (
        "a research (external_untrusted) dispatch outcome must NOT be stored first_party"
    )


@pytest.mark.asyncio
async def test_record_outcome_first_party_profile_leaves_origin_none(db):
    """An observe-profile (first-party) dispatch passes origin_class=None so the
    store's own derive_origin_class resolves first_party — no over-stamping."""
    from genesis.db.crud.ego import create_proposal

    store = _RecordingStore()
    await create_proposal(db, id="prop-fp", action_type="dispatch", content="x", status="executed")
    req = DirectSessionRequest(prompt="t", profile="observe", caller_context="ego_proposal:prop-fp")
    res = DirectSessionResult(session_id="s-fp", success=True, output_text="ok")
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    out = [c for c in store.calls if c.get("source") == "ego_dispatch_outcome"]
    assert len(out) == 1
    assert out[0].get("origin_class") is None


@pytest.mark.asyncio
async def test_record_outcome_pins_fact_memory_class(db):
    """Dispatch outcomes pin memory_class='fact' — an outcome whose summary echoes
    a rule-like phrase (MUST/NEVER) must not be heuristically misfiled as a rule."""
    from genesis.db.crud.ego import create_proposal

    store = _RecordingStore()
    await create_proposal(db, id="prop-mc", action_type="dispatch", content="x", status="executed")
    req = DirectSessionRequest(prompt="t", caller_context="ego_proposal:prop-mc")
    res = DirectSessionResult(
        session_id="s-mc", success=True, output_text="You MUST NEVER skip the gate"
    )
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    out = [c for c in store.calls if c.get("source") == "ego_dispatch_outcome"]
    assert len(out) == 1
    assert out[0].get("memory_class") == "fact"


@pytest.mark.asyncio
async def test_verification_failure_memory_stamps_origin_and_class(db, tmp_path):
    """The verification-failure outcome store (the OTHER recallable outcome write)
    is stamped the same way — external_untrusted origin + fact class."""
    from genesis.db.crud.ego import create_proposal

    store = _RecordingStore()
    await create_proposal(
        db,
        id="prop-vf",
        action_type="dispatch",
        content="x",
        status="executed",
        expected_outputs=json.dumps({"files": [str(tmp_path / "never-written.md")]}),
    )
    req = DirectSessionRequest(
        prompt="t", profile="research", caller_context="ego_proposal:prop-vf"
    )
    res = DirectSessionResult(session_id="s-vf", success=True, output_text="claims done")
    await _recording_runner(db, store)._record_proposal_outcome(req, res)

    vf = [c for c in store.calls if c.get("source") == "ego_dispatch_verification"]
    assert len(vf) == 1
    assert vf[0].get("origin_class") == "external_untrusted"
    assert vf[0].get("memory_class") == "fact"
