"""Tests for CCReflectionBridge."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.awareness.types import Depth, TickResult
from genesis.cc.reflection_bridge import CCReflectionBridge
from genesis.cc.types import CCModel, CCOutput, EffortLevel


@pytest.fixture
def mock_invoker():
    invoker = AsyncMock()
    invoker.run = AsyncMock(
        return_value=CCOutput(
            session_id="refl-1",
            text='{"observations":["test obs"],"patterns":[],"recommendations":[]}',
            model_used="sonnet",
            cost_usd=0.02,
            input_tokens=500,
            output_tokens=200,
            duration_ms=5000,
            exit_code=0,
        ),
    )
    return invoker


@pytest.fixture
def mock_session_mgr():
    mgr = AsyncMock()
    mgr.create_background = AsyncMock(return_value={"id": "bg-sess-1"})
    return mgr


@pytest.fixture
def tick():
    return TickResult(
        tick_id="tick-1",
        timestamp="2026-03-07T12:00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=Depth.DEEP,
        trigger_reason="test",
    )


@pytest.fixture
def bridge(db, mock_invoker, mock_session_mgr):
    return CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=mock_invoker, db=db,
    )


def test_model_for_depth(bridge):
    assert bridge._model_for_depth(Depth.LIGHT) == CCModel.HAIKU
    assert bridge._model_for_depth(Depth.DEEP) == CCModel.SONNET
    assert bridge._model_for_depth(Depth.STRATEGIC) == CCModel.OPUS


def test_effort_for_context(bridge):
    # LIGHT→LOW, DEEP→HIGH (fixed), STRATEGIC→MAX
    assert bridge._effort_for_context(Depth.LIGHT) == EffortLevel.LOW
    assert bridge._effort_for_context(Depth.DEEP) == EffortLevel.HIGH
    assert bridge._effort_for_context(Depth.STRATEGIC) == EffortLevel.MAX
    # Deep is fixed HIGH regardless of escalation source or signal load
    # (escalation_source is logged but doesn't change effort — that's V4 executor)
    assert bridge._effort_for_context(Depth.DEEP, escalation_source="critical_bypass") == EffortLevel.HIGH
    assert bridge._effort_for_context(Depth.DEEP, escalation_source="light_escalation") == EffortLevel.HIGH


async def test_reflect_deep(db, bridge, tick, mock_invoker):
    result = await bridge.reflect(Depth.DEEP, tick, db=db)
    assert result.success
    mock_invoker.run.assert_called_once()
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.model == CCModel.SONNET


async def test_reflect_strategic(db, bridge, tick, mock_invoker):
    result = await bridge.reflect(Depth.STRATEGIC, tick, db=db)
    assert result.success
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.model == CCModel.OPUS


async def test_reflect_handles_cc_error(db, mock_session_mgr):
    bad_invoker = AsyncMock()
    bad_invoker.run = AsyncMock(
        return_value=CCOutput(
            session_id="",
            text="",
            model_used="sonnet",
            cost_usd=0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=100,
            exit_code=1,
            is_error=True,
            error_message="CLI crashed",
        ),
    )
    mock_session_mgr.create_background = AsyncMock(return_value={"id": "bg-fail"})
    b = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=bad_invoker, db=db,
    )
    t = TickResult(
        tick_id="t1",
        timestamp="2026-03-07T12:00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=Depth.DEEP,
        trigger_reason="test",
    )
    result = await b.reflect(Depth.DEEP, t, db=db)
    assert not result.success
    assert result.reason == "CLI crashed"


async def test_reflect_handles_session_creation_failure(db):
    bad_mgr = AsyncMock()
    bad_mgr.create_background = AsyncMock(side_effect=RuntimeError("boom"))
    invoker = AsyncMock()
    b = CCReflectionBridge(
        session_manager=bad_mgr, invoker=invoker, db=db,
    )
    t = TickResult(
        tick_id="t2",
        timestamp="2026-03-07T12:00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=Depth.DEEP,
        trigger_reason="test",
    )
    result = await b.reflect(Depth.DEEP, t, db=db)
    assert not result.success
    assert result.reason == "Session creation failed"
    invoker.run.assert_not_called()


def test_system_prompt_loads_from_file(tmp_path, db, mock_invoker, mock_session_mgr):
    deep_file = tmp_path / "REFLECTION_DEEP.md"
    deep_file.write_text("Deep prompt content here")
    strategic_file = tmp_path / "REFLECTION_STRATEGIC.md"
    strategic_file.write_text("Strategic prompt content here")

    b = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=mock_invoker, db=db,
        prompt_dir=tmp_path,
    )
    assert "Deep prompt content" in b._system_prompt_for_depth(Depth.DEEP)
    assert "Strategic prompt content" in b._system_prompt_for_depth(Depth.STRATEGIC)


def test_system_prompt_falls_back_when_file_missing(db, mock_invoker, mock_session_mgr):
    b = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=mock_invoker, db=db,
        prompt_dir=Path("/nonexistent"),
    )
    prompt = b._system_prompt_for_depth(Depth.DEEP)
    assert "Genesis" in prompt


async def test_reflection_prompt_includes_cognitive_state(db, bridge, tick):
    from genesis.db.crud import cognitive_state

    now = datetime.now(UTC).isoformat()
    await cognitive_state.create(
        db, id=str(uuid.uuid4()), content="Currently researching vehicles",
        section="active_context", generated_by="test", created_at=now,
    )
    from genesis.cc.reflection_bridge._prompts import build_reflection_prompt
    prompt, _obs_ids, _surplus_ids = await build_reflection_prompt(
        depth=Depth.DEEP, tick=tick, db=db,
        context_gatherer=None, context_assembler=None,
        prompt_dir=Path("/nonexistent"),
    )
    assert "vehicles" in prompt.lower()


def test_awareness_loop_accepts_bridge_param(db):
    from genesis.awareness.loop import AwarenessLoop

    mock_bridge = AsyncMock()
    loop = AwarenessLoop(db, [], cc_reflection_bridge=mock_bridge)
    assert loop._cc_reflection_bridge is mock_bridge


def test_awareness_loop_set_cc_reflection_bridge(db):
    from genesis.awareness.loop import AwarenessLoop

    loop = AwarenessLoop(db, [])
    assert loop._cc_reflection_bridge is None
    mock_bridge = AsyncMock()
    loop.set_cc_reflection_bridge(mock_bridge)
    assert loop._cc_reflection_bridge is mock_bridge


@pytest.mark.asyncio
async def test_route_deep_output_failure_falls_back_to_legacy(db, bridge, tick):
    """When route_deep_output raises, store_reflection_output must be called."""
    from unittest.mock import patch
    bridge._output_router = AsyncMock()  # enable deep routing path

    mock_route = AsyncMock(side_effect=RuntimeError("routing broke"))
    mock_store = AsyncMock()

    with patch("genesis.cc.reflection_bridge._bridge.route_deep_output", mock_route), \
         patch("genesis.cc.reflection_bridge._bridge.store_reflection_output", mock_store):
        result = await bridge.reflect(depth=Depth.DEEP, tick=tick, db=db)

    assert result.success
    mock_route.assert_awaited_once()
    mock_store.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_deep_output_success_skips_legacy(db, bridge, tick):
    """When route_deep_output succeeds, store_reflection_output must NOT be called."""
    from unittest.mock import patch
    bridge._output_router = AsyncMock()  # enable deep routing path

    mock_route = AsyncMock(return_value={"observations_written": 1})
    mock_store = AsyncMock()

    with patch("genesis.cc.reflection_bridge._bridge.route_deep_output", mock_route), \
         patch("genesis.cc.reflection_bridge._bridge.store_reflection_output", mock_store):
        result = await bridge.reflect(depth=Depth.DEEP, tick=tick, db=db)

    assert result.success
    mock_route.assert_awaited_once()
    mock_store.assert_not_awaited()


# ── Per-model prompt loading ──────────────────────────────────────────


def test_model_specific_prompt_loaded(tmp_path):
    """When REFLECTION_LIGHT_HAIKU.md exists, it's loaded for LIGHT depth."""
    (tmp_path / "REFLECTION_LIGHT.md").write_text("generic prompt")
    (tmp_path / "REFLECTION_LIGHT_HAIKU.md").write_text("haiku-optimized prompt")
    bridge = CCReflectionBridge(
        session_manager=AsyncMock(),
        invoker=AsyncMock(),
        db=AsyncMock(),
        prompt_dir=tmp_path,
    )
    result = bridge._system_prompt_for_depth(Depth.LIGHT)
    # Light gets condensed identity prefix + depth-specific prompt
    assert "haiku-optimized prompt" in result
    assert "Genesis" in result  # condensed identity present


