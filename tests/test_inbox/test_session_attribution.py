"""Invocation attribution stays local while durable inbox side effects keep provenance."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.cc import invoker as invoker_module
from genesis.cc import transient_retry
from genesis.cc.exceptions import (
    CCError,
    CCNetworkOfflineError,
    CCOverloadedError,
    CCReplayUnsafeError,
)
from genesis.cc.invoker import CCInvoker
from genesis.db.crud import inbox_items
from genesis.inbox.monitor import InboxMonitor
from genesis.inbox.writer import ResponseWriter
from genesis.observability.events import GenesisEventBus
from genesis.observability.session_context import get_session_id, session_scope
from tests.test_inbox import test_edge_cases as shared

_success_output = shared._success_output
config = shared.config
db = shared.db
inbox_dir = shared.inbox_dir
mock_invoker = shared.mock_invoker
mock_session_manager = shared.mock_session_manager
monitor = shared.monitor
writer = shared.writer


def _environment(invocation):
    env = CCInvoker()._build_env(invocation)
    return (
        get_session_id(),
        env.get("GENESIS_SESSION_ID"),
        env.get("GENESIS_SESSION_ORIGIN"),
        env.get("GENESIS_SESSION_SUPERVISED"),
        env.get("GENESIS_CC_SESSION"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outer", [None, "outer-session"])
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "error",
        "cancel",
        "replay",
        "offline",
        "overload_once",
        "overload_exhausted",
        "returned_error",
    ],
)
async def test_invocation_environment_and_cleanup(
    outer,
    outcome,
    monitor,
    inbox_dir,
    mock_invoker,
    mock_session_manager,
    db,
    monkeypatch,
):
    (inbox_dir / "Note.md").write_text("https://example.com/first")
    bus = GenesisEventBus()
    bus.enable_persistence(db)
    monkeypatch.setattr(invoker_module, "_runtime_event_bus", lambda: bus)
    monkeypatch.setattr(invoker_module, "_failure_event_state", {})
    monkeypatch.setattr(transient_retry, "_sleep", AsyncMock())
    observed, handlers = [], []

    async def finalize(*args, **kwargs):
        handlers.append(get_session_id())

    mock_session_manager.complete.side_effect = finalize
    mock_session_manager.fail.side_effect = finalize

    async def invoke(invocation):
        observed.append(_environment(invocation))
        if outcome == "error":
            exc = CCError("synthetic transport error")
            await invoker_module._emit_invocation_failed_event(exc, invocation, streaming=False)
            raise exc
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "replay":
            raise CCReplayUnsafeError("synthetic replay refusal")
        if outcome == "offline":
            raise CCNetworkOfflineError("synthetic offline")
        if outcome == "overload_exhausted" or (outcome == "overload_once" and len(observed) == 1):
            raise CCOverloadedError("synthetic provider capacity")
        output = _success_output()
        if outcome == "returned_error":
            output = replace(output, is_error=True, error_message="synthetic result error")
        return output

    mock_invoker.run.side_effect = invoke
    try:
        with session_scope(outer):
            if outcome == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await monitor.check_once()
            else:
                result = await monitor.check_once()
                assert result.batches_dispatched == (outcome in {"success", "overload_once"})
            assert get_session_id() == outer
        await bus.stop()
        attempts = 4 if outcome == "overload_exhausted" else 2 if outcome == "overload_once" else 1
        assert observed == [("sess-bg-1", "sess-bg-1", "external_untrusted", None, "1")] * attempts
        assert handlers == ([] if outcome == "cancel" else [outer])
        rows = await (
            await db.execute("SELECT status,retry_count,error_message FROM inbox_items")
        ).fetchall()
        assert len(rows) == 1
        if outcome in {"success", "overload_once"}:
            assert rows[0]["status"] == "completed"
        elif outcome != "cancel":
            assert rows[0]["status"] == "failed"
        if outcome in {"replay", "offline", "overload_exhausted"}:
            assert rows[0]["retry_count"] == 0
        if outcome == "replay":
            assert rows[0]["error_message"].startswith(inbox_items.REPLAY_UNSAFE_PREFIX)
        if outcome == "error":
            events = await (
                await db.execute(
                    "SELECT session_id,details FROM events WHERE event_type='cc.invocation_failed'",
                )
            ).fetchall()
            assert len(events) == 1
            assert events[0]["session_id"] == "sess-bg-1"
            assert json.loads(events[0]["details"])["session_id"] == "sess-bg-1"
    finally:
        await bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("outer", [None, "outer-session"])
async def test_manager_creation_failure_never_installs_invocation_context(
    outer,
    monitor,
    inbox_dir,
    mock_invoker,
    mock_session_manager,
    db,
):
    (inbox_dir / "Note.md").write_text("https://example.com/first")
    seen = []

    async def refuse(**kwargs):
        seen.append(get_session_id())
        raise CCError("synthetic manager refusal")

    mock_session_manager.create_background.side_effect = refuse
    with session_scope(outer):
        result = await monitor.check_once()
        assert get_session_id() == outer
    assert seen == [outer]
    assert result.batches_dispatched == 0
    mock_invoker.run.assert_not_awaited()
    rows = await (await db.execute("SELECT status FROM inbox_items")).fetchall()
    assert [r["status"] for r in rows] == ["failed"]


@pytest.mark.asyncio
async def test_sequential_batches_restore_context_between_manager_ids(
    monitor,
    inbox_dir,
    mock_invoker,
    mock_session_manager,
):
    (inbox_dir / "Note.md").write_text("https://example.com/first\nhttps://example.com/second")
    monitor._config = replace(monitor._config, items_per_eval=1)
    creates, calls = [], []

    async def create(**kwargs):
        creates.append(get_session_id())
        return {"id": f"manager-{len(creates)}"}

    async def invoke(invocation):
        calls.append(_environment(invocation))
        return _success_output()

    mock_session_manager.create_background.side_effect = create
    mock_invoker.run.side_effect = invoke
    with session_scope("outer-session"):
        result = await monitor.check_once()
        assert get_session_id() == "outer-session"
    assert result.batches_dispatched == 2
    assert creates == ["outer-session", "outer-session"]
    assert [r[:2] for r in calls] == [("manager-1", "manager-1"), ("manager-2", "manager-2")]


@pytest.mark.asyncio
async def test_sequential_same_model_failures_in_one_scan_each_reach_their_manager(
    monitor,
    inbox_dir,
    mock_invoker,
    mock_session_manager,
    db,
    monkeypatch,
):
    """#4342: two batches of one scan fail the same way (same error class,
    caller tag and model) inside the coalescing window. Each failure belongs
    to its own manager session, so each gets its own event; neither is
    swallowed into the other's coalesced count."""
    (inbox_dir / "Note.md").write_text("https://example.com/first\nhttps://example.com/second")
    monitor._config = replace(monitor._config, items_per_eval=1)
    bus = GenesisEventBus()
    bus.enable_persistence(db)
    monkeypatch.setattr(invoker_module, "_runtime_event_bus", lambda: bus)
    monkeypatch.setattr(invoker_module, "_failure_event_state", {})
    creates = []

    async def create(**kwargs):
        creates.append(1)
        return {"id": f"manager-{len(creates)}"}

    async def invoke(invocation):
        exc = CCError("synthetic transport error")
        await invoker_module._emit_invocation_failed_event(exc, invocation, streaming=False)
        raise exc

    mock_session_manager.create_background.side_effect = create
    mock_invoker.run.side_effect = invoke
    try:
        result = await monitor.check_once()
        assert result.batches_dispatched == 0
        await bus.stop()
        rows = await (
            await db.execute(
                "SELECT session_id,details FROM events "
                "WHERE event_type='cc.invocation_failed' ORDER BY rowid",
            )
        ).fetchall()
        assert [r["session_id"] for r in rows] == ["manager-1", "manager-2"]
        assert [json.loads(r["details"])["coalesced"] for r in rows] == [0, 0]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_concurrent_monitors_keep_their_own_context(
    monitor,
    inbox_dir,
    mock_invoker,
    mock_session_manager,
    db,
    tmp_path,
):
    other_dir = tmp_path / "other-inbox"
    other_dir.mkdir()
    (inbox_dir / "Note.md").write_text("https://example.com/first")
    (other_dir / "Note.md").write_text("https://example.com/second")
    other_invoker, other_manager = AsyncMock(), AsyncMock()
    other_manager.create_background.return_value = {"id": "other-manager"}
    other = InboxMonitor(
        db=db,
        invoker=other_invoker,
        session_manager=other_manager,
        config=replace(monitor._config, watch_path=other_dir),
        writer=ResponseWriter(watch_path=other_dir, timezone="UTC"),
        clock=monitor._clock,
    )
    barrier = asyncio.Barrier(2)
    calls = {}

    def handler(key):
        async def invoke(invocation):
            before = _environment(invocation)
            await barrier.wait()
            calls[key] = (before, _environment(invocation))
            return _success_output()

        return invoke

    mock_invoker.run.side_effect = handler("first")
    other_invoker.run.side_effect = handler("second")

    async def run(mon, outer):
        with session_scope(outer):
            result = await mon.check_once()
            assert get_session_id() == outer
            return result.batches_dispatched

    assert await asyncio.gather(run(monitor, "outer-a"), run(other, "outer-b")) == [1, 1]
    for key, manager in [("first", "sess-bg-1"), ("second", "other-manager")]:
        assert calls[key] == ((manager, manager, "external_untrusted", None, "1"),) * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_id", ["reported-cli-session", ""])
