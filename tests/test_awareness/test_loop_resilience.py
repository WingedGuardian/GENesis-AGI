"""Tests for awareness loop resilience integration."""

import json
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: F401 - MagicMock used

from genesis.awareness.loop import AwarenessLoop, perform_tick
from genesis.awareness.types import Depth, SignalReading, TickResult


class _HotSignal:
    """Signal that always triggers Micro depth."""
    signal_name = "software_error_spike"

    async def collect(self):
        return SignalReading(
            name="software_error_spike", value=1.0,
            source="health_mcp", collected_at="2026-03-03T12:00:00+00:00",
        )


class _FailingReflectionEngine:
    async def reflect(self, depth, result, *, db=None):
        raise RuntimeError("reflection boom")


async def test_reflection_failure_enqueues_deferred_work(db):
    """When reflection fails and deferred_queue is available, work is enqueued."""
    dq = AsyncMock()
    dq.enqueue = AsyncMock(return_value="item-id")

    result = await perform_tick(
        db, [_HotSignal()], source="scheduled",
        reflection_engine=_FailingReflectionEngine(),
        deferred_queue=dq,
    )

    # The tick should have triggered a depth (Micro at minimum)
    assert result.classified_depth is not None
    dq.enqueue.assert_awaited_once()
    call_kwargs = dq.enqueue.call_args[1]
    assert call_kwargs["work_type"] == "reflection"
    assert call_kwargs["reason"] == "reflection_failed"
    assert call_kwargs["staleness_policy"] == "ttl"
    assert call_kwargs["staleness_ttl_s"] == 14400
    payload = json.loads(call_kwargs["payload"])
    assert payload["tick_id"] == result.tick_id


async def test_reflection_failure_without_deferred_queue(db):
    """When reflection fails without deferred_queue, tick completes normally."""
    result = await perform_tick(
        db, [_HotSignal()], source="scheduled",
        reflection_engine=_FailingReflectionEngine(),
    )
    # Should not raise, tick completes
    assert result.tick_id is not None


async def test_late_binding_setters(db):
    """Late-binding setters wire up correctly."""
    loop = AwarenessLoop(db, [])

    sm = MagicMock()
    dq = AsyncMock()

    loop.set_resilience_state_machine(sm)
    loop.set_deferred_queue(dq)

    assert loop._resilience_state_machine is sm
    assert loop._deferred_queue is dq


# ── LIGHT → CC Haiku (primary) ───────────────────────────────────────────

class _LightDecision:
    depth = Depth.LIGHT
    reason = "test light trigger"


async def test_light_reflection_uses_cc_bridge_directly(db):
    """Light reflections go directly to CC Haiku bridge (not API engine)."""
    api_engine = AsyncMock()
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock()

    with patch("genesis.awareness.loop.classify_depth", return_value=_LightDecision()):
        result = await perform_tick(
            db, [_HotSignal()], source="scheduled",
            reflection_engine=api_engine,
            cc_reflection_bridge=cc_bridge,
        )

    assert result.classified_depth == Depth.LIGHT
    api_engine.reflect.assert_not_awaited()  # API engine NOT called for Light
    cc_bridge.reflect.assert_awaited_once()  # CC bridge called directly


async def test_light_reflection_defers_when_cc_fails(db):
    """When CC bridge fails for LIGHT, work is deferred."""
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(side_effect=RuntimeError("CC dead"))
    dq = AsyncMock()
    dq.enqueue = AsyncMock(return_value="deferred-1")

    with patch("genesis.awareness.loop.classify_depth", return_value=_LightDecision()):
        result = await perform_tick(
            db, [_HotSignal()], source="scheduled",
            cc_reflection_bridge=cc_bridge,
            deferred_queue=dq,
        )

    assert result.classified_depth == Depth.LIGHT
    dq.enqueue.assert_awaited_once()