def test_fallback_to_generic_prompt(tmp_path):
    """When no model-specific file exists, falls back to generic."""
    (tmp_path / "REFLECTION_LIGHT.md").write_text("generic prompt")
    bridge = CCReflectionBridge(
        session_manager=AsyncMock(),
        invoker=AsyncMock(),
        db=AsyncMock(),
        prompt_dir=tmp_path,
    )
    result = bridge._system_prompt_for_depth(Depth.LIGHT)
    assert "generic prompt" in result


def test_deep_prompt_includes_identity_when_files_exist(tmp_path):
    """Deep/Strategic system prompt includes SOUL + USER + STEERING when present."""
    (tmp_path / "SOUL.md").write_text("I am Genesis.")
    (tmp_path / "USER.md").write_text("User context.")
    (tmp_path / "STEERING.md").write_text("Hard constraints.")
    (tmp_path / "REFLECTION_DEEP.md").write_text("Deep prompt content")
    bridge = CCReflectionBridge(
        session_manager=AsyncMock(),
        invoker=AsyncMock(),
        db=AsyncMock(),
        prompt_dir=tmp_path,
    )
    result = bridge._system_prompt_for_depth(Depth.DEEP)
    assert "I am Genesis." in result
    assert "User context." in result
    assert "Hard constraints." in result
    assert "Deep prompt content" in result
    # Identity comes before depth-specific prompt
    assert result.index("I am Genesis.") < result.index("Deep prompt content")


