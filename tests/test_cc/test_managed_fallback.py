"""Provider preflight shares one durable task, deadline, and cancellation owner."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aiosqlite
import pytest

from genesis.cc import (
    direct_session,
    fallback_recovery,
    fallback_state,
    peer_availability,
    roster,
    types,
)
from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner
from genesis.cc.exceptions import CCProcessError, CCQuotaExhaustedError, CCRateLimitError
from genesis.cc.managed_session import prepared_metadata
from genesis.cc.provider_readiness import READY
from genesis.cc.session_manager import SessionManager
from genesis.cc.types import CCInvocation, CCOutput, StreamEvent
from genesis.db.crud import cc_sessions
from genesis.db.schema import create_all_tables, seed_data


def answer(text="actual answer", **changes):
    return replace(
        CCOutput("provider-session", text, "reported-model", 0, 1, 1, 1, 0, tools_used=()),
        **changes,
    )


async def readiness(on_event):
    await on_event(
        StreamEvent(
            "init", raw={"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
        )
    )
    return answer(READY)


@pytest.fixture
async def managed(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path / "probes"))
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "synthetic-key")
    monkeypatch.setattr(types, "_BACKGROUND_SESSION_DIR", tmp_path / "working")
    monkeypatch.setattr(direct_session, "_BG_CC_TMP_ROOT", tmp_path / "sessions")
    alerts = AsyncMock()
    monkeypatch.setattr(fallback_recovery, "fire_fallback_alert", alerts)
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers":{}}')
    config = {
        "default": "claude",
        "models": {
            "claude": {"native_subscription": True, "failover_order": 0},
            "backup": {
                "anthropic_base_url": "https://provider.example.org",
                "model_id": "backup-id",
                "auth_env": "SYNTHETIC_PROVIDER_KEY",
                "failover_order": 1,
            },
            "last": {
                "anthropic_base_url": "https://last.example.org",
                "model_id": "last-id",
                "auth_env": "SYNTHETIC_PROVIDER_KEY",
                "failover_order": 2,
            },
        },
    }
    monkeypatch.setattr(roster, "load_roster", lambda: config)
    db = await aiosqlite.connect(tmp_path / "receipts.db")
    await db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    await seed_data(db)
    await db.commit()
    calls, tasks = [], []

    async def execute(invocation, on_event=None):
        calls.append(invocation)
        if invocation.disable_tools:
            if not invocation.anthropic_base_url:
                raise CCQuotaExhaustedError("synthetic refusal")
            return await readiness(on_event)
        tasks.append(invocation)
        return answer()

    invoker = SimpleNamespace(run_streaming=AsyncMock(side_effect=execute))
    runtime = SimpleNamespace(
        _db=db,
        _autonomy_manager=SimpleNamespace(
            effective_level=AsyncMock(return_value=3), get_state=AsyncMock(return_value=None)
        ),
    )
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=SessionManager(db=db),
        runtime=runtime,
        config_builder=SimpleNamespace(build_mcp_config=lambda **kwargs: str(mcp)),
    )
    native_build = runner._build_invocation
    runner._build_invocation = lambda req, sid: CCInvocation(
        prompt=req.prompt,
        system_prompt=req.system_prompt,
        roster_eligible=True,
        disallowed_tools=["Write"],
        timeout_s=req.timeout_s,
    )
    yield SimpleNamespace(
        db=db,
        runner=runner,
        invoker=invoker,
        calls=calls,
        tasks=tasks,
        execute=execute,
        config=config,
        native_build=native_build,
        alerts=alerts,
    )
    await runner.shutdown()
    await db.close()


async def start(b, *, adopt=True, roster_model=None, **meta):
    req = DirectSessionRequest(
        prompt="private actual task",
        system_prompt="Synthetic independent Genesis identity",
        profile="observe",
        source_tag="agent_api",
        caller_context="agent_api:synthetic",
        managed=True,
        notify=False,
        roster_model=roster_model,
    )
    sid = str(uuid4())
    fields = prepared_metadata(req)
    fields.update(meta)
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        b.db,
        id=sid,
        session_type="background_task",
        model="sonnet",
        effort="low",
        source_tag="agent_api",
        channel="agent",
        origin_class="external_untrusted",
        metadata=json.dumps(fields),
        started_at=now,
        last_activity_at=now,
    )
    if adopt:
        await b.runner.adopt(sid, req)
    return sid


async def settle(b, sid):
    await asyncio.wait_for(asyncio.shield(b.runner._active[sid]), 5)
    row = await cc_sessions.get_by_id(b.db, sid)
    return row, json.loads(row["metadata"])


async def test_refusal_selects_backup_and_executes_actual_task_once(managed):
    b = managed
    sid = await start(b)
    row, meta = await settle(b, sid)
    assert row["status"] == "completed" and meta["native_state"] == "succeeded"
    assert [inv.disable_tools for inv in b.calls] == [True, True, False]
    assert len(b.tasks) == 1 and b.tasks[0].prompt == "private actual task"
    assert b.tasks[0].disallowed_tools == ["Write"]
    assert b.tasks[0].origin == "external_untrusted" and not b.tasks[0].supervised
    assert b.tasks[0].confirm_cleanup and meta["execution_provider"] == "backup"
    assert [attempt["outcome"] for attempt in meta["provider_attempts"]] == ["refused", "ready"]
    assert meta["roster_endpoint"]["roster_model"] == "backup"
    assert fallback_state.read().fallback == "backup"
    monitor = await (
        await b.db.execute(
            "SELECT provider_used,success,response_text FROM call_site_last_run WHERE call_site_id='cc_fallback'"
        )
    ).fetchone()
    assert tuple(monitor) == ("backup", 1, None)
    assert not b.runner._managed_slots


async def test_home_ready_executes_native_without_fallback(managed):
    async def run(invocation, on_event=None):
        if invocation.disable_tools:
            await on_event(
                StreamEvent(
                    "init",
                    raw={"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
                )
            )
            return answer(READY)
        managed.tasks.append(invocation)
        return answer()

    managed.invoker.run_streaming.side_effect = run
    fallback_state.enter("claude", "backup", "rate_limit")
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "completed" and len(managed.tasks) == 1
    assert managed.tasks[0].anthropic_base_url is None
    assert not meta["fallback_attempted"] and not fallback_state.read().is_fallback


@pytest.mark.parametrize("route", ["home", "backup", "changed_home"])
async def test_only_current_home_recovery_clears_shared_status_and_banner(managed, route):
    from genesis.cc.invoker import CCInvoker

    status = AsyncMock()
    shared = CCInvoker(on_cc_status_change=status)
    managed.runner._rt._cc_invoker = shared
    await shared._notify_status_change(CCQuotaExhaustedError())
    fallback_state.enter("claude", "backup", "rate_limit")

    async def run(invocation, on_event=None):
        if route == "backup":
            return await managed.execute(invocation, on_event=on_event)
        if invocation.disable_tools:
            if route == "changed_home":
                managed.config["default"] = "backup"
            await on_event(
                StreamEvent(
                    "init",
                    raw={"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
                )
            )
            return answer(READY)
        return answer()

    managed.invoker.run_streaming.side_effect = run
    await settle(managed, await start(managed))
    assert fallback_state.read().is_fallback == (route != "home")
    assert (status.call_args.args == ("NORMAL",)) == (route == "home")


async def test_backup_refusal_advances_once_to_next_provider(managed):
    async def run(invocation, on_event=None):
        if invocation.disable_tools and invocation.model_id_override == "backup-id":
            managed.calls.append(invocation)
            raise CCRateLimitError("synthetic backup limit")
        return await managed.execute(invocation, on_event)

    managed.invoker.run_streaming.side_effect = run
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "completed" and meta["execution_provider"] == "last"
    assert len(managed.tasks) == 1
    assert len(meta["provider_attempts"]) == 3


@pytest.mark.parametrize(
    "error", [CCProcessError("local fault"), CCQuotaExhaustedError("all limited")]
)
async def test_failed_selection_executes_no_task(managed, error):
    managed.invoker.run_streaming.side_effect = error
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "failed" and meta["error"] == "execution_failed"
    assert not managed.tasks and not managed.runner._managed_slots
    expected = 3 if isinstance(error, CCQuotaExhaustedError) else 1
    assert managed.invoker.run_streaming.await_count == expected
    assert not fallback_state.read().is_fallback
    assert not await managed.db.execute_fetchall(
        "SELECT 1 FROM call_site_last_run WHERE call_site_id='cc_fallback'"
    )


async def test_mid_task_limit_does_not_replay_side_effect(managed, tmp_path):
    marker = tmp_path / "one-effect"

    async def run(invocation, on_event=None):
        if invocation.disable_tools:
            return await managed.execute(invocation, on_event)
        marker.write_text(marker.read_text() + "effect\n" if marker.exists() else "effect\n")
        managed.tasks.append(invocation)
        raise CCRateLimitError("limit after work")

    managed.invoker.run_streaming.side_effect = run
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "failed" and len(managed.tasks) == 1
    assert marker.read_text() == "effect\n"
    assert not fallback_state.read().is_fallback
    assert not meta.get("rate_limit_park_id")


@pytest.mark.parametrize("phase", ["probe", "execution"])
async def test_cancel_during_either_phase_starts_no_new_provider(managed, phase):
    entered = asyncio.Event()

    async def run(invocation, on_event=None):
        if (phase == "probe") == invocation.disable_tools:
            entered.set()
            await asyncio.Event().wait()
        return await managed.execute(invocation, on_event)

    managed.invoker.run_streaming.side_effect = run
    sid = await start(managed)
    await asyncio.wait_for(entered.wait(), 2)
    assert managed.runner.cancel(sid)
    calls = managed.invoker.run_streaming.await_count
    row, meta = await settle(managed, sid)
    assert meta["native_state"] == "cancelled" and row["status"] == "failed"
    assert managed.invoker.run_streaming.await_count == calls
    assert not managed.runner._managed_slots and not fallback_state.read().is_fallback


async def test_probe_uses_remaining_receipt_budget(managed):
    sid = await start(managed, deadline_at=(datetime.now(UTC) + timedelta(seconds=4)).isoformat())
    await settle(managed, sid)
    assert all(0 < inv.timeout_s <= 4 for inv in managed.calls if inv.disable_tools)


async def test_no_false_success_when_result_is_partial(managed):
    async def run(invocation, on_event=None):
        if invocation.disable_tools:
            return await managed.execute(invocation, on_event)
        return answer(result_received=False)

    managed.invoker.run_streaming.side_effect = run
    row, _ = await settle(managed, await start(managed))
    assert row["status"] == "failed" and not fallback_state.read().is_fallback
    value = await (
        await managed.db.execute(
            "SELECT success FROM call_site_last_run WHERE call_site_id='cc_fallback'"
        )
    ).fetchone()
    assert value[0] == 0


async def test_roster_change_cannot_retarget_selected_work_or_saved_endpoint(managed, monkeypatch):
    async def run(invocation, on_event=None):
        output = await managed.execute(invocation, on_event)
        if invocation.disable_tools:
            changed = {"default": "last", "models": {}}
            monkeypatch.setattr(roster, "load_roster", lambda: changed)
        return output

    managed.invoker.run_streaming.side_effect = run
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "completed" and len(managed.tasks) == 1
    assert managed.tasks[0].model_id_override == "backup-id"
    assert meta["roster_endpoint"]["model_id"] == "backup-id"


async def test_artifact_failure_is_not_successful_fallback(managed, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic artifact refusal")

    monkeypatch.setattr("genesis.cc.managed_session.atomic_write_text", fail)
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "failed" and meta["native_state"] == "failed"
    assert not fallback_state.read().is_fallback
    value = await (
        await managed.db.execute(
            "SELECT success FROM call_site_last_run WHERE call_site_id='cc_fallback'"
        )
    ).fetchone()
    assert value[0] == 0


async def test_selection_does_not_commit_the_runtime_connections_transaction(managed):
    from genesis.cc.managed_fallback import select_provider

    sid = await start(managed, adopt=False)
    await managed.db.execute("BEGIN")
    await managed.db.execute("SELECT id FROM cc_sessions")
    await select_provider(
        managed.runner,
        SimpleNamespace(roster_model=None),
        sid,
        CCInvocation(prompt="private actual task", roster_eligible=True),
    )
    assert managed.db.in_transaction
    await managed.db.rollback()
    assert not managed.tasks


@pytest.mark.parametrize("missing", ["anthropic_base_url", "model_id", "auth_env"])
async def test_unusable_backup_does_not_hide_a_later_provider(managed, missing):
    managed.config["models"]["backup"].pop(missing)
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "completed" and meta["execution_provider"] == "last"
    assert len(managed.tasks) == 1
    assert [attempt["provider"] for attempt in meta["provider_attempts"]] == ["claude", "last"]


@pytest.mark.parametrize("missing", ["anthropic_base_url", "model_id", "auth_env"])
@pytest.mark.parametrize("pinned", [False, True])
async def test_unusable_home_or_pin_fails_without_running_elsewhere(managed, missing, pinned):
    managed.config["models"]["backup"].pop(missing)
    managed.config["default"] = "claude" if pinned else "backup"
    row, meta = await settle(
        managed, await start(managed, roster_model="backup" if pinned else None)
    )
    assert row["status"] == "failed" and meta["error"] == "execution_failed"
    managed.invoker.run_streaming.assert_not_awaited()
    assert not managed.tasks


@pytest.mark.parametrize("pin", ["", "not-configured"])
async def test_unknown_pin_does_not_fall_back_to_the_global_default(managed, pin):
    row, meta = await settle(managed, await start(managed, roster_model=pin))
    assert row["status"] == "failed" and meta["error"] == "execution_failed"
    managed.invoker.run_streaming.assert_not_awaited()


@pytest.mark.parametrize("home", ["claude", "backup"])
async def test_home_refusal_is_not_a_standby_observation(managed, home):
    managed.config["default"] = home
    home_model = managed.config["models"][home].get("model_id")

    async def run(invocation, on_event=None):
        if invocation.disable_tools:
            if invocation.model_id_override == home_model:
                raise CCQuotaExhaustedError("synthetic home refusal")
            return await readiness(on_event)
        return await managed.execute(invocation, on_event)

    managed.invoker.run_streaming.side_effect = run
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "completed"
    assert meta["provider_attempts"][0]["outcome"] == "refused"
    selected = "backup" if home == "claude" else "claude"
    assert set(peer_availability.read()) == {selected}


@pytest.mark.parametrize("pin", ["claude", "native-alias"])
@pytest.mark.parametrize("ready", [False, True])
async def test_real_builder_native_pin_still_checks_readiness(managed, pin, ready):
    managed.config["models"]["native-alias"] = {"native_subscription": True}
    managed.config["default"] = "backup"
    managed.runner._build_invocation = managed.native_build

    async def run(invocation, on_event=None):
        managed.calls.append(invocation)
        if invocation.disable_tools:
            if not ready:
                raise CCQuotaExhaustedError("synthetic pinned refusal")
            return await readiness(on_event)
        managed.tasks.append(invocation)
        return answer()

    managed.invoker.run_streaming.side_effect = run
    row, meta = await settle(managed, await start(managed, roster_model=pin))
    assert row["status"] == ("completed" if ready else "failed")
    assert [inv.disable_tools for inv in managed.calls] == ([True, False] if ready else [True])
    assert all(inv.anthropic_base_url is None for inv in managed.calls)
    assert len(managed.tasks) == int(ready)
    assert meta.get("execution_provider") == (pin if ready else None)
    assert [attempt["provider"] for attempt in meta["provider_attempts"]] == [pin]


async def test_unpinned_routing_opt_out_stays_direct(managed):
    managed.runner._build_invocation = lambda req, sid: CCInvocation(prompt=req.prompt)
    row, meta = await settle(managed, await start(managed))
    assert row["status"] == "completed" and len(managed.tasks) == 1
    assert len(managed.calls) == 1 and not managed.calls[0].disable_tools
    assert "provider_attempts" not in meta


async def test_cancelled_backup_probe_is_not_a_failed_execution(managed):
    entered = asyncio.Event()

    async def run(invocation, on_event=None):
        if invocation.disable_tools and invocation.model_id_override == "backup-id":
            entered.set()
            await asyncio.Event().wait()
        return await managed.execute(invocation, on_event)

    managed.invoker.run_streaming.side_effect = run
    sid = await start(managed)
    await asyncio.wait_for(entered.wait(), 2)
    assert managed.runner.cancel(sid)
    row, meta = await settle(managed, sid)
    assert row["status"] == "failed" and meta["native_state"] == "cancelled"
    assert not managed.tasks and "execution_provider" not in meta
    assert not await managed.db.execute_fetchall(
        "SELECT 1 FROM call_site_last_run WHERE call_site_id='cc_fallback'"
    )


async def test_managed_switch_alert_follows_the_outage_transition(managed):
    ids = await asyncio.gather(start(managed), start(managed))
    await asyncio.gather(*(settle(managed, sid) for sid in ids))
    await settle(managed, await start(managed))
    assert managed.alerts.await_count == 1
    assert managed.alerts.call_args.kwargs["topic"] == "cc_fallback_switch"
    assert "private actual task" not in managed.alerts.call_args.kwargs["context"]
    fallback_state.clear()
    await settle(managed, await start(managed))
    assert managed.alerts.await_count == 2