async def test_micro_does_not_fall_back_to_cc(db):
    """MICRO failures go to deferred queue, not CC bridge."""
    api_engine = AsyncMock()
    api_engine.reflect = AsyncMock(side_effect=RuntimeError("API dead"))
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock()
    dq = AsyncMock()
    dq.enqueue = AsyncMock(return_value="deferred-1")

    # _HotSignal triggers MICRO naturally
    result = await perform_tick(
        db, [_HotSignal()], source="scheduled",
        reflection_engine=api_engine,
        cc_reflection_bridge=cc_bridge,
        deferred_queue=dq,
    )

    if result.classified_depth == Depth.MICRO:
        cc_bridge.reflect.assert_not_awaited()  # CC NOT called for micro
        dq.enqueue.assert_awaited_once()


# ── _retry_deferred_reflection tests ─────────────────────────────────────

class _MockReflectionResult:
    def __init__(self, success: bool, reason: str | None = None):
        self.success = success
        self.reason = reason


async def test_retry_deferred_reflection_success_marks_completed(db):
    """When reflect() returns success=True, the deferred item is completed."""
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(return_value=_MockReflectionResult(success=True))

    dq = AsyncMock()
    dq.next_pending = AsyncMock(return_value={
        "id": "item-1",
        "work_type": "reflection",
        "payload_json": json.dumps({"depth": "Deep"}),
        "attempts": 0,
    })
    dq.mark_processing = AsyncMock(return_value=True)
    dq.mark_completed = AsyncMock(return_value=True)
    dq.reset_to_pending = AsyncMock(return_value=True)

    loop = AwarenessLoop(db, [])
    loop.set_deferred_queue(dq)
    loop.set_cc_reflection_bridge(cc_bridge)

    tick = TickResult(
        tick_id="t1", timestamp="2026-03-24T05:00:00+00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.DEEP, trigger_reason=None,
    )
    await loop._retry_deferred_reflection(tick)

    dq.mark_completed.assert_awaited_once_with("item-1")
    dq.reset_to_pending.assert_not_awaited()
    # WS-6 head-of-line: the consumer must filter by work_type so a higher-priority
    # non-reflection item at the head can't block reflection retries.
    dq.next_pending.assert_awaited_once_with(work_type="reflection", max_priority=40)


async def test_retry_deferred_reflection_failure_resets_to_pending(db):
    """When reflect() returns success=False, the item is reset for retry."""
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(
        return_value=_MockReflectionResult(success=False, reason="CC throttled"),
    )

    dq = AsyncMock()
    dq.next_pending = AsyncMock(return_value={
        "id": "item-2",
        "work_type": "reflection",
        "payload_json": json.dumps({"depth": "Light"}),
        "attempts": 0,
    })
    dq.mark_processing = AsyncMock(return_value=True)
    dq.mark_completed = AsyncMock(return_value=True)
    dq.reset_to_pending = AsyncMock(return_value=True)

    loop = AwarenessLoop(db, [])
    loop.set_deferred_queue(dq)
    loop.set_cc_reflection_bridge(cc_bridge)

    tick = TickResult(
        tick_id="t2", timestamp="2026-03-24T05:00:00+00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.LIGHT, trigger_reason=None,
    )
    await loop._retry_deferred_reflection(tick)

    dq.reset_to_pending.assert_awaited_once_with("item-2")
    dq.mark_completed.assert_not_awaited()


# ── _resume_approved_reflections tests ───────────────────────────────────

def _approval_loop(db, cc_bridge, approved_for):
    """Build an AwarenessLoop wired to a mock bridge whose approval gate
    reports the given ``policy_id`` → approval-request-id pairs as
    approved-but-unconsumed.
    """
    gate = MagicMock()

    async def _find(*, subsystem, policy_id):
        rid = approved_for.get(policy_id)
        return {"id": rid} if rid else None

    gate.find_recently_approved = AsyncMock(side_effect=_find)
    gate.mark_consumed = AsyncMock(return_value=True)

    dispatcher = MagicMock()
    dispatcher.approval_gate = gate
    cc_bridge._autonomous_dispatcher = dispatcher

    loop = AwarenessLoop(db, [])
    loop.set_cc_reflection_bridge(cc_bridge)
    loop._last_tick_result = TickResult(
        tick_id="t-resume", timestamp="2026-03-24T05:00:00+00:00",
        source="scheduled", signals=[], scores=[],
        classified_depth=Depth.LIGHT, trigger_reason=None,
    )
    return loop, gate