# ── Observation truncation fix ──────────────────────────────────────


def test_data_pointers_section_exists():
    """build_data_pointers returns a non-empty string with key resources."""
    from genesis.cc.reflection_bridge._prompts import build_data_pointers
    result = build_data_pointers()
    assert "Available Data Sources" in result
    assert "health_status" in result
    assert "memory_recall" in result
    assert "genesis.db" in result
    assert "transcripts" in result.lower() or "jsonl" in result.lower()


async def test_strategic_enriched_path_includes_observations_and_pointers(db, mock_invoker, mock_session_mgr):
    """Strategic reflection with context_gatherer uses enriched path with observations."""
    from genesis.reflection.types import (
        ContextBundle,
        CostSummary,
        PendingWorkSummary,
        ProcedureStats,
    )

    mock_gatherer = AsyncMock()
    mock_gatherer.gather = AsyncMock(return_value=ContextBundle(
        cognitive_state="test cognitive state",
        recent_observations=[
            {"id": "obs-1", "type": "test", "source": "test", "priority": "medium",
             "content": "Observation content here", "created_at": "2026-04-02T12:00:00"},
        ],
        intelligence_digest="",
        pending_work=PendingWorkSummary(memory_consolidation=False),
        procedure_stats=ProcedureStats(total_active=0, total_quarantined=0, avg_success_rate=0, low_performers=[]),
        cost_summary=CostSummary(daily_usd=0.0, weekly_usd=0.0, monthly_usd=0.0,
                                 daily_budget_pct=0.0, weekly_budget_pct=0.0, monthly_budget_pct=0.0),
        recent_conversations=[],
        gathered_observation_ids=("obs-1",),
    ))

    bridge = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=mock_invoker, db=db,
    )
    bridge._context_gatherer = mock_gatherer

    tick = TickResult(
        tick_id="tick-strat", timestamp="2026-04-02T12:00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.STRATEGIC, trigger_reason="test",
    )

    from genesis.cc.reflection_bridge._prompts import build_reflection_prompt
    prompt, obs_ids, _surplus_ids = await build_reflection_prompt(
        depth=Depth.STRATEGIC, tick=tick, db=db,
        context_gatherer=mock_gatherer, context_assembler=None,
        prompt_dir=Path("/nonexistent"),
    )

    assert "Observation content here" in prompt
    assert "Available Data Sources" in prompt
    assert "Strategic" in prompt
    assert "obs-1" in obs_ids


# ── Model downgrade response (Layer 2) ────────────────────────────


