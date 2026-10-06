"""genesis.util.inflight: the work a restart of this server would cancel, as the
server itself reports it (GET /api/genesis/inflight), and the three places that
register it: every Claude invocation, a dispatched session for its whole life,
and a CLI reflection from its session's creation until it returns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.util import inflight as inf
from genesis.util.inflight import inflight, snapshot


@pytest.fixture(autouse=True)
def _empty_registry():
    inf._items.clear()
    yield
    inf._items.clear()


def _ids() -> list[str]:
    return [i.id for i in snapshot()]


# -- the registry -----------------------------------------------------------------
def test_a_unit_is_listed_while_open_and_gone_after():
    with inflight("direct_session", "x", item_id="s-1") as iid:
        assert iid == "s-1"
        (item,) = snapshot()
        assert (item.id, item.kind, item.label) == ("s-1", "direct_session", "x")
    assert snapshot() == []


def test_a_unit_is_gone_after_an_exception_too():
    with pytest.raises(RuntimeError), inflight("claude"):
        assert len(snapshot()) == 1
        raise RuntimeError("boom")
    assert snapshot() == []


def test_work_inside_an_open_unit_is_part_of_it():
    with inflight("direct_session", item_id="s-1"):
        with inflight("claude") as inner:
            assert inner == "s-1"
            assert _ids() == ["s-1"]
        assert _ids() == ["s-1"], "the inner exit must not close the outer unit"
    assert snapshot() == []


async def test_a_task_spawned_in_a_unit_registers_its_own_work_once_the_unit_ends():
    """A task copies its creator's context, so it still sees the unit's id after the
    unit has ended; only a LIVE unit absorbs it."""
    later = asyncio.Event()
    seen: list[list[str]] = []

    async def child():
        await later.wait()
        with inflight("claude", "late call"):
            seen.append(_ids())

    with inflight("direct_session", item_id="s-1"):
        task = asyncio.create_task(child())
    later.set()
    await task
    assert len(seen) == 1 and len(seen[0]) == 1 and seen[0][0].startswith("claude-")


async def test_a_task_started_inside_a_unit_stays_visible_after_the_unit_ends():
    """A child task copies the unit's context but can outlive it: its work must
    register on its own, or it vanishes from the report when the parent ends."""
    started, release = asyncio.Event(), asyncio.Event()

    async def child():
        with inflight("claude", "child call"):
            started.set()
            await release.wait()

    with inflight("direct_session", item_id="s-1"):
        task = asyncio.create_task(child())
        await started.wait()
        assert sorted(i.kind for i in snapshot()) == ["claude", "direct_session"]
    assert [i.kind for i in snapshot()] == ["claude"], "the child outlived its parent"
    release.set()
    await task
    assert snapshot() == []


async def test_work_in_the_same_task_is_absorbed():
    with inflight("direct_session", item_id="s-1"):
        with inflight("claude") as inner:
            assert inner == "s-1"
        await asyncio.sleep(0)
        assert _ids() == ["s-1"]


def test_generated_ids_name_this_process_life():
    with inflight("claude") as iid:
        assert iid.startswith(f"claude-{inf._BOOT}-")


def test_generated_ids_are_unique_and_a_reused_id_stays_visible():
    with inflight("claude") as a, inflight("x", item_id="dup"):
        pass
    with inflight("claude") as b:
        assert a != b
    # Two concurrent units given the same id (outside each other's context).
    ctx_a = inflight("x", item_id="dup")
    ctx_a.__enter__()
    try:
        token = inf._enclosing.set(None)
        try:
            with inflight("x", item_id="dup") as second:
                assert second != "dup"
                assert sorted(_ids()) == sorted(["dup", second])
        finally:
            inf._enclosing.reset(token)
    finally:
        ctx_a.__exit__(None, None, None)


# -- the call sites -----------------------------------------------------------------
async def test_every_claude_invocation_is_registered_for_its_whole_call(monkeypatch):
    from genesis.cc import invoker as invoker_mod
    from genesis.cc.types import CCInvocation, CCModel, CCOutput

    monkeypatch.setattr(invoker_mod.roster, "apply_active", lambda inv: (inv, ""))
    inv = invoker_mod.CCInvoker()
    seen: list[list[tuple[str, str]]] = []

    async def fake(_invocation, _roster, *_rest):
        seen.append([(i.kind, i.label) for i in snapshot()])
        return CCOutput(
            session_id="x",
            text="",
            model_used="sonnet",
            cost_usd=0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
            exit_code=0,
        )

    monkeypatch.setattr(inv, "_run_traced", fake)
    monkeypatch.setattr(inv, "_run_streaming_traced", fake)
    call = CCInvocation(prompt="SECRET PROMPT", model=CCModel.SONNET, resume_session_id="abc-1")
    await inv.run(call)
    await inv.run_streaming(call)
    assert seen == [[("claude", "sonnet, resumes abc-1")]] * 2
    assert "SECRET" not in repr(seen), "the prompt is never part of the report"
    assert snapshot() == []


async def test_a_dispatched_session_is_registered_by_its_id_for_its_whole_run(db):
    from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
    from genesis.cc.session_manager import SessionManager

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    runner = DirectSessionRunner(
        invoker=AsyncMock(), session_manager=sm, config_builder=AsyncMock(), runtime=object()
    )
    release = asyncio.Event()
    seen: list[list[tuple[str, str]]] = []

    async def fake_run(_req, _sid):
        # Stands in for the whole run: the slot wait, the Claude call, and the
        # storing and delivery after it.
        seen.append([(i.id, i.kind) for i in snapshot()])
        await release.wait()
        seen.append([(i.id, i.kind) for i in snapshot()])

    runner._run_session = fake_run
    sid = await runner.spawn(
        DirectSessionRequest(prompt="x", profile="research", source_tag="probe")
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(runner._active.get(sid) or asyncio.sleep(0))
    assert seen == [[(sid, "direct_session")], [(sid, "direct_session")]]
    assert snapshot() == []


async def test_a_dispatched_session_is_registered_before_its_task_first_runs(db):
    """spawn() returns once the row exists and the task is scheduled; shutdown
    cancels it from then on, so the report must name it before the task has had
    a single step."""
    from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
    from genesis.cc.session_manager import SessionManager

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    runner = DirectSessionRunner(
        invoker=AsyncMock(), session_manager=sm, config_builder=AsyncMock(), runtime=object()
    )
    started = []

    async def fake_run(_req, _sid):
        started.append(True)

    runner._run_session = fake_run
    sid = await runner.spawn(
        DirectSessionRequest(prompt="x", profile="research", source_tag="probe")
    )
    assert not started, "the task ran before the check below; the test proves nothing"
    assert _ids() == [sid]
    await asyncio.gather(runner._active.get(sid) or asyncio.sleep(0))
    await asyncio.sleep(0)
    assert snapshot() == [], "the unit closes with its task"


async def test_a_claude_call_inside_a_dispatched_session_is_part_of_it(db):
    from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
    from genesis.cc.session_manager import SessionManager

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    runner = DirectSessionRunner(
        invoker=AsyncMock(), session_manager=sm, config_builder=AsyncMock(), runtime=object()
    )
    seen: list[list[str]] = []

    async def fake_run(_req, _sid):
        with inflight("claude", "the session's call"):
            seen.append(_ids())

    runner._run_session = fake_run
    sid = await runner.spawn(DirectSessionRequest(prompt="x", profile="research"))
    await asyncio.gather(runner._active.get(sid) or asyncio.sleep(0))
    assert seen == [[sid]], "one piece of work is one item"


async def test_a_session_cancelled_before_it_starts_is_released(db):
    from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
    from genesis.cc.session_manager import SessionManager

    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    runner = DirectSessionRunner(
        invoker=AsyncMock(), session_manager=sm, config_builder=AsyncMock(), runtime=object()
    )

    async def fake_run(_req, _sid):
        await asyncio.sleep(3600)

    runner._run_session = fake_run
    sid = await runner.spawn(DirectSessionRequest(prompt="x", profile="research"))
    task = runner._active[sid]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert snapshot() == []


async def test_a_cli_reflection_stays_registered_through_its_post_processing(db):
    """The session's row is marked completed before the corpus recording and the
    routing that follow it, which a restart also cancels: the unit spans them."""
    from genesis.awareness.types import Depth, TickResult
    from genesis.cc.reflection_bridge import CCReflectionBridge
    from genesis.cc.types import CCOutput

    during: dict[str, list[str]] = {}

    async def fake_run(_inv):
        during["claude"] = _ids()
        return CCOutput(
            session_id="refl-1",
            text='{"observations":[],"patterns":[],"recommendations":[]}',
            model_used="sonnet",
            cost_usd=0.0,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            exit_code=0,
        )

    async def fake_complete(*_a, **_k):
        during["complete"] = _ids()

    invoker = SimpleNamespace(run=AsyncMock(side_effect=fake_run))
    sm = AsyncMock()
    sm.create_background = AsyncMock(return_value={"id": "bg-sess-1"})
    sm.complete = AsyncMock(side_effect=fake_complete)
    bridge = CCReflectionBridge(session_manager=sm, invoker=invoker, db=db)
    tick = TickResult(
        tick_id="t",
        timestamp="2026-03-07T12:00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=Depth.DEEP,
        trigger_reason="test",
    )
    result = await bridge.reflect(Depth.DEEP, tick, db=db)
    assert result.success
    assert during == {"claude": ["bg-sess-1"], "complete": ["bg-sess-1"]}
    assert snapshot() == []
