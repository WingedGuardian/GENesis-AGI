"""Standalone-installed runtime, real approval/runner/HTTP, external adapters faked."""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from flask import Flask
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from genesis.cc.exceptions import CCRateLimitError
from genesis.dashboard.routes.agent_api import ROOT, agent_api_bp
from genesis.peers import provider_state
from genesis.peers.recovery import PeerRecovery
from genesis.peers.tasks import TaskRefusal
from genesis.runtime.init import peers as installation
from tests.test_peers import test_coordinator_flow as flow
from tests.test_peers.test_api import configure, task_message


class InstalledFixture(SimpleNamespace):
    def __repr__(self):
        return "<isolated peer runtime fixture>"


@pytest.fixture
async def installed(tmp_path, monkeypatch):
    registry = flow.PeerRegistry(tmp_path / "state.db")
    async with aiosqlite.connect(registry.path) as db:
        for name in flow.TABLES:
            if name.startswith("peer_") or name in {
                "peers",
                "cc_sessions",
                "direct_session_queue",
                "approval_requests",
                "cc_rate_limit_parks",
            }:
                await db.execute(flow.TABLES[name])
        for sql in flow.INDEXES:
            if " ON cc_rate_limit_parks" in sql:
                await db.execute(sql)
        await db.commit()
    headers = await configure(registry, monkeypatch)
    headers["A2A-Version"] = "1.0"
    await registry.grant("muse", "conversation", "ask")
    resource = await flow.PublishedResources(registry).publish("Fixture", "Public fixture")
    await registry.grant("muse", "resource:" + resource["resource_id"], "ask")
    private_home = Path(tempfile.mkdtemp(prefix="pr-", dir=tmp_path.parent.parent))
    monkeypatch.setattr(installation, "genesis_home", lambda: private_home)
    monkeypatch.setattr(installation, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.delenv("GENESIS_RATE_LIMIT_RESUME_DISABLED", raising=False)
    monkeypatch.setattr(
        provider_state.config, "load_config", lambda: dict(provider_state.config.DEFAULTS)
    )
    monkeypatch.setattr(
        provider_state.scheduling, "next_attempt_at", lambda reset, now, cfg: now.isoformat()
    )
    notifications, calls = [], []

    async def send_message(recipient, text, *, message_thread_id, reply_markup):
        notifications.append(reply_markup)
        return "fixture_delivery"

    async def invoke(invocation, on_event):
        calls.append(invocation)
        entry = json.loads(Path(invocation.mcp_config).read_text())["mcpServers"]["genesis_peer"]
        params = StdioServerParameters(
            command=entry["command"],
            args=entry["args"],
            cwd=invocation.working_dir,
            env={
                "HOME": str(Path.home()),
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(Path.cwd() / "src"),
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            assert not (await client.call_tool("task_context", {})).isError
            assert not (
                await client.call_tool(
                    "resource_read",
                    {
                        "resource_id": resource["resource_id"],
                    },
                )
            ).isError
        if len(calls) == 2:
            raise CCRateLimitError("Fixture provider interruption")
        await on_event(flow.StreamEvent("result"))
        return flow.CCOutput("fixture-cli", "Installed peer answer.", "sonnet", 0, 0, 0, 1, 0)

    async with registry.connection() as db:
        runtime = flow.GenesisRuntime()
        runtime._PAUSE_FILE = tmp_path / "paused.json"
        runtime._db, runtime._bootstrapped = db, True
        runtime._autonomy_manager = SimpleNamespace(
            get_state=AsyncMock(
                return_value=SimpleNamespace(
                    current_level=2,
                    total_corrections=0,
                    total_successes=0,
                )
            )
        )
        runtime._approval_manager = flow.ApprovalManager(db=db)
        runtime._outreach_pipeline = flow.OutreachPipeline(
            flow.GovernanceGate(flow._DEFAULTS, db),
            None,
            flow.ContentFormatter(),
            {"telegram": SimpleNamespace(send_message=send_message)},
            config=flow._DEFAULTS,
            recipients={"telegram": "fixture_owner"},
            deferred_queue=SimpleNamespace(
                has_open=AsyncMock(return_value=False), enqueue=AsyncMock()
            ),
        )
        runtime._autonomous_cli_approval_gate = flow.AutonomousCliApprovalGate(
            runtime=runtime,
            approval_manager=runtime._approval_manager,
        )
        runtime._autonomous_cli_approval_gate._policy = lambda: SimpleNamespace(
            reask_hours_for=lambda _: 0
        )
        invoker = SimpleNamespace(run_streaming=invoke)
        runtime._session_manager = flow.SessionManager(db=db, invoker=invoker, day_boundary_hour=0)
        runtime._direct_session_runner = flow.DirectSessionRunner(
            invoker=invoker,
            session_manager=runtime._session_manager,
            config_builder=SimpleNamespace(_load_identity_block=lambda: "Fixture identity"),
            runtime=runtime,
        )
        app = Flask(__name__)
        app.secret_key = os.urandom(32)
        app.config.update(
            GENESIS_EVENT_LOOP=asyncio.get_running_loop(), GENESIS_PEER_REGISTRY=registry
        )
        app.before_request(flow.auth.check_api_network_scope)
        app.before_request(flow.auth.check_api_mutation_auth)
        app.register_blueprint(flow.blueprint)
        app.register_blueprint(agent_api_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
        monkeypatch.setattr(flow.PeerSegment, "stop_and_drain", AsyncMock(return_value=None))
        try:
            yield InstalledFixture(
                runtime=runtime,
                registry=registry,
                app=app,
                client=client,
                headers=headers,
                notifications=notifications,
                calls=calls,
            )
        finally:
            if runtime._peer_runtime is not None:
                await runtime._peer_runtime.stop()
            shutil.rmtree(private_home)


async def start(fixture):
    await fixture.runtime._init_peers()
    service = fixture.runtime._peer_runtime
    fixture.app.config.update(
        GENESIS_PEER_TASKS=service,
        GENESIS_PEER_RESULTS=service.results,
        GENESIS_PEER_APPROVALS=service.coordinator.approvals,
    )
    return service


async def request(fixture, method, path, **kwargs):
    return await asyncio.to_thread(
        fixture.client.open, ROOT + path, method=method, headers=fixture.headers, **kwargs
    )


async def wait_for(predicate):
    async with asyncio.timeout(15):
        while not await predicate():
            await asyncio.sleep(0.01)


async def resolve_owner(fixture, identifier):
    with (
        patch.object(flow.GenesisRuntime, "instance", return_value=fixture.runtime),
        patch.object(flow.auth, "get_dashboard_password", return_value="fixture-only"),
        patch.object(flow.auth, "api_mutation_auth_disabled", return_value=False),
    ):
        resolved = await asyncio.to_thread(
            fixture.client.post,
            "/api/genesis/approvals/" + identifier + "/resolve",
            json={"decision": "approved"},
            headers={"Origin": "http://localhost"},
        )
        assert resolved.status_code == 200


async def test_installed_http_owner_approval_resume_result(installed):
    service = await start(installed)
    assert await service.execution_allowed()
    health = await request(installed, "GET", "/health")
    assert health.status_code == 200 and health.json["task_service_ready"]
    card = await request(installed, "GET", "/.well-known/agent-card.json")
    assert {skill["id"] for skill in card.json["skills"]} == {"conversation", "research"}
    sent = await request(installed, "POST", "/message:send", json=task_message())
    assert sent.status_code == 200
    task_id = sent.json["task"]["id"]
    identity = await installed.registry.get("muse")
    for count in (1, 2):

        async def notified(count=count):
            return len(installed.notifications) == count and bool(
                await service.coordinator.approvals.pending(identity)
            )

        try:
            await wait_for(notified)
        except TimeoutError:
            row = await service.owned(identity, task_id)
            pending = await service.coordinator.approvals.pending(identity)
            raise AssertionError(
                (
                    count,
                    len(installed.notifications),
                    len(installed.calls),
                    row["state"],
                    service.poll.done(),
                    [p.get("notification_error") for p in pending],
                )
            ) from None
        assert len(installed.calls) == count - 1
        pending = await service.coordinator.approvals.pending(identity)
        await resolve_owner(installed, pending[0]["approval_id"])

    async def completed():
        return (await service.owned(identity, task_id))["state"] == "completed"

    await wait_for(completed)
    fetched = await request(installed, "GET", "/tasks/" + task_id)
    assert fetched.json["status"]["message"]["parts"][0]["text"] == "Installed peer answer."
    artifact_id = fetched.json["artifacts"][0]["artifactId"]
    download = await request(installed, "GET", "/tasks/" + task_id + "/artifacts/" + artifact_id)
    assert download.status_code == 200 and download.data == b"Installed peer answer."
    assert len(installed.calls) == 3 and len(installed.notifications) == 2
    async with installed.registry.connection() as db:
        park = await (await db.execute("SELECT * FROM cc_rate_limit_parks")).fetchone()
        assert park["status"] == "resumed" and park["attempts"] == 0
        assert (
            await (await db.execute("SELECT sum(admissions) FROM peer_daily_admissions")).fetchone()
        )[0] == 1
        assert (await (await db.execute("SELECT count(*) FROM peer_task_consents")).fetchone())[
            0
        ] == 2
    assert installed.runtime._direct_session_runner._semaphore._value == 2
    await service.stop()
    assert not await service.execution_allowed()
    assert (await request(installed, "GET", "/health")).json["task_service_ready"] is False


@pytest.mark.parametrize(
    "missing", ["disabled", "bootstrap", "runner", "session", "approval", "gate"]
)
async def test_installation_stays_unavailable_without_prerequisites(installed, missing):
    runtime = installed.runtime
    if missing == "disabled":
        await installed.registry.configure("disabled")
    elif missing == "bootstrap":
        runtime._bootstrapped = False
    else:
        setattr(
            runtime,
            {
                "runner": "_direct_session_runner",
                "session": "_session_manager",
                "approval": "_approval_manager",
                "gate": "_autonomous_cli_approval_gate",
            }[missing],
            None,
        )
    await runtime._init_peers()
    service = runtime._peer_runtime
    assert service.recovered and service.coordinator is None
    assert runtime._peer_session_lifecycle is None
    assert not await service.ready()
    await service.stop()


async def test_pause_and_disable_stop_admission_and_claim(installed):
    service = await start(installed)
    runtime = installed.runtime
    runtime.set_paused(True, "fixture")
    assert (
        await request(installed, "POST", "/message:send", json=task_message())
    ).status_code == 503
    assert await service.coordinator.dispatch_one() is None
    assert not installed.calls
    runtime.set_paused(False)
    assert await service.execution_allowed()
    await installed.registry.configure("disabled")
    assert not await service.execution_allowed()
    assert await service.coordinator.dispatch_one() is None


async def test_repeated_init_and_private_per_boot_broker(installed):
    first = await start(installed)
    await installed.runtime._init_peers()
    assert installed.runtime._peer_runtime is first
    first_paths = set((first.directory / "broker").iterdir())
    assert len(first_paths) == 1
    await first.stop()
    installed.runtime._peer_runtime = installed.runtime._peer_session_lifecycle = None
    second = await start(installed)
    assert await second.ready()
    assert len(set((second.directory / "broker").iterdir()) - first_paths) == 1


async def test_peer_stop_failure_does_not_block_unrelated_sender_shutdown(installed, caplog):
    runtime = installed.runtime
    runtime._peer_runtime = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("private")))
    runtime._outreach_recovery_worker = SimpleNamespace(stop=AsyncMock())
    await runtime.stop_outbound_senders()
    runtime._outreach_recovery_worker.stop.assert_awaited_once()
    assert "Peer shutdown requires reconciliation" in caplog.text
    assert "private" not in caplog.text
    runtime._bootstrapped = False
    await runtime.shutdown()
    runtime._peer_runtime = None


async def test_broker_start_failure_keeps_owned_partial_setup_dark(installed, monkeypatch):
    monkeypatch.setattr(
        flow.PeerCoordinator, "start", AsyncMock(side_effect=RuntimeError("fixture"))
    )
    with pytest.raises(RuntimeError):
        await installed.runtime._init_peers()
    service = installed.runtime._peer_runtime
    assert service is not None and service.stopping and service.poll is None
    assert installed.runtime._peer_session_lifecycle is None
    assert not await service.ready()
    await installed.runtime._init_peers()
    assert installed.runtime._peer_runtime is service


async def test_poll_failure_refuses_http_admission(installed, monkeypatch):
    service = await start(installed)
    monkeypatch.setattr(service.coordinator, "tick", AsyncMock(side_effect=RuntimeError("private")))

    async def failed():
        return service.poll.done()

    await wait_for(failed)
    with pytest.raises(RuntimeError, match="Peer runtime polling failed"):
        await service.poll
    assert service.coordinator._stopping
    assert not await service.ready()
    assert (
        await request(installed, "POST", "/message:send", json=task_message())
    ).status_code == 503


@pytest.mark.parametrize("failure", ["settings", "tick", "cancel"])
async def test_monitor_loss_fences_running_session(installed, monkeypatch, failure):
    await installed.registry.grant("muse", "conversation", "allow")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(invocation, on_event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(installed.runtime._direct_session_runner._invoker, "run_streaming", blocked)
    service = await start(installed)
    response = await request(installed, "POST", "/message:send", json=task_message())
    assert response.status_code == 200
    async with asyncio.timeout(15):
        await started.wait()
    binding = next(iter(service.coordinator._bindings.values()))
    session_id = service.coordinator._sessions[binding.segment.segment_id]
    running = service.coordinator.runner._active[session_id]
    settings = service.registry.settings
    if failure == "cancel":
        service.poll.cancel()
        expected = asyncio.CancelledError
    else:
        target = service.registry if failure == "settings" else service.coordinator
        monkeypatch.setattr(target, failure, AsyncMock(side_effect=RuntimeError("private")))
        expected = RuntimeError
    async with asyncio.timeout(15):
        with pytest.raises(expected):
            await service.poll
        await cancelled.wait()
        await asyncio.gather(running, return_exceptions=True)
    monkeypatch.setattr(service.registry, "settings", settings)
    assert service.coordinator._stopping
    assert binding.segment.segment_id in service.coordinator.broker._revoked_segments
    assert session_id not in service.coordinator.runner._active
    assert not await service.ready()
    fetched = await request(installed, "GET", "/tasks/" + response.json["task"]["id"])
    assert fetched.status_code == 200
    assert fetched.json["status"]["state"] == "TASK_STATE_FAILED"
    assert not fetched.json.get("artifacts")


async def test_shutdown_cancels_notifications_before_transport_close(installed):
    service = await start(installed)
    began, cancelled = asyncio.Event(), asyncio.Event()

    async def delivery():
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    notification = asyncio.create_task(delivery())
    service.coordinator._notifications["fixture"] = notification
    await began.wait()
    await installed.runtime.stop_outbound_senders()
    assert cancelled.is_set() and notification.cancelled()
    assert not await service.ready()


async def test_shutdown_does_not_wait_forever_for_unconfirmed_close(installed, monkeypatch, caplog):
    service = await start(installed)
    release = asyncio.Event()
    monkeypatch.setattr(installation, "_SHUTDOWN_GRACE_S", 0.01)
    real_close = service.coordinator.close

    async def delayed():
        await release.wait()
        await real_close()

    monkeypatch.setattr(service.coordinator, "close", delayed)
    try:
        async with asyncio.timeout(1):
            await service.stop()
        assert not service.closing.done() and not await service.ready()
        assert "Peer scope shutdown requires reconciliation" in caplog.text
    finally:
        release.set()
        await service.closing


async def test_disable_during_ceiling_check_cannot_commit_begin(installed, monkeypatch):
    from genesis.peers import coordinator as coordinator_module

    service = await start(installed)
    coordinator = service.coordinator
    await installed.registry.grant("muse", "conversation", "allow")
    identity = await installed.registry.get("muse")
    async with coordinator._dispatch_lock:
        await service.admit(identity, task_message()["message"])
        row = await coordinator.state.claim()
        binding = coordinator._binding(row | {"task_id": row["id"], "id": row["segment_id"]})

        async def ceiling(runtime):
            await installed.registry.configure("disabled")
            return 2

        monkeypatch.setattr(coordinator_module, "_ceiling", ceiling)
        with pytest.raises(TaskRefusal):
            await coordinator.begin(binding, "fixture-session", 2)
        async with installed.registry.connection() as db:
            segment = await (
                await db.execute("SELECT status,started_at FROM peer_segments")
            ).fetchone()
            assert segment["status"] == "prepared" and segment["started_at"] is None
    assert not installed.calls
    assert await PeerRecovery(installed.registry, service.directory / "segments").run()


async def test_pause_after_queue_insert_rolls_back_entire_admission(installed, monkeypatch):
    from genesis.peers import tasks as tasks_module

    await start(installed)
    original = tasks_module.queue.insert_prepared

    async def pause_after_insert(db, item):
        await original(db, item)
        installed.runtime.set_paused(True, "fixture")

    monkeypatch.setattr(tasks_module.queue, "insert_prepared", pause_after_insert)
    sent = await request(installed, "POST", "/message:send", json=task_message())
    assert sent.status_code == 503
    async with installed.registry.connection() as db:
        for name in (
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
            "direct_session_queue",
        ):
            assert (await (await db.execute("SELECT COUNT(*) FROM " + name)).fetchone())[0] == 0
    assert not installed.calls


@pytest.mark.parametrize("bootstrapped", [True, False])
async def test_actual_host_bootstrap_installs_after_full_bootstrap(
    installed, monkeypatch, bootstrapped
):
    from genesis.hosting.standalone import StandaloneAdapter
    from genesis.runtime import _capabilities

    runtime = installed.runtime
    calls = []

    async def full_bootstrap(*, mode):
        assert mode == "full"
        calls.append("full")
        runtime._bootstrapped = bootstrapped

    monkeypatch.setattr(runtime, "bootstrap", full_bootstrap)
    monkeypatch.setattr(flow.GenesisRuntime, "instance", lambda: runtime)
    monkeypatch.setattr(
        _capabilities, "write_bootstrap_manifest_file", lambda rt: calls.append("manifest")
    )
    monkeypatch.setattr(
        _capabilities, "write_capabilities_file", lambda rt: calls.append("capabilities")
    )
    adapter = StandaloneAdapter(no_telegram=True)
    monkeypatch.setattr(adapter, "_create_flask_app", lambda: installed.app)
    monkeypatch.setattr(adapter, "_register_blueprints", lambda: calls.append("bind"))
    await adapter.bootstrap()
    service = runtime._peer_runtime
    assert service.recovered
    assert calls[:3] == ["full", "manifest", "capabilities"]
    if bootstrapped:
        assert runtime._bootstrap_manifest["peers"] == "ok"
        assert installed.app.config["GENESIS_PEER_TASKS"] is service
        assert installed.app.config["GENESIS_PEER_REGISTRY"].path == installed.registry.path
        assert calls[-1] == "bind"
    else:
        assert service.coordinator is None and adapter._app is None
        assert runtime._bootstrap_manifest["peers"] == "degraded"


@pytest.mark.parametrize("boundary", ["pending", "approved", "consumed", "provider"])
async def test_restart_preserves_exact_hold_consent_and_allowance(installed, monkeypatch, boundary):
    service = await start(installed)
    identity = await installed.registry.get("muse")
    if boundary == "consumed":
        monkeypatch.setattr(service.coordinator, "dispatch_one", AsyncMock(return_value=None))
        # The first claim is explicit; later poll dispatches stay blocked until reboot.
    if boundary == "provider":
        monkeypatch.setattr(service.coordinator, "resume_provider", AsyncMock(return_value=False))
    sent = await request(installed, "POST", "/message:send", json=task_message())
    assert sent.status_code == 200
    task_id = sent.json["task"]["id"]
    if boundary == "consumed":
        await flow.PeerCoordinator.dispatch_one(service.coordinator)

    async def first_notification():
        return len(installed.notifications) == 1 and bool(
            await service.coordinator.approvals.pending(identity)
        )

    await wait_for(first_notification)
    pending = await service.coordinator.approvals.pending(identity)
    first_id = pending[0]["approval_id"]
    if boundary == "approved":
        await service.stop()
        await resolve_owner(installed, first_id)
    elif boundary in {"consumed", "provider"}:
        await resolve_owner(installed, first_id)
        if boundary == "consumed":

            async def submitted():
                return (await service.owned(identity, task_id))["state"] == "submitted"

            await wait_for(submitted)
        else:

            async def second_notification():
                return len(installed.notifications) == 2 and bool(
                    await service.coordinator.approvals.pending(identity)
                )

            await wait_for(second_notification)
            pending = await service.coordinator.approvals.pending(identity)
            await resolve_owner(installed, pending[0]["approval_id"])

            async def held():
                async with installed.registry.connection() as db:
                    row = await (
                        await db.execute("SELECT hold_reason FROM peer_task_runtime")
                    ).fetchone()
                    return (
                        row[0] == "provider"
                        and not await (
                            await db.execute("SELECT 1 FROM peer_segments WHERE status!='drained'")
                        ).fetchone()
                    )

            await wait_for(held)
    await service.stop()
    installed.runtime._peer_runtime = installed.runtime._peer_session_lifecycle = None
    fresh = await start(installed)
    assert fresh is not service and await fresh.ready()
    if boundary == "pending":
        pending = await fresh.coordinator.approvals.pending(identity)
        assert pending[0]["approval_id"] == first_id
        await resolve_owner(installed, first_id)
    if boundary != "provider":

        async def resource_pending():
            return len(installed.notifications) == 2 and bool(
                await fresh.coordinator.approvals.pending(identity)
            )

        await wait_for(resource_pending)
        pending = await fresh.coordinator.approvals.pending(identity)
        await resolve_owner(installed, pending[0]["approval_id"])

    async def completed():
        return (await fresh.owned(identity, task_id))["state"] == "completed"

    await wait_for(completed)
    fetched = await request(installed, "GET", "/tasks/" + task_id)
    artifact_id = fetched.json["artifacts"][0]["artifactId"]
    download = await request(installed, "GET", "/tasks/" + task_id + "/artifacts/" + artifact_id)
    assert download.status_code == 200 and download.data == b"Installed peer answer."
    assert len(installed.calls) == 3 and len(installed.notifications) == 2
    async with installed.registry.connection() as db:
        assert (
            await (await db.execute("SELECT sum(admissions) FROM peer_daily_admissions")).fetchone()
        )[0] == 1
        assert (await (await db.execute("SELECT count(*) FROM peer_task_consents")).fetchone())[
            0
        ] == 2