@pytest.mark.asyncio
async def test_strategic_retries_on_downgrade(db, mock_session_mgr):
    """Strategic reflection retries once when model is downgraded."""
    downgraded_output = CCOutput(
        session_id="refl-1",
        text='{"observations":["obs"],"patterns":[],"recommendations":[]}',
        model_used="claude-sonnet-4-6",
        cost_usd=0.02, input_tokens=500, output_tokens=200,
        duration_ms=5000, exit_code=0,
        model_requested="opus", downgraded=True,
    )
    normal_output = CCOutput(
        session_id="refl-1",
        text='{"observations":["obs"],"patterns":[],"recommendations":[]}',
        model_used="claude-opus-4-6",
        cost_usd=0.05, input_tokens=500, output_tokens=200,
        duration_ms=8000, exit_code=0,
        model_requested="opus", downgraded=False,
    )
    invoker = AsyncMock()
    invoker.run = AsyncMock(side_effect=[downgraded_output, normal_output])
    bridge = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=invoker, db=db,
    )
    tick = TickResult(
        tick_id="t1", timestamp="2026-04-02T12:00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.STRATEGIC, trigger_reason="test",
    )
    with patch("genesis.cc.reflection_bridge._bridge.asyncio.sleep", new_callable=AsyncMock):
        result = await bridge.reflect(Depth.STRATEGIC, tick, db=db)
    assert result.success
    assert invoker.run.call_count == 2


@pytest.mark.asyncio
async def test_strategic_proceeds_on_double_downgrade(db, mock_session_mgr):
    """Strategic reflection completes even if retry is also downgraded."""
    downgraded_output = CCOutput(
        session_id="refl-1",
        text='{"observations":["obs"],"patterns":[],"recommendations":[]}',
        model_used="claude-sonnet-4-6",
        cost_usd=0.02, input_tokens=500, output_tokens=200,
        duration_ms=5000, exit_code=0,
        model_requested="opus", downgraded=True,
    )
    invoker = AsyncMock()
    invoker.run = AsyncMock(return_value=downgraded_output)
    bridge = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=invoker, db=db,
    )
    tick = TickResult(
        tick_id="t2", timestamp="2026-04-02T12:00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.STRATEGIC, trigger_reason="test",
    )
    with patch("genesis.cc.reflection_bridge._bridge.asyncio.sleep", new_callable=AsyncMock):
        result = await bridge.reflect(Depth.STRATEGIC, tick, db=db)
    assert result.success
    assert invoker.run.call_count == 2  # original + 1 retry


@pytest.mark.asyncio
async def test_deep_no_retry_on_downgrade(db, mock_session_mgr):
    """Deep reflection does NOT retry on downgrade."""
    downgraded_output = CCOutput(
        session_id="refl-1",
        text='{"observations":["obs"],"patterns":[],"recommendations":[]}',
        model_used="claude-haiku-4-5",
        cost_usd=0.01, input_tokens=500, output_tokens=200,
        duration_ms=3000, exit_code=0,
        model_requested="sonnet", downgraded=True,
    )
    invoker = AsyncMock()
    invoker.run = AsyncMock(return_value=downgraded_output)
    bridge = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=invoker, db=db,
    )
    tick = TickResult(
        tick_id="t3", timestamp="2026-04-02T12:00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.DEEP, trigger_reason="test",
    )
    result = await bridge.reflect(Depth.DEEP, tick, db=db)
    assert result.success
    assert invoker.run.call_count == 1  # no retry


@pytest.mark.asyncio
async def test_strategic_retry_failure_uses_original_output(db, mock_session_mgr):
    """If strategic retry raises, the original downgraded output is kept."""
    from genesis.cc.exceptions import CCTimeoutError

    downgraded_output = CCOutput(
        session_id="refl-1",
        text='{"observations":["obs"],"patterns":[],"recommendations":[]}',
        model_used="claude-sonnet-4-6",
        cost_usd=0.02, input_tokens=500, output_tokens=200,
        duration_ms=5000, exit_code=0,
        model_requested="opus", downgraded=True,
    )
    invoker = AsyncMock()
    invoker.run = AsyncMock(side_effect=[downgraded_output, CCTimeoutError("timeout")])
    bridge = CCReflectionBridge(
        session_manager=mock_session_mgr, invoker=invoker, db=db,
    )
    tick = TickResult(
        tick_id="t4", timestamp="2026-04-02T12:00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.STRATEGIC, trigger_reason="test",
    )
    with patch("genesis.cc.reflection_bridge._bridge.asyncio.sleep", new_callable=AsyncMock):
        result = await bridge.reflect(Depth.STRATEGIC, tick, db=db)
    assert result.success  # original downgraded output still usable
    assert invoker.run.call_count == 2


