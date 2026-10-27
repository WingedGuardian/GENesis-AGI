"""Selection acknowledgement races must not fabricate actual execution history."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from genesis.cc.managed_fallback import record_fallback
from genesis.cc.types import StreamEvent
from genesis.db.crud import cc_sessions
from tests.test_cc.test_managed_fallback import managed as managed
from tests.test_cc.test_managed_fallback import settle, start


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
async def test_ready_metadata_ack_interruption_preserves_actual_history(
    managed, monkeypatch, interruption
):
    ready = asyncio.Event()
    original = cc_sessions.merge_metadata

    async def delayed_ack(db, sid, patch, **kwargs):
        result = await original(db, sid, patch, **kwargs)
        if patch.get("execution_provider") == "backup":
            ready.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(cc_sessions, "merge_metadata", delayed_ack)
    await record_fallback(managed.db, "last", "previous-model", success=True)
    meta = {}
    if interruption == "deadline":
        meta["deadline_at"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    sid = await start(managed, **meta)
    await asyncio.wait_for(ready.wait(), 3)
    if interruption == "cancel":
        assert managed.runner.cancel(sid)
    row, meta = await settle(managed, sid)
    assert not managed.tasks and not meta.get("execution_started")
    assert meta["execution_provider"] == "backup" and row["status"] == "failed"
    assert not managed.runner._managed_slots
    values = await managed.db.execute_fetchall(
        "SELECT provider_used,success FROM call_site_last_run WHERE call_site_id='cc_fallback'"
    )
    assert tuple(values[0]) == ("last", 1)


@pytest.mark.parametrize("info", [None, [], ["rejected"], "rejected", 1])
async def test_malformed_advisory_quota_does_not_lose_valid_work(managed, info):
    async def run(invocation, on_event=None):
        output = await managed.execute(invocation, on_event)
        if not invocation.disable_tools:
            await on_event(
                StreamEvent(
                    "rate_limit_event",
                    raw={
                        "type": "rate_limit_event",
                        "rate_limit_info": info,
                    },
                )
            )
        return output

    managed.invoker.run_streaming.side_effect = run
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "completed" and meta["execution_started"]
    assert len(managed.tasks) == 1