async def test_resume_approved_light_reflection_dispatches_as_light(db):
    """An approved LIGHT reflection resumes as Depth.LIGHT (not STRATEGIC).

    Regression guard: the resume loop's previous binary depth resolution
    (``Depth.DEEP if name == "deep" else Depth.STRATEGIC``) would have
    mis-dispatched light as an expensive STRATEGIC reflection.
    """
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(return_value=_MockReflectionResult(success=True))

    loop, gate = _approval_loop(db, cc_bridge, {"reflection_light": "appr-light"})

    await loop._resume_approved_reflections()

    gate.mark_consumed.assert_awaited_once_with("appr-light")
    cc_bridge.reflect.assert_awaited_once()
    call = cc_bridge.reflect.call_args
    assert call.args[0] == Depth.LIGHT
    assert call.kwargs["skip_approval"] is True


async def test_resume_approved_reflection_depth_mapping(db):
    """Each approved policy_id resumes at its OWN depth — no binary collapse."""
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(return_value=_MockReflectionResult(success=True))

    loop, gate = _approval_loop(db, cc_bridge, {
        "reflection_deep": "appr-deep",
        "reflection_strategic": "appr-strat",
        "reflection_light": "appr-light",
    })

    await loop._resume_approved_reflections()

    # Order-independent on purpose: the invariant is that each policy_id resumes
    # at its OWN depth (no binary collapse), NOT a specific dispatch order. If
    # light had collapsed to STRATEGIC, the set would be missing Depth.LIGHT.
    dispatched = [c.args[0] for c in cc_bridge.reflect.call_args_list]
    assert len(dispatched) == 3
    assert set(dispatched) == {Depth.DEEP, Depth.STRATEGIC, Depth.LIGHT}
    assert gate.mark_consumed.await_count == 3


async def test_reflection_light_approval_resume_data_path(db):
    """Integration: a real approved ``reflection_light`` request flows through
    the same crud the resume loop uses — pending → approved → found →
    atomically consumed. The loop's dispatch is covered by the unit tests
    above; this covers the real data layer (no mocks) end-to-end.
    """
    import json

    from genesis.autonomy.approval import ApprovalManager
    from genesis.db.crud import approval_requests as ar_crud

    mgr = ApprovalManager(db=db)
    req_id = await mgr.request_approval(
        action_type="autonomous_cli_fallback",
        action_class="costly_reversible",
        description="Approve Claude Code session for light reflection?",
        context=json.dumps({
            "kind": "autonomous_cli_fallback",
            "subsystem": "reflection",
            "policy_id": "reflection_light",
        }),
    )

    # A still-pending request is NOT resumable.
    assert await ar_crud.find_approved_unconsumed(
        db, subsystem="reflection", policy_id="reflection_light",
    ) is None

    # User approves it.
    assert await mgr.resolve(req_id, status="approved") is True

    found = await ar_crud.find_approved_unconsumed(
        db, subsystem="reflection", policy_id="reflection_light",
    )
    assert found is not None and found["id"] == req_id

    # Atomic consume — the second call must fail (double-dispatch guard).
    assert await ar_crud.mark_consumed(
        db, req_id, consumed_at="2026-06-19T00:00:00+00:00",
    ) is True
    assert await ar_crud.mark_consumed(
        db, req_id, consumed_at="2026-06-19T00:00:01+00:00",
    ) is False

    # Once consumed, it is no longer resumable.
    assert await ar_crud.find_approved_unconsumed(
        db, subsystem="reflection", policy_id="reflection_light",
    ) is None