# ── F3+F4: pid, cc_session_id, and dispatch_mode backfill ─────────


@pytest.mark.asyncio
async def test_bridge_writes_pid_and_cc_session_id(db):
    """Integration: bridge CLI path writes pid + cc_session_id + dispatch_mode."""
    from genesis.cc.session_manager import SessionManager

    FAKE_PID = 54321
    CC_SESSION_UUID = "cc-uuid-from-cli"

    async def mock_run(invocation):
        # Fire on_spawn with fake PID (simulates what CCInvoker does)
        if invocation.on_spawn is not None:
            await invocation.on_spawn(FAKE_PID)
        return CCOutput(
            session_id=CC_SESSION_UUID,
            text='{"observations":["test"],"patterns":[],"recommendations":[]}',
            model_used="sonnet",
            cost_usd=0.02, input_tokens=500, output_tokens=200,
            duration_ms=5000, exit_code=0,
        )

    invoker = AsyncMock()
    invoker.run = AsyncMock(side_effect=mock_run)

    mgr = SessionManager(db=db, invoker=invoker, day_boundary_hour=0)
    bridge = CCReflectionBridge(
        session_manager=mgr, invoker=invoker, db=db,
    )

    tick = TickResult(
        tick_id="t-backfill", timestamp="2026-04-16T12:00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.DEEP, trigger_reason="test",
    )

    result = await bridge.reflect(Depth.DEEP, tick, db=db)
    assert result.success

    # Find the session row created by the bridge
    rows = await db.execute_fetchall(
        "SELECT id, pid, cc_session_id, metadata FROM cc_sessions "
        "WHERE session_type = 'background_reflection' ORDER BY started_at DESC LIMIT 1"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["pid"] == FAKE_PID
    assert row["cc_session_id"] == CC_SESSION_UUID
    meta = json.loads(row["metadata"])
    assert meta["dispatch_mode"] == "cli"


@pytest.mark.asyncio
async def test_bridge_cc_session_id_after_retry(db):
    """After a strategic retry, cc_session_id reflects the RETRY output, not the first."""
    from genesis.cc.session_manager import SessionManager

    FIRST_CC_SID = "first-cc-session"
    RETRY_CC_SID = "retry-cc-session"
    call_count = 0

    async def mock_run(invocation):
        nonlocal call_count
        call_count += 1
        if invocation.on_spawn is not None:
            await invocation.on_spawn(50000 + call_count)
        if call_count == 1:
            return CCOutput(
                session_id=FIRST_CC_SID,
                text='{"observations":["obs"],"patterns":[],"recommendations":[]}',
                model_used="claude-sonnet-4-6",
                cost_usd=0.02, input_tokens=500, output_tokens=200,
                duration_ms=5000, exit_code=0,
                model_requested="opus", downgraded=True,
            )
        return CCOutput(
            session_id=RETRY_CC_SID,
            text='{"observations":["obs"],"patterns":[],"recommendations":[]}',
            model_used="claude-opus-4-6",
            cost_usd=0.05, input_tokens=500, output_tokens=200,
            duration_ms=8000, exit_code=0,
            model_requested="opus", downgraded=False,
        )

    invoker = AsyncMock()
    invoker.run = AsyncMock(side_effect=mock_run)

    mgr = SessionManager(db=db, invoker=invoker, day_boundary_hour=0)
    bridge = CCReflectionBridge(
        session_manager=mgr, invoker=invoker, db=db,
    )

    tick = TickResult(
        tick_id="t-retry", timestamp="2026-04-16T12:00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.STRATEGIC, trigger_reason="test",
    )

    with patch("genesis.cc.reflection_bridge._bridge.asyncio.sleep", new_callable=AsyncMock):
        result = await bridge.reflect(Depth.STRATEGIC, tick, db=db)

    assert result.success
    assert call_count == 2

    rows = await db.execute_fetchall(
        "SELECT pid, cc_session_id FROM cc_sessions "
        "WHERE session_type = 'background_reflection' ORDER BY started_at DESC LIMIT 1"
    )
    row = rows[0]
    # PID should be from the retry (50002), not the first run (50001)
    assert row["pid"] == 50002
    # cc_session_id should be from the retry, not the first run
    assert row["cc_session_id"] == RETRY_CC_SID
