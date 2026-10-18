"""Actual-launch attribution, provider-qualified evidence and cleanup ownership."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.cc import invoker as invoker_module
from genesis.cc import peer_availability
from genesis.cc.exceptions import (
    CCProcessError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCReplayUnsafeError,
)
from genesis.cc.invoker import CCInvoker
from genesis.cc.managed_fallback import record_fallback
from genesis.cc.provider_readiness import isolated_provider
from genesis.cc.types import StreamEvent
from genesis.resilience.cc_fallback_probe import CCFallbackProbeWorker
from genesis.util.proc_kill import ProcessCleanupUncertain
from tests.test_cc.test_managed_fallback import answer, readiness, settle, start
from tests.test_cc.test_managed_fallback import managed as managed


@pytest.mark.parametrize("provider", ["claude", "backup"])
@pytest.mark.parametrize("kind", ["quota", "rate", "credit", "wrapped", "mcp", "local"])
async def test_actual_refusal_updates_only_attributable_health(managed, provider, kind):
    status = AsyncMock()
    managed.runner._rt._cc_invoker = CCInvoker(on_cc_status_change=status)
    peer_availability.note_success("backup")
    errors = {
        "quota": CCQuotaExhaustedError(),
        "rate": CCRateLimitError(),
        "credit": CCProcessError("insufficient credit"),
        "wrapped": CCReplayUnsafeError(cause=CCQuotaExhaustedError()),
        "mcp": CCReplayUnsafeError("MCP tool failure", cause=CCQuotaExhaustedError()),
        "local": CCProcessError("local validation failure"),
    }

    async def run(invocation, on_event=None):
        if invocation.disable_tools:
            if provider == "backup" and invocation.anthropic_base_url is None:
                raise CCQuotaExhaustedError()
            return await readiness(on_event)
        managed.tasks.append(invocation)
        await invocation.on_spawn(424215)
        raise errors[kind]

    managed.invoker.run_streaming.side_effect = run
    row, _ = await settle(managed, await start(managed))
    assert row["status"] == "failed" and len(managed.tasks) == 1
    refused = kind not in ("mcp", "local")
    if provider == "claude":
        assert status.call_args.args == (
            ("RATE_LIMITED" if kind == "rate" else "UNAVAILABLE") if refused else "NORMAL",
        )
        assert peer_availability.read_peer("backup").available
    else:
        status.assert_awaited_once_with("UNAVAILABLE")
        assert peer_availability.read_peer("backup").available == (not refused)


@pytest.mark.parametrize("provider", ["claude", "backup"])
@pytest.mark.parametrize(
    "states,overage,refused",
    [
        (["rejected"], False, True),
        (["rejected", "allowed"], False, False),
        (["allowed", "rejected"], False, True),
        (["rejected"], True, False),
    ],
)
async def test_returned_work_keeps_final_quota_evidence(
    managed, provider, states, overage, refused
):
    status = AsyncMock()
    managed.runner._rt._cc_invoker = CCInvoker(on_cc_status_change=status)

    async def run(invocation, on_event=None):
        if invocation.disable_tools:
            if provider == "backup" and invocation.anthropic_base_url is None:
                raise CCQuotaExhaustedError()
            return await readiness(on_event)
        managed.tasks.append(invocation)
        await invocation.on_spawn(424215)
        for state in states:
            await on_event(
                StreamEvent(
                    "rate_limit_event",
                    raw={
                        "type": "rate_limit_event",
                        "rate_limit_info": {"status": state, "isUsingOverage": overage},
                    },
                )
            )
        return answer()

    managed.invoker.run_streaming.side_effect = run
    row, _ = await settle(managed, await start(managed))
    assert row["status"] == "completed" and len(managed.tasks) == 1
    if provider == "claude":
        assert status.call_args.args == ("RATE_LIMITED" if refused else "NORMAL",)
    else:
        status.assert_awaited_once_with("UNAVAILABLE")
        assert peer_availability.read_peer("backup").available == (not refused)


@pytest.mark.parametrize("failure", ["local", "cancel"])
async def test_prelaunch_exit_preserves_previous_execution_history(managed, failure):
    await record_fallback(managed.db, "last", "previous-model", success=True)

    async def run(invocation, on_event=None):
        if invocation.disable_tools:
            return await managed.execute(invocation, on_event)
        if failure == "cancel":
            raise asyncio.CancelledError
        raise CCProcessError("pre-spawn local failure")

    managed.invoker.run_streaming.side_effect = run
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "failed" and not meta.get("execution_started")
    values = await managed.db.execute_fetchall(
        "SELECT provider_used,success FROM call_site_last_run WHERE call_site_id='cc_fallback'"
    )
    assert tuple(values[0]) == ("last", 1)


@pytest.mark.parametrize("probe", [False, True])
async def test_supplied_downgrade_callback_reports_actual_execution_only(probe):
    callback = AsyncMock()
    invoker = CCInvoker(on_model_downgrade=callback)
    with isolated_provider(probe=probe):
        await invoker._fire_downgrade_callback(
            answer(model_requested="opus", model_used="sonnet", downgraded=True)
        )
    assert callback.await_count == (0 if probe else 1)


@pytest.mark.parametrize("cleanup", ["confirmed", "cancelled", "uncertain"])
async def test_group_ownership_survives_pruning_and_same_key_registration(monkeypatch, cleanup):
    invoker = CCInvoker()
    proc = SimpleNamespace(pid=424215, returncode=0)
    invoker._register_proc("old", proc, confirm_cleanup=True)
    other = SimpleNamespace(pid=424216, returncode=None)
    key = invoker._register_proc("old", other)
    assert key != "old" and invoker._active_procs["old"] is proc
    error = {
        "confirmed": None,
        "cancelled": asyncio.CancelledError(),
        "uncertain": ProcessCleanupUncertain(),
    }[cleanup]
    monkeypatch.setattr(invoker_module, "confirm_process_exit", AsyncMock(side_effect=error))
    if error is None:
        await invoker._confirm_registered_exit(proc, "old")
    else:
        with pytest.raises(type(error)):
            await invoker._confirm_registered_exit(proc, "old")
    assert ("old" in invoker._active_procs) == (cleanup == "uncertain")
    assert ("old" in invoker._cleanup_owned_procs) == (cleanup == "uncertain")


async def test_idle_uncertainty_retains_identity_and_directory_across_stop_start(tmp_path):
    worker = CCFallbackProbeWorker(invoker=CCInvoker(), interval_s=0)
    proc = SimpleNamespace(pid=424215)
    error = ProcessCleanupUncertain(process=proc)
    error.probe_directory = tmp_path
    worker._probe_once = AsyncMock(side_effect=error)
    worker.start()
    await asyncio.wait_for(worker._task, 2)
    await worker.stop()
    worker.start()
    assert worker._task is None and worker._cleanup_uncertain
    assert worker._uncertain_process is proc and worker._uncertain_directory == tmp_path
