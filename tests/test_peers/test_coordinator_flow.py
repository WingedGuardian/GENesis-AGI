"""Actual approval/coordinator/broker/facade cycle with fake external adapters."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from flask import Flask
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
from genesis.cc.direct_session import DirectSessionRunner
from genesis.cc.peer_segment import PeerSegment
from genesis.cc.session_manager import SessionManager
from genesis.cc.types import CCOutput, StreamEvent
from genesis.content.formatter import ContentFormatter
from genesis.dashboard import auth
from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.routes import state  # noqa: F401 — register the real owner routes
from genesis.db.schema import INDEXES, TABLES
from genesis.outreach.config import _DEFAULTS
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.peers.coordinator import PeerCoordinator
from genesis.peers.registry import PeerRegistry
from genesis.peers.resources import PublishedResources
from genesis.runtime import GenesisRuntime


@pytest.mark.parametrize("outcome", ["approved", "rejected", "revoked", "cancel", "delivery_retry"])
async def test_owner_approval_resume_through_real_facade(tmp_path, outcome):
    root = tmp_path
    registry = PeerRegistry(root / "state.db")
    async with aiosqlite.connect(registry.path) as db:
        for name in (
            "peer_settings",
            "peers",
            "peer_grants",
            "cc_sessions",
            "direct_session_queue",
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
            "approval_requests",
            "cc_rate_limit_parks",
            "peer_operation_approvals",
            "peer_segments",
            "peer_task_runtime",
            "peer_task_consents",
            "peer_operations",
            "peer_resources",
        ):
            await db.execute(TABLES[name])
        for sql in INDEXES:
            if " ON cc_rate_limit_parks" in sql:
                await db.execute(sql)
        await db.commit()
    await registry.register(
        "fixture", same_owner=True, daily_allowance=10, token_name="GENESIS_PEER_FIXTURE_TOKEN"
    )
    resource = await PublishedResources(registry).publish(
        "Fixture publication", "A low-stakes published fixture."
    )
    await registry.grant("fixture", "conversation", "ask")
    await registry.grant("fixture", "resource:" + resource["resource_id"], "ask")
    calls = []
    notifications = []
    published = []
    delivery_fails = outcome == "delivery_retry"

    async def send_message(recipient, text, *, message_thread_id, reply_markup):
        if delivery_fails:
            raise RuntimeError("Fixture transport unavailable")
        notifications.append([b.callback_data for row in reply_markup.inline_keyboard for b in row])
        return "fixture_delivery"

    async def publish(binding, session_id, result):
        assert result["cleanup_confirmed"] and result["success"]
        published.append((binding, session_id, result))

    async def invoke(invocation, on_event):
        calls.append(invocation)
        config = json.loads(Path(invocation.mcp_config).read_text())
        entry = config["mcpServers"]["genesis_peer"]
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
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            context = await client.call_tool("task_context", {})
            assert not context.isError
            content = await client.call_tool(
                "resource_read", {"resource_id": resource["resource_id"]}
            )
            assert not content.isError
        await on_event(StreamEvent("result"))
        return CCOutput("fixture-cli", "Safe fixture answer.", "sonnet", 0, 0, 0, 1, 0)

    async with registry.connection() as db:
        runtime = SimpleNamespace(
            _db=db,
            is_bootstrapped=True,
            _autonomy_manager=SimpleNamespace(
                get_state=AsyncMock(
                    return_value=SimpleNamespace(
                        current_level=2, total_corrections=0, total_successes=0
                    )
                )
            ),
        )
        manager = ApprovalManager(db=db)
        runtime._outreach_pipeline = OutreachPipeline(
            GovernanceGate(_DEFAULTS, db),
            None,
            ContentFormatter(),
            {"telegram": SimpleNamespace(send_message=send_message)},
            config=_DEFAULTS,
            recipients={"telegram": "fixture_owner"},
            deferred_queue=SimpleNamespace(
                has_open=AsyncMock(return_value=False), enqueue=AsyncMock()
            ),
        )
        gate = AutonomousCliApprovalGate(runtime=runtime, approval_manager=manager)
        gate._policy = lambda: SimpleNamespace(reask_hours_for=lambda _: 0)
        runtime._autonomous_cli_approval_gate = gate
        invoker = SimpleNamespace(run_streaming=invoke)
        runner = DirectSessionRunner(
            invoker=invoker,
            session_manager=SessionManager(db=db, invoker=invoker, day_boundary_hour=0),
            config_builder=SimpleNamespace(_load_identity_block=lambda: "Fixture identity"),
            runtime=runtime,
        )
        coordinator = PeerCoordinator(registry, runner, manager, gate, root / "private", publish)
        runtime._peer_session_lifecycle = coordinator
        app = Flask("coordinator-flow")
        app.secret_key = os.urandom(32)
        app.config["GENESIS_EVENT_LOOP"] = asyncio.get_running_loop()
        app.before_request(auth.check_api_network_scope)
        app.before_request(auth.check_api_mutation_auth)
        app.register_blueprint(blueprint)
        client = app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True

        async def resolve(identifier, decision="approved"):
            with (
                patch.object(GenesisRuntime, "instance", return_value=runtime),
                patch.object(auth, "get_dashboard_password", return_value="fixture-only"),
                patch.object(auth, "api_mutation_auth_disabled", return_value=False),
            ):
                response = await asyncio.to_thread(
                    client.post,
                    "/api/genesis/approvals/" + identifier + "/resolve",
                    json={"decision": decision},
                    headers={"Origin": "http://localhost"},
                )
                assert response.status_code == 200, (response.status_code, response.get_json())

        async def notify():
            await asyncio.gather(*tuple(coordinator._notifications.values()))

        async def stop_scope(self):
            pass

        with patch.object(PeerSegment, "stop_and_drain", stop_scope):
            await coordinator.start()
            try:
                identity = await registry.get("fixture")
                task = await coordinator.admit(
                    identity,
                    {
                        "messageId": "flow",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Read the permitted fixture resource."}],
                    },
                )
                assert await coordinator.dispatch_one() is None
                await notify()
                assert len(calls) == 0
                pending = await coordinator.approvals.pending(identity)
                assert len(pending) == 1
                if delivery_fails:
                    assert len(notifications) == 0
                    assert pending[0]["notification_error"] == "notification_unavailable"
                    delivery_fails = False
                    await coordinator.tick()
                    await notify()
                assert len(notifications) == 1
                await resolve(
                    pending[0]["approval_id"], "rejected" if outcome == "rejected" else "approved"
                )
                if outcome == "revoked":
                    await registry.grant("fixture", "conversation", "deny")
                elif outcome == "cancel":
                    await coordinator.cancel(identity, task["id"])
                if outcome in {"rejected", "revoked", "cancel"}:
                    await coordinator.tick()
                    assert not calls and not published
                    async with registry.connection() as check:
                        terminal = await (
                            await check.execute(
                                "SELECT state FROM peer_tasks WHERE id=?", (task["id"],)
                            )
                        ).fetchone()
                        assert terminal["state"] == (
                            "canceled" if outcome == "cancel" else "failed"
                        )
                        assert (
                            await (
                                await check.execute(
                                    "SELECT sum(admissions) FROM peer_daily_admissions"
                                )
                            ).fetchone()
                        )[0] == 1
                    return
                await coordinator.tick()
                active = tuple(runner._active.values())
                await asyncio.gather(*active, return_exceptions=True)
                await notify()
                assert len(calls) == 1 and len(notifications) == 2
                pending = await coordinator.approvals.pending(identity)
                assert len(pending) == 1
                await resolve(pending[0]["approval_id"])
                await coordinator.tick()
                await asyncio.gather(*tuple(runner._active.values()))
                assert len(calls) == 2 and len(notifications) == 2 and len(published) == 1
                async with registry.connection() as check:
                    assert (
                        await (
                            await check.execute("SELECT sum(admissions) FROM peer_daily_admissions")
                        ).fetchone()
                    )[0] == 1
                    assert (
                        await (
                            await check.execute("SELECT count(*) FROM peer_task_consents")
                        ).fetchone()
                    )[0] == 2
                    assert not await (
                        await check.execute("SELECT 1 FROM peer_segments WHERE status!='drained'")
                    ).fetchone()
                assert runner._semaphore._value == 2
            finally:
                await coordinator.close()
