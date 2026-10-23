"""Whole native lifecycle isolation, with ordinary Muse as a positive control."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from genesis.cc import direct_session, fallback_state, peer_availability
from genesis.cc.direct_session import DirectSessionRequest
from genesis.cc.exceptions import CCQuotaExhaustedError
from genesis.cc.managed_fallback import record_fallback
from genesis.cc.managed_session import prepared_metadata
from genesis.cc.provider_readiness import contained_provider
from genesis.cc.types import PROBE_CALLER_TAG
from genesis.db.crud import cc_sessions
from tests.test_cc.test_managed_fallback import answer, readiness
from tests.test_cc.test_managed_fallback import managed as managed


async def prepare(b, request):
    sid = str(uuid4())
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        b.db, id=sid, session_type="background_task", model="sonnet", effort="medium",
        source_tag="agent_api", channel="agent", origin_class="external_untrusted",
        metadata=json.dumps(prepared_metadata(request)), started_at=now, last_activity_at=now,
    )
    return sid


def request(contained):
    return DirectSessionRequest(
        prompt="Explicit public conversation", system_prompt="Explicit public identity",
        source_tag="agent_api", caller_context="agent_api:synthetic", managed=True,
        notify=False, contained_peer=contained,
    )


@pytest.mark.parametrize("contained", [False, True])
@pytest.mark.parametrize("initial_fallback", [False, True])
async def test_outer_selection_execution_and_final_observers(managed, monkeypatch, contained, initial_fallback):
    b = managed
    if initial_fallback:
        fallback_state.enter("claude", "last", "rate_limit")
    else:
        fallback_state.clear()
    before_state = fallback_state.read()
    await record_fallback(b.db, "last", "previous-model", success=True)
    peer_availability.note_failure("backup", CCQuotaExhaustedError("synthetic refusal"))
    before = peer_availability.read_peer("backup")
    observer = SimpleNamespace(_notify_status_change=AsyncMock())
    b.runner._rt._cc_invoker = observer
    calls, actual = [], []
    if contained:
        def forbidden(*args, **kwargs):
            raise AssertionError("Private invocation/transcript builder reached")
        monkeypatch.setattr(b.runner, "_build_invocation", forbidden)
        monkeypatch.setattr(direct_session, "background_session_dir", forbidden)
    async def execute(invocation, on_event=None):
        calls.append(invocation)
        assert contained_provider() is contained
        if invocation.caller_tag == PROBE_CALLER_TAG:
            if invocation.anthropic_base_url is None:
                raise CCQuotaExhaustedError("synthetic refusal")
            return await readiness(on_event)
        actual.append(invocation)
        await invocation.on_spawn(424215)
        return answer()
    b.invoker.run_streaming.side_effect = execute
    req = request(contained)
    sid = await prepare(b, req)
    await b.runner.adopt(sid, req)
    await asyncio.wait_for(asyncio.shield(b.runner._active[sid]), 5)
    row = await cc_sessions.get_by_id(b.db, sid)
    meta = json.loads(row["metadata"])
    assert row["status"] == "completed" and meta["native_state"] == "succeeded"
    assert meta["result_committed"] and meta["execution_started"]
    assert meta["execution_provider"] == "backup" and len(actual) == 1 and len(calls) == 3
    assert not b.runner._managed_slots and not contained_provider()
    observed = await (await b.db.execute(
        "SELECT provider_used,success FROM call_site_last_run WHERE call_site_id='cc_fallback'"
    )).fetchone()
    if contained:
        assert tuple(observed) == ("last", 1)
        assert fallback_state.read() == before_state
        assert peer_availability.read_peer("backup") == before
        observer._notify_status_change.assert_not_awaited()
        b.alerts.assert_not_awaited()
        assert meta["transcript_path"] == ""
        deadline = datetime.fromisoformat(meta["deadline_at"]).timestamp()
        assert all(inv.peer_segment.deadline_at == deadline for inv in calls)
        assert len({inv.peer_segment.segment_id for inv in calls}) == 3
        assert all(inv.disable_tools and inv.mcp_config is None and not inv.safe_mode for inv in calls)
    else:
        assert tuple(observed) == ("backup", 1)
        assert fallback_state.read().fallback == "backup"
        assert peer_availability.read_peer("backup").available
        observer._notify_status_change.assert_awaited()
        assert b.alerts.await_count == (0 if initial_fallback else 1)


@pytest.mark.parametrize("failure", ["artifact", "database"])
async def test_contained_finalization_faults_do_not_release_early_or_repeat_work(
    managed, monkeypatch, failure
):
    from genesis.cc import managed_session

    b = managed
    actual = []
    async def execute(invocation, on_event=None):
        if invocation.caller_tag == PROBE_CALLER_TAG:
            return await readiness(on_event)
        actual.append(invocation)
        await invocation.on_spawn(424215)
        return answer()
    b.invoker.run_streaming.side_effect = execute
    req = request(True)
    sid = await prepare(b, req)
    entered, release = asyncio.Event(), asyncio.Event()
    original = b.runner._store_result
    stored = []
    async def store(*args, **kwargs):
        assert contained_provider() and sid in b.runner._managed_slots
        stored.append(True)
        if failure == "database" and len(stored) == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("Synthetic persistence refusal")
        return await original(*args, **kwargs)
    monkeypatch.setattr(b.runner, "_store_result", store)
    writes = []
    write = managed_session.atomic_write_text
    def artifact(*args, **kwargs):
        writes.append(True)
        if failure == "artifact":
            raise OSError("Synthetic artifact refusal")
        return write(*args, **kwargs)
    monkeypatch.setattr(managed_session, "atomic_write_text", artifact)
    await b.runner.adopt(sid, req)
    if failure == "database":
        await asyncio.wait_for(entered.wait(), 5)
        assert sid in b.runner._managed_slots and len(actual) == 1
        release.set()
    await asyncio.wait_for(asyncio.shield(b.runner._active[sid]), 5)
    row = await cc_sessions.get_by_id(b.db, sid)
    meta = json.loads(row["metadata"])
    assert len(actual) == len(writes) == 1 and not b.runner._managed_slots
    assert row["status"] == ("failed" if failure == "artifact" else "completed")
    assert meta["result_committed"] is (failure != "artifact")
    assert meta["transcript_path"] == ""