async def test_successful_scan_preserves_response_followup_baseline_and_detached_context(
    cli_id,
    monitor,
    inbox_dir,
    mock_invoker,
    db,
    monkeypatch,
):
    from genesis.util import tasks

    source = inbox_dir / "Note.md"
    source.write_text("https://example.com/first")
    text = (
        "# Inbox Evaluation\n\n## 1. Sample tool\n"
        "**Source:** <https://example.com/first>\n\n"
        "### Recommendation\n```yaml\naction: ADOPT\n"
        "next_step: Compare the documented integration interface.\nscope: V4\n```\n"
    )
    calls, postprocess, detached, children = [], [], [], []

    async def invoke(invocation):
        calls.append(_environment(invocation))
        return replace(_success_output(text), session_id=cli_id)

    original_followups = monitor._create_follow_ups_from_eval

    async def followups(**kwargs):
        postprocess.append(get_session_id())
        return await original_followups(**kwargs)

    async def triage(*args):
        detached.append(get_session_id())

    def launch(coro, **kwargs):
        # Preserve actual asyncio task-context copying while isolating runtime bookkeeping.
        postprocess.append(get_session_id())
        task = asyncio.create_task(coro)
        children.append(task)
        return task

    mock_invoker.run.side_effect = invoke
    monkeypatch.setattr(monitor, "_create_follow_ups_from_eval", followups)
    monitor._triage_pipeline = object()
    monkeypatch.setattr(monitor, "_fire_triage", triage)
    monkeypatch.setattr(tasks, "tracked_task", launch)
    try:
        with session_scope("outer-session"):
            result = await monitor.check_once()
            await asyncio.gather(*children)
            assert get_session_id() == "outer-session"
            assert (await monitor.check_once()).batches_dispatched == 0
        assert result.batches_dispatched == 1
        assert calls == [("sess-bg-1", "sess-bg-1", "external_untrusted", None, "1")]
        assert postprocess == ["outer-session", "outer-session"]
        assert detached == ["outer-session"]
        rows = await (
            await db.execute(
                "SELECT status,evaluated_content,response_path FROM inbox_items",
            )
        ).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "completed"
        assert "https://example.com/first" in rows[0]["evaluated_content"]
        assert text in Path(rows[0]["response_path"]).read_text()
        followup_rows = await (
            await db.execute(
                "SELECT source_session FROM follow_ups WHERE source='inbox_evaluation'",
            )
        ).fetchall()
        assert [r["source_session"] for r in followup_rows] == [cli_id or None]
    finally:
        await asyncio.gather(*children, return_exceptions=True)
