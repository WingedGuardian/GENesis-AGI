"""Receipt admission and disclosure through real disposable SQLite and Flask."""

import asyncio
import hashlib
import json
import secrets
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aiosqlite
import pytest
from flask import Flask
from werkzeug.serving import make_server

from genesis.cc.direct_session import DirectSessionRunner
from genesis.cc.session_manager import SessionManager
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import CCInvocation, CCOutput
from genesis.dashboard.routes.agent_api import ROOT, agent_api_bp
from genesis.db.connection import SerializedConnection, get_db
from genesis.db.schema import create_all_tables, seed_data
from genesis.peers.auth import PeerIdentity
from genesis.peers.conversation import ConversationRefusal, ConversationService, head
from genesis.peers.protocol import parse_send
from genesis.peers.registry import PeerRegistry
from genesis.peers.results import full_result, project


@pytest.fixture
async def http_bridge(bridge, monkeypatch):
    from genesis.dashboard.api import blueprint
    from genesis.dashboard.auth import check_api_mutation_auth
    from genesis.hosting.request_input import PeerRequestHandler
    from genesis.runtime import GenesisRuntime

    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(
        GENESIS_EVENT_LOOP=asyncio.get_running_loop(),
        GENESIS_PEER_RUNTIME=bridge.runtime,
        GENESIS_PEER_REGISTRY=bridge.registry,
    )
    app.before_request(check_api_mutation_auth)
    app.register_blueprint(blueprint)
    app.register_blueprint(agent_api_bp)
    bridge.runtime.is_bootstrapped = True
    monkeypatch.setattr(GenesisRuntime, "instance", classmethod(lambda cls: bridge.runtime))
    monkeypatch.setenv("DASHBOARD_PASSWORD", secrets.token_urlsafe(32))
    monkeypatch.setenv("GENESIS_DASHBOARD_API_AUTH", "on")

    class QuietHandler(PeerRequestHandler):
        def log_request(self, *args):
            pass

    http = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=http.serve_forever)
    thread.start()
    owner = app.test_client()
    with owner.session_transaction() as session:
        session["authenticated"] = True
    yield bridge, app, f"http://127.0.0.1:{http.server_port}", owner
    await asyncio.to_thread(http.shutdown)
    http.server_close()
    thread.join(2)
    assert not thread.is_alive()


@asynccontextmanager
async def sdk_client(base):
    import os

    import httpx
    from a2a.client.transports.rest import RestTransport
    from a2a.types import AgentCard
    from google.protobuf.json_format import ParseDict

    client = httpx.AsyncClient(
        headers={
            "Authorization": "Bearer " + os.environ["GENESIS_PEER_ALPHA_TOKEN"],
            "A2A-Version": "1.0",
        },
        trust_env=False,
        timeout=10,
    )
    async with client:
        card = await client.get(base + ROOT + "/.well-known/agent-card.json")
        assert card.status_code == 200
        parsed = ParseDict(card.json(), AgentCard())
        yield client, RestTransport(client, parsed, base + ROOT)


async def sdk_send(transport, payload):
    from a2a.types import SendMessageRequest
    from google.protobuf.json_format import ParseDict

    return (await transport.send_message(ParseDict(payload, SendMessageRequest()))).task


async def sdk_settle(transport, sid):
    from a2a.types import GetTaskRequest, TaskState

    for _ in range(100):
        task = await transport.get_task(GetTaskRequest(id=sid))
        if task.status.state in (
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
        ):
            return task
        await asyncio.sleep(0.02)
    pytest.fail("Native receipt did not reach terminal state")


async def test_real_sdk_http_bounded_send_poll_full_continuation(http_bridge):
    from a2a.types import ListTasksRequest, TaskState

    b, app, base, owner = http_bridge
    async with sdk_client(base) as (client, transport):
        payload = message(timeout_s=30)
        task = await sdk_send(transport, payload)
        done = await sdk_settle(transport, task.id)
        assert done.status.state == TaskState.TASK_STATE_COMPLETED
        result = await client.get(base + ROOT + f"/tasks/{done.id}/result")
        assert result.status_code == 200 and result.text == "A considered reply"
        assert (await sdk_send(transport, payload)).id == done.id
        follow = await sdk_send(transport, message(context=done.context_id))
        await sdk_settle(transport, follow.id)
        listed = await transport.list_tasks(ListTasksRequest(page_size=1))
        assert len(listed.tasks) == 1 and listed.next_page_token and listed.total_size == 2
        assert b.invoker.run_streaming.await_count == 2
        assert "A considered reply" in b.invoker.run_streaming.call_args.args[0].prompt


def _fallback_turn(b, tool_turn, monkeypatch):
    from dataclasses import replace

    from genesis.cc import roster
    from genesis.cc.exceptions import CCQuotaExhaustedError
    from genesis.cc.provider_readiness import READY
    from genesis.cc.types import StreamEvent

    monkeypatch.setenv("GENESIS_HOME", str(b.registry.path.parent))
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "synthetic-key")
    monkeypatch.setattr(
        roster,
        "load_roster",
        lambda: {
            "default": "claude",
            "models": {
                "claude": {"native_subscription": True},
                "backup": {
                    "model_id": "backup-id",
                    "anthropic_base_url": "https://provider.example.org",
                    "auth_env": "SYNTHETIC_PROVIDER_KEY",
                    "failover_order": 1,
                },
            },
        },
    )
    build = b.runner._build_invocation
    b.runner._build_invocation = lambda req, sid: replace(build(req, sid), roster_eligible=True)

    async def selected_turn(invocation, **kwargs):
        if invocation.disable_tools:
            if not invocation.anthropic_base_url:
                raise CCQuotaExhaustedError("synthetic refusal")
            await kwargs["on_event"](
                StreamEvent(
                    "init",
                    raw={"type": "system", "subtype": "init", "tools": [], "mcp_servers": []},
                )
            )
            return CCOutput("probe", READY, "backup-id", 0, 1, 1, 1, 0, tools_used=())
        return await tool_turn(invocation, **kwargs)

    return selected_turn


@pytest.mark.parametrize("waiting", [True, False])
@pytest.mark.parametrize("fallback", [False, True])
async def test_agent_gate_owner_notification_resolution_and_wait_or_hold(
    http_bridge, waiting, fallback, monkeypatch
):
    from a2a.types import GetTaskRequest, TaskState

    from genesis.autonomy.approval import ApprovalManager
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
    from genesis.autonomy.cli_policy import AutonomousCliPolicy
    from genesis.autonomy.email_gate import EmailAutonomyGate
    from genesis.autonomy.email_gate_watcher import drain_pending_email_sends
    from genesis.content.formatter import ContentFormatter
    from genesis.outreach.config import QUIET_HOURS_DISABLED, OutreachConfig
    from genesis.outreach.governance import GovernanceGate
    from genesis.outreach.pipeline import OutreachPipeline
    from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

    b, app, base, owner = http_bridge
    sent = asyncio.Event()
    adapter = SimpleNamespace(send_message=AsyncMock(return_value="owner-delivery"))
    email = SimpleNamespace(send_message=AsyncMock(return_value="synthetic-email-delivery"))
    config = OutreachConfig(
        quiet_hours=QUIET_HOURS_DISABLED,
        channel_preferences={"default": "telegram"},
        thresholds={"approval": 0, "notification": 0},
        max_daily=999,
        surplus_daily=999,
        content_daily=999,
        notification_daily=999,
        morning_report_time="07:00",
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, b.db),
        drafter=AsyncMock(),
        formatter=ContentFormatter(),
        channels={"telegram": adapter, "email": email},
        db=b.db,
        config=config,
        recipients={"telegram": "synthetic-owner"},
    )
    b.runtime._outreach_pipeline = pipeline
    manager = ApprovalManager(db=b.db)
    gate = AutonomousCliApprovalGate(
        runtime=b.runtime, approval_manager=manager, policy_loader=lambda: AutonomousCliPolicy()
    )
    b.runtime._autonomous_cli_approval_gate = gate
    pipeline.set_autonomy_gate(EmailAutonomyGate(db=b.db, approval_manager=manager))

    async def tool_turn(invocation, **kwargs):
        if waiting:
            state, approval, _ = await gate.ensure_approval(
                subsystem="peer-fixture",
                policy_id="low-stakes",
                action_label="synthetic gated action",
                invocation=invocation,
            )
            assert state == "pending"
        else:
            held = await pipeline.submit(
                OutreachRequest(
                    category=OutreachCategory.NOTIFICATION,
                    topic="Synthetic follow-up",
                    context="Low-stakes synthetic message",
                    channel="email",
                    validated_recipient="recipient@example.com",
                    salience_score=1,
                    verbatim=True,
                )
            )
            assert held.status == OutreachStatus.HELD
            email.send_message.assert_not_awaited()
        sent.set()
        if waiting:
            while (await manager.get_by_id(approval))["status"] == "pending":
                await asyncio.sleep(0.01)
            assert (await manager.get_by_id(approval))["resolved_by"] == "dashboard"
        return CCOutput(
            session_id="cli",
            text="Approved tool completed"
            if waiting
            else "Action held for owner; cognition finished",
            model_used="sonnet",
            cost_usd=0,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            exit_code=0,
        )

    b.invoker.run_streaming.side_effect = (
        _fallback_turn(b, tool_turn, monkeypatch) if fallback else tool_turn
    )
    async with sdk_client(base) as (client, transport):
        task = await sdk_send(transport, message())
        await asyncio.wait_for(sent.wait(), 3)
        pending = (await client.get(base + ROOT + "/approvals")).json()["approvals"]
        assert len(pending) == 1 and pending[0]["timeout"] is None
        approval = pending[0]["approvalId"]
        stored = await manager.get_by_id(approval)
        assert json.loads(stored["context"])["agent_session_id"] == task.id
        if waiting:
            assert json.loads(stored["context"])["delivery_id"] == "owner-delivery"
            adapter.send_message.assert_awaited_once()
        before = (
            await transport.get_task(GetTaskRequest(id=task.id))
            if waiting
            else await sdk_settle(transport, task.id)
        )
        assert before.status.state == (
            TaskState.TASK_STATE_AUTH_REQUIRED if waiting else TaskState.TASK_STATE_COMPLETED
        )
        assert before.metadata["pendingOwnerActions"] is True
        endpoint = f"/api/genesis/approvals/{approval}/resolve"
        denied = await asyncio.to_thread(
            owner.post, endpoint, json={"decision": "approved"}, headers=dict(client.headers)
        )
        assert (
            denied.status_code == 403 and (await manager.get_by_id(approval))["status"] == "pending"
        )
        resolved = await asyncio.to_thread(
            owner.post,
            endpoint,
            json={"decision": "approved"},
            headers={"Origin": "http://localhost"},
        )
        assert resolved.status_code == 200
        if not waiting:
            assert await drain_pending_email_sends(b.runtime) == 1
            assert await drain_pending_email_sends(b.runtime) == 0
            email.send_message.assert_awaited_once()
        done = await sdk_settle(transport, task.id)
        assert done.status.state == TaskState.TASK_STATE_COMPLETED
        assert (await client.get(base + ROOT + "/approvals")).json()["approvals"] == []
        assert b.invoker.run_streaming.await_count == (3 if fallback else 1)


def message(mid=None, context=None, **options):
    msg = {
        "messageId": mid or str(uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": "Please reason independently"}],
    }
    if context:
        msg["contextId"] = context
    return {"message": msg, "configuration": {"returnImmediately": True}, "metadata": options}


def incoming(payload=None):
    return parse_send(json.dumps(payload or message()).encode())


@pytest.fixture
async def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = tmp_path / "genesis.db"
    db = await get_db(path)
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    await seed_data(db)
    await db.commit()
    registry = PeerRegistry(path)
    await registry.configure("fallback", service_url="https://agent.example.org" + ROOT)
    for name in ("alpha", "beta"):
        monkeypatch.setenv(f"GENESIS_PEER_{name.upper()}_TOKEN", secrets.token_urlsafe(32))
        await registry.register(
            name, same_owner=True, token_name=f"GENESIS_PEER_{name.upper()}_TOKEN"
        )
        await registry.grant(name, "conversation", "allow")
    identities = {p["peer_id"]: PeerIdentity(p["token_name"], p) for p in await registry.rows()}
    runtime = SimpleNamespace(
        _db=db,
        _autonomy_manager=SimpleNamespace(
            effective_level=AsyncMock(return_value=3), get_state=AsyncMock(return_value=None)
        ),
    )
    invoker = SimpleNamespace(
        run_streaming=AsyncMock(
            return_value=CCOutput(
                session_id="cli",
                text="A considered reply",
                model_used="sonnet",
                cost_usd=0,
                input_tokens=1,
                output_tokens=1,
                duration_ms=1,
                exit_code=0,
            )
        )
    )
    runner = DirectSessionRunner(
        invoker=invoker,
        session_manager=SessionManager(db=db),
        runtime=runtime,
        config_builder=AsyncMock(),
    )
    runtime._direct_session_runner = runner
    runner._build_invocation = lambda req, sid: CCInvocation(
        prompt=req.prompt, system_prompt=req.system_prompt
    )
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "SOUL.md").write_text("Synthetic independent Genesis identity")
    monkeypatch.setattr(
        "genesis.peers.conversation.SystemPromptAssembler",
        lambda: SystemPromptAssembler(identity_dir=identity_dir),
    )
    service = ConversationService(registry, runtime)
    yield SimpleNamespace(
        service=service,
        registry=registry,
        runtime=runtime,
        runner=runner,
        invoker=invoker,
        identities=identities,
        db=db,
        home=tmp_path,
    )
    for sid in tuple(runner._active):
        runner.cancel(sid)
    await asyncio.gather(*tuple(runner._active.values()), return_exceptions=True)
    await db.close()


async def settle(bridge, row):
    task = bridge.runner._active.get(row["id"])
    if task:
        await asyncio.wait_for(asyncio.shield(task), 3)
    return await bridge.service.get(bridge.identities["alpha"], row["id"])


async def test_send_retry_continuation_identity_result(bridge):
    identity = bridge.identities["alpha"]
    payload = message(timeout_s=30.0)
    first = await bridge.service.send(identity, incoming(payload))
    done = await settle(bridge, first)
    assert project(done)["status"]["state"] == "TASK_STATE_COMPLETED"
    assert full_result(done) == "A considered reply"
    retry = await bridge.service.send(identity, incoming(payload))
    assert retry["id"] == done["id"]
    assert bridge.invoker.run_streaming.await_count == 1
    second = await bridge.service.send(
        identity, incoming(message(context=json.loads(done["metadata"])["context_id"]))
    )
    await settle(bridge, second)
    calls = bridge.invoker.run_streaming.call_args_list
    assert "Synthetic independent Genesis identity" in calls[0].args[0].system_prompt
    assert "A considered reply" in calls[1].args[0].prompt
    assert calls[1].args[0].origin == "external_untrusted"
    assert calls[1].args[0].supervised is False


async def test_bridge_uses_production_connection(bridge):
    assert isinstance(bridge.db, SerializedConnection)
    assert (await (await bridge.db.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
    assert (await (await bridge.db.execute("PRAGMA foreign_keys")).fetchone())[0] == 1


async def test_admission_checks_database_before_taking_write_lock(bridge, monkeypatch):
    same_database = bridge.service._same_database
    checks = 0

    async def checked(db):
        nonlocal checks
        checks += 1
        assert not db.in_transaction
        # A runtime writer must be able to finish while identity is checked.
        await bridge.db.execute("UPDATE peer_settings SET mode=mode WHERE id=1")
        await bridge.db.commit()
        await same_database(db)

    monkeypatch.setattr(bridge.service, "_same_database", checked)
    row = await asyncio.wait_for(bridge.service.send(bridge.identities["alpha"], incoming()), 3)
    assert checks == 2  # Preflight and the actual admission connection.
    monkeypatch.setattr(bridge.service, "_same_database", same_database)
    assert project(await settle(bridge, row))["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_admission_rechecks_actual_database_after_enrichment(bridge, tmp_path, monkeypatch):
    other_path = tmp_path / "other.db"
    async with aiosqlite.connect(other_path):
        pass

    class ChangedRegistryAssembler:
        async def assemble(self, **kwargs):
            bridge.registry.path = other_path
            return "Synthetic identity"

    monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", ChangedRegistryAssembler)
    with pytest.raises(ConversationRefusal, match="not_ready"):
        await bridge.service.send(bridge.identities["alpha"], incoming())
    bridge.invoker.run_streaming.assert_not_awaited()
    assert (await (await bridge.db.execute("SELECT count(*) FROM cc_sessions")).fetchone())[0] == 0
    assert bridge.service._enriching == {}


async def test_changed_retry_conflicts(bridge):
    payload = message()
    row = await bridge.service.send(bridge.identities["alpha"], incoming(payload))
    await settle(bridge, row)
    payload["message"]["parts"][0]["text"] = "different"
    with pytest.raises(ConversationRefusal, match="state_conflict"):
        await bridge.service.send(bridge.identities["alpha"], incoming(payload))


async def test_retry_also_checks_persisted_ownership(bridge):
    item = incoming()
    row = await settle(bridge, await bridge.service.send(bridge.identities["alpha"], item))
    meta = json.loads(row["metadata"])
    meta["peer_id"] = "beta"
    await bridge.db.execute(
        "UPDATE cc_sessions SET metadata=? WHERE id=?", (json.dumps(meta), row["id"])
    )
    await bridge.db.commit()
    with pytest.raises(ConversationRefusal, match="state_conflict"):
        await bridge.service.send(bridge.identities["alpha"], item)
    assert bridge.invoker.run_streaming.await_count == 1


async def test_concurrency_context_and_global_reservation(bridge):
    entered = asyncio.Event()

    async def blocked(*a, **kw):
        entered.set()
        await asyncio.Event().wait()

    bridge.invoker.run_streaming.side_effect = blocked
    identity = bridge.identities["alpha"]
    row = await bridge.service.send(identity, incoming())
    await entered.wait()
    with pytest.raises(ConversationRefusal, match="state_conflict"):
        await bridge.service.send(
            identity, incoming(message(context=json.loads(row["metadata"])["context_id"]))
        )
    await bridge.service.send(identity, incoming())
    with pytest.raises(ConversationRefusal, match="rate_limited"):
        await bridge.service.send(bridge.identities["beta"], incoming())
    assert len(bridge.runner._active) == 2


async def test_concurrent_exact_retry_adopts_once(bridge):
    item = incoming()
    one, two = await asyncio.gather(
        *(bridge.service.send(bridge.identities["alpha"], item) for _ in range(2))
    )
    assert one["id"] == two["id"]
    await settle(bridge, one)
    assert bridge.invoker.run_streaming.await_count == 1


async def test_revocation_foreign_epoch_and_unknown(bridge):
    row = await bridge.service.send(bridge.identities["alpha"], incoming())
    await settle(bridge, row)
    with pytest.raises(ConversationRefusal, match="not_found"):
        await bridge.service.get(bridge.identities["beta"], row["id"])
    with pytest.raises(ConversationRefusal, match="not_found"):
        await bridge.service.get(bridge.identities["alpha"], str(uuid4()))
    await bridge.registry.revoke("alpha")
    with pytest.raises(ConversationRefusal, match="unauthorized"):
        await bridge.service.get(bridge.identities["alpha"], row["id"])


async def test_cancel_terminal_and_no_daily_quota(bridge):
    for _ in range(3):
        row = await bridge.service.send(bridge.identities["alpha"], incoming())
        row = await settle(bridge, row)
    from a2a.utils.errors import TaskNotCancelableError

    with pytest.raises(TaskNotCancelableError):
        await bridge.service.cancel(bridge.identities["alpha"], row["id"])
    assert bridge.invoker.run_streaming.await_count == 3  # Legacy allowance defaults1.


async def test_adoption_failure_retains_same_terminal_retry(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge.runner, "adopt", AsyncMock(side_effect=RuntimeError("private synthetic error"))
    )
    item = incoming()
    row = await bridge.service.send(bridge.identities["alpha"], item)
    assert project(row)["status"]["state"] == "TASK_STATE_FAILED"
    assert (await bridge.service.send(bridge.identities["alpha"], item))["id"] == row["id"]
    assert bridge.runner.adopt.await_count == 1


async def test_revoked_during_identity_assembly_never_admits(bridge, monkeypatch):
    async def changed(**kwargs):
        await bridge.registry.revoke("alpha")
        return "Synthetic identity"

    monkeypatch.setattr(
        "genesis.peers.conversation.SystemPromptAssembler",
        lambda: SimpleNamespace(assemble=changed),
    )
    with pytest.raises(ConversationRefusal, match="unauthorized"):
        await bridge.service.send(bridge.identities["alpha"], incoming())
    assert (
        await (
            await bridge.db.execute("SELECT count(*) FROM cc_sessions WHERE source_tag='agent_api'")
        ).fetchone()
    )[0] == 0
    bridge.invoker.run_streaming.assert_not_awaited()


async def test_waiter_cancellation_cannot_abandon_committed_receipt(bridge, monkeypatch):
    reached, release = asyncio.Event(), asyncio.Event()
    original = bridge.runner.adopt

    async def delayed(sid, request):
        reached.set()
        await release.wait()
        return await original(sid, request)

    monkeypatch.setattr(bridge.runner, "adopt", delayed)
    task = asyncio.create_task(bridge.service.send(bridge.identities["alpha"], incoming()))
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(bridge.service._admissions) == 1
    release.set()
    await asyncio.gather(*tuple(bridge.service._admissions))
    rows, _, _ = await bridge.service.list(bridge.identities["alpha"], 20)
    await settle(bridge, rows[0])
    assert bridge.invoker.run_streaming.await_count == 1


async def test_cancel_committed_receipt_before_registration_prevents_effects(bridge, monkeypatch):
    reached, release = asyncio.Event(), asyncio.Event()
    original = bridge.runner.adopt

    async def delayed(sid, request):
        reached.set()
        await release.wait()
        return await original(sid, request)

    monkeypatch.setattr(bridge.runner, "adopt", delayed)
    sender = asyncio.create_task(bridge.service.send(bridge.identities["alpha"], incoming()))
    await reached.wait()
    rows, _, _ = await bridge.service.list(bridge.identities["alpha"], 20)
    sid = rows[0]["id"]
    assert not bridge.runner.is_active(sid)
    canceled = asyncio.create_task(bridge.service.cancel(bridge.identities["alpha"], sid))
    await bridge.service._adopting[sid][1].wait()
    release.set()
    row = await canceled
    await sender
    assert json.loads(row["metadata"])["native_state"] == "cancelled"
    assert not bridge.runner.is_active(sid)
    assert not bridge.service._adopting
    bridge.invoker.run_streaming.assert_not_awaited()


async def test_insert_rollback_has_no_receipt_or_effect(bridge, monkeypatch):
    original = __import__("genesis.db.crud.cc_sessions", fromlist=["create"]).create

    async def failed(db, **kw):
        await original(db, **kw)
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr("genesis.db.crud.cc_sessions.create", failed)
    with pytest.raises(RuntimeError):
        await bridge.service.send(bridge.identities["alpha"], incoming())
    assert (
        await (
            await bridge.db.execute("SELECT count(*) FROM cc_sessions WHERE source_tag='agent_api'")
        ).fetchone()
    )[0] == 0
    bridge.invoker.run_streaming.assert_not_awaited()


@pytest.mark.parametrize("reason", ["other_owner", "ask", "lower_ceiling", "unknown_context"])
async def test_authority_and_context_fail_closed(bridge, reason):
    payload = message(profile="interact")
    if reason == "other_owner":
        await bridge.db.execute("UPDATE peers SET same_owner=0 WHERE peer_id='alpha'")
        await bridge.db.commit()
    elif reason == "ask":
        await bridge.registry.grant("alpha", "conversation", "ask")
    elif reason == "lower_ceiling":
        bridge.runtime._autonomy_manager.effective_level.return_value = 2
    else:
        payload["message"]["contextId"] = str(uuid4())
    with pytest.raises(ConversationRefusal):
        await bridge.service.send(bridge.identities["alpha"], incoming(payload))
    bridge.invoker.run_streaming.assert_not_awaited()


async def test_last_eight_turns_have_visible_omission(bridge):
    context = None
    for _ in range(10):
        row = await settle(
            bridge,
            await bridge.service.send(
                bridge.identities["alpha"], incoming(message(context=context))
            ),
        )
        context = json.loads(row["metadata"])["context_id"]
    prompt = bridge.invoker.run_streaming.call_args.args[0].prompt
    assert "[Earlier turns omitted]" in prompt
    assert prompt.count("Peer: ") == 8
    assert (
        len(prompt.encode())
        < 64 * 1024 + len("Latest peer message: Please reason independently") + 10
    )


async def test_sdk_cancel_waits_for_cleanup_and_reuses_cancelled(http_bridge):
    from a2a.types import CancelTaskRequest, TaskState

    b, _, base, _ = http_bridge
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    b.invoker.run_streaming.side_effect = blocked
    async with sdk_client(base) as (_, transport):
        task = await sdk_send(transport, message())
        await entered.wait()
        canceled = await transport.cancel_task(CancelTaskRequest(id=task.id))
        assert canceled.status.state == TaskState.TASK_STATE_CANCELED
        assert not b.runner.is_active(task.id)
        assert (await transport.cancel_task(CancelTaskRequest(id=task.id))).id == task.id


async def test_read_database_mismatch_refuses_disclosure(bridge, tmp_path):
    original = bridge.runtime._db
    other = await aiosqlite.connect(tmp_path / "other.db")
    bridge.runtime._db = other
    try:
        with pytest.raises(ConversationRefusal, match="not_ready"):
            await bridge.service.get(bridge.identities["alpha"], str(uuid4()))
    finally:
        bridge.runtime._db = original
        await other.close()


@pytest.mark.parametrize("enabled", [True, False])
async def test_host_bootstrap_records_peer_binding_readiness(bridge, monkeypatch, enabled):
    from genesis.hosting.standalone import StandaloneAdapter
    from genesis.runtime import GenesisRuntime
    from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

    bridge.runtime._bootstrap_manifest = {}
    bridge.runtime.bootstrap = AsyncMock()
    bridge.runtime.is_bootstrapped = True
    if not enabled:
        await bridge.registry.configure("disabled")
    monkeypatch.setattr(GenesisRuntime, "instance", classmethod(lambda cls: bridge.runtime))
    monkeypatch.setattr("genesis.peers.registry.PeerRegistry", lambda: bridge.registry)
    write = __import__("unittest.mock", fromlist=["Mock"]).Mock()
    monkeypatch.setattr("genesis.runtime._capabilities.write_capabilities_file", write)
    app = Flask(__name__)
    adapter = StandaloneAdapter()
    app.config.update(GENESIS_PEER_RUNTIME=bridge.runtime, GENESIS_PEER_REGISTRY=bridge.registry)
    monkeypatch.setattr(adapter, "_create_flask_app", lambda: app)
    monkeypatch.setattr(
        adapter, "_register_blueprints", lambda: app.register_blueprint(agent_api_bp)
    )
    await adapter.bootstrap()
    assert bridge.runtime._bootstrap_manifest["agent_api"] == ("ok" if enabled else "disabled")
    assert "agent_api" in _CAPABILITY_DESCRIPTIONS
    write.assert_called_once_with(bridge.runtime)


async def test_a_rewritten_result_file_is_not_served(bridge):
    row = await settle(bridge, await bridge.service.send(bridge.identities["alpha"], incoming()))
    assert full_result(row) == "A considered reply"
    path = bridge.home / ".genesis/output" / f"bg-session-{row['id']}.md"
    path.write_text("A later session's replacement")  # same path, same file type
    with pytest.raises(ConversationRefusal, match="result_unavailable"):
        full_result(row)
    meta = json.loads(row["metadata"])
    del meta["result_sha256"]  # a receipt without a committed digest
    row["metadata"] = json.dumps(meta)
    path.write_text("A considered reply")
    with pytest.raises(ConversationRefusal, match="result_unavailable"):
        full_result(row)
    meta["result_sha256"] = "é" * 64  # corrupted metadata fails closed, not 500
    row["metadata"] = json.dumps(meta)
    with pytest.raises(ConversationRefusal, match="result_unavailable"):
        full_result(row)


@pytest.mark.parametrize("second_service", [False, True])
async def test_a_retry_in_admission_shares_its_claim(bridge, monkeypatch, second_service):
    entered, release = [], asyncio.Event()

    class SlowAssembler:
        async def assemble(self, **kwargs):
            entered.append(True)
            await release.wait()
            return "Synthetic identity"

    monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", SlowAssembler)
    identity, payload = bridge.identities["alpha"], message()
    other = (
        ConversationService(bridge.registry, bridge.runtime) if second_service else bridge.service
    )
    sends = [
        asyncio.create_task((other if index % 2 else bridge.service).send(identity, incoming(p)))
        for index, p in enumerate((*([payload] * 12), message()))
    ]
    for _ in range(100):
        if len(entered) >= 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert len(entered) == 2 and not any(send.done() for send in sends)
    release.set()
    rows = [await send for send in sends]
    assert len({row["id"] for row in rows[:12]}) == 1
    assert rows[0]["id"] != rows[-1]["id"]
    for row in (rows[0], rows[-1]):
        await settle(bridge, row)
    assert bridge.service._enriching == {}


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_failed_identity_work_releases_admission_claim(bridge, monkeypatch, failure):
    class BrokenAssembler:
        async def assemble(self, **kwargs):
            raise failure("Synthetic failure")

    monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", BrokenAssembler)
    with pytest.raises(failure):
        await bridge.service.send(bridge.identities["alpha"], incoming())
    assert bridge.runner._peer_admissions == {}
    assert bridge.runner._active == {}
    bridge.invoker.run_streaming.assert_not_awaited()


@pytest.mark.parametrize("peer", ["alpha", "beta"])
async def test_a_send_burst_does_identity_work_only_within_capacity(bridge, monkeypatch, peer):
    entered, release = [], asyncio.Event()
    both_entered = asyncio.Event()

    class SlowAssembler:
        async def assemble(self, **kwargs):
            entered.append(True)
            if len(entered) == 2:
                both_entered.set()
            await release.wait()
            return "Synthetic identity"

    monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", SlowAssembler)
    identities = [bridge.identities["alpha"], bridge.identities[peer], bridge.identities["alpha"]]
    sends = [asyncio.create_task(bridge.service.send(i, incoming())) for i in identities]
    # Preflights interleave, so which send is refused is not fixed.
    done, _ = await asyncio.wait(sends, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    (refused,) = done
    with pytest.raises(ConversationRefusal, match="rate_limited"):
        await refused
    # A refused send may finish while another admitted send is still awaiting
    # its preflight or autonomy lookup. Wait for both actual assembly entries.
    await asyncio.wait_for(both_entered.wait(), 5)
    assert len(entered) == 2
    release.set()
    for send in sends:
        if send is not refused:
            task = bridge.runner._active.get((await asyncio.wait_for(send, 10))["id"])
            if task:
                await asyncio.wait_for(asyncio.shield(task), 3)
    assert bridge.service._enriching == {}


async def test_missing_partial_symlink_and_whole_result_scan(bridge):
    row = await settle(bridge, await bridge.service.send(bridge.identities["alpha"], incoming()))
    path = bridge.home / ".genesis/output" / f"bg-session-{row['id']}.md"
    path.write_text("benign prefix\n" + json.dumps({"password": "syntheticcredential"}))
    meta = json.loads(row["metadata"])
    meta["result_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()  # as committed
    row["metadata"] = json.dumps(meta)
    with pytest.raises(ConversationRefusal, match="disclosure_refused"):
        full_result(row)
    meta["output_text"] = path.read_text()
    row["metadata"] = json.dumps(meta)
    assert "message" not in project(row)["status"]
    other = bridge.home / "external-result"
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(ConversationRefusal, match="result_unavailable"):
        full_result(row)
    path.unlink()
    with pytest.raises(ConversationRefusal, match="result_unavailable"):
        full_result(row)


@pytest.mark.parametrize(
    "option",
    [
        {"profile": "foreground"},
        {"tool_exceptions": []},
        {"system_prompt": "override"},
        {"timeout_s": True},
        {"timeout_s": 7201},
        {"effort": "max"},
    ],
)
def test_privileged_or_invalid_options_rejected(option):
    from a2a.utils.errors import InvalidParamsError

    with pytest.raises(InvalidParamsError):
        incoming(message(**option))


def test_async_version_alias_and_head_boundaries():
    from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError

    body = message()
    body["configuration"]["returnImmediately"] = False
    with pytest.raises(UnsupportedOperationError):
        incoming(body)
    with pytest.raises(InvalidParamsError):
        parse_send(b'{"message":{},"message":{}}')
    assert len(head("\u20ac" * 6000).encode()) <= 4096
    assert "omitted" in head("\u20ac" * 6000)


@pytest.mark.parametrize(
    ("header", "supported"),
    [
        ("1.0", True),
        ("1", True),
        ("1.0.3", True),  # patch versions do not affect compatibility
        ("1.1", False),
        ("2.0", False),
        ("0.3", False),
        (None, False),  # a missing header means 0.3
        ("1.0rc1", False),
        ("1.0+local", False),
        ("1!1.0", False),
        ("not a version", False),
    ],
)
def test_only_a2a_1_0_is_served(header, supported):
    from a2a.utils.errors import VersionNotSupportedError

    from genesis.peers.protocol import validate_version

    if supported:
        validate_version(header)
    else:
        with pytest.raises(VersionNotSupportedError):
            validate_version(header)


async def test_read_pagination_owned_contexts(bridge):
    for _ in range(3):
        await settle(bridge, await bridge.service.send(bridge.identities["alpha"], incoming()))
    rows, token, total = await bridge.service.list(bridge.identities["alpha"], 2)
    tail, last, total2 = await bridge.service.list(bridge.identities["alpha"], 2, token)
    assert len(rows) == 2 and len(tail) == 1 and not last and total == total2 == 3
    assert set(r["id"] for r in rows).isdisjoint(r["id"] for r in tail)


async def test_bearer_fail_closed_body_and_owner_resolution(bridge, monkeypatch):
    import os

    from genesis.dashboard.auth import check_api_mutation_auth

    app = Flask(__name__)
    app.config.update(
        GENESIS_EVENT_LOOP=asyncio.get_running_loop(),
        GENESIS_PEER_RUNTIME=bridge.runtime,
        GENESIS_PEER_REGISTRY=bridge.registry,
    )
    app.before_request(check_api_mutation_auth)
    app.register_blueprint(agent_api_bp)

    @app.post("/api/genesis/approvals/<request_id>/resolve")
    def forbidden(request_id):
        pytest.fail("Peer reached owner resolver")

    headers = {
        "Authorization": "Bearer " + os.environ["GENESIS_PEER_ALPHA_TOKEN"],
        "A2A-Version": "1.0",
    }

    async def call(method, path, **kw):
        return await asyncio.to_thread(lambda: app.test_client().open(path, method=method, **kw))

    assert (await call("GET", ROOT + "/health", headers=headers)).status_code == 200
    assert (
        await call("GET", ROOT + "/health", headers={"Authorization": "Bearer invalid"})
    ).status_code == 401
    assert (
        await call("POST", ROOT + "/message:send", headers=headers, data=b"x" * (256 * 1024 + 1))
    ).status_code == 413
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    monkeypatch.setenv("GENESIS_DASHBOARD_API_AUTH", "off")
    assert (
        await call("POST", "/api/genesis/approvals/synthetic/resolve", headers=headers)
    ).status_code == 403
    monkeypatch.setenv("GENESIS_PEER_ALPHA_TOKEN", "")
    monkeypatch.setenv("GENESIS_PEER_BETA_TOKEN", "")
    for path in (
        "/health",
        "/tasks",
        "/approvals",
        "/message:send",
        "/tasks/synthetic:cancel",
        "/unknown",
    ):
        assert (
            await call(
                "POST" if path.endswith(":send") or path.endswith(":cancel") else "GET",
                ROOT + path,
                headers=headers,
            )
        ).status_code == 503


async def test_approvals_page_and_task_projection_is_a_bounded_check(bridge):
    from genesis.db.crud import approval_requests

    identity = bridge.identities["alpha"]
    row = await settle(bridge, await bridge.service.send(identity, incoming()))
    for n in range(5):
        await approval_requests.create(
            bridge.db,
            id=f"synthetic-approval-{n}",
            action_type="email_send",
            action_class="irreversible",
            description=f"Synthetic held action {n}",
            context=json.dumps({"agent_session_id": row["id"]}),
            created_at=f"2026-01-01T00:00:0{n}",
        )
    first, token = await bridge.service.approvals(identity, 2)
    second, token2 = await bridge.service.approvals(identity, 2, token)
    third, token3 = await bridge.service.approvals(identity, 2, token2)
    ids = [a["approvalId"] for a in first + second + third]
    assert ids == [f"synthetic-approval-{n}" for n in range(5)]
    assert token and token2 and token3 == ""
    assert await bridge.service.has_pending_approval(identity, row["id"]) is True
    other = bridge.identities["beta"]
    assert await bridge.service.has_pending_approval(other, row["id"]) is False
    with pytest.raises(ConversationRefusal):
        await bridge.service.approvals(other, 2, token)  # foreign anchor
    # The owner resolving the anchor between pages does not invalidate the cursor.
    assert await approval_requests.resolve(
        bridge.db, token, status="approved", resolved_at="2026-01-01T00:01:00"
    )
    after, _ = await bridge.service.approvals(identity, 2, token)
    assert [a["approvalId"] for a in after] == [a["approvalId"] for a in second]


@pytest.mark.parametrize("context", ["not-a-uuid", "unowned"])
async def test_bad_continuation_anchor_is_refused_before_identity_work(
    bridge, monkeypatch, context
):
    assembled = AsyncMock()
    monkeypatch.setattr(
        "genesis.peers.conversation.SystemPromptAssembler",
        lambda: SimpleNamespace(assemble=assembled),
    )
    payload = message(context=str(uuid4()) if context == "unowned" else context)
    with pytest.raises(ConversationRefusal) as refused:
        await bridge.service.send(bridge.identities["alpha"], incoming(payload))
    assert refused.value.status == (404 if context == "unowned" else 400)
    assembled.assert_not_awaited()
    bridge.invoker.run_streaming.assert_not_awaited()


async def test_shutdown_drains_admissions_and_refuses_new_ones(bridge, monkeypatch):
    identity = bridge.identities["alpha"]
    entered, release = asyncio.Event(), asyncio.Event()
    real = SystemPromptAssembler

    class SlowAssembler:
        async def assemble(self, **kwargs):
            entered.set()
            await release.wait()
            return "Synthetic identity"

    monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", SlowAssembler)
    send = asyncio.create_task(bridge.service.send(identity, incoming()))
    await asyncio.wait_for(entered.wait(), 3)
    stop = asyncio.create_task(bridge.runner.shutdown(grace_s=0.2))
    await asyncio.sleep(0.05)
    with pytest.raises(ConversationRefusal) as late:
        await bridge.service.send(identity, incoming())
    assert late.value.status == 503
    await asyncio.wait_for(stop, 3)
    assert send.done()
    # The cut-off admission never adopted: no native effect ran.
    bridge.invoker.run_streaming.assert_not_awaited()
    assert not bridge.runner._active
    assert real is SystemPromptAssembler


async def test_not_started_persistence_stops_retrying_once_closing(bridge, monkeypatch):
    from genesis.peers import conversation

    identity = bridge.identities["alpha"]

    async def refuse(*args, **kwargs):
        raise ValueError("Prepared receipt cannot be adopted")

    attempts = []

    async def failing_update(*args, **kwargs):
        attempts.append(1)
        bridge.runner._closing = True
        raise RuntimeError("synthetic closed database")

    monkeypatch.setattr(bridge.runner, "adopt", refuse)
    monkeypatch.setattr(conversation, "update_state", failing_update)
    await asyncio.wait_for(bridge.service.send(identity, incoming()), 3)
    assert attempts == [1]


async def test_not_started_retry_logs_error_type_without_private_values(bridge, monkeypatch, caplog):
    import sqlite3

    from genesis.peers import conversation

    error = sqlite3.OperationalError("synthetic private SQL and values")
    error.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"
    original = conversation.update_state
    attempts = 0

    async def flaky_update(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise error
        await original(*args, **kwargs)

    monkeypatch.setattr(bridge.runner, "adopt", AsyncMock(side_effect=ValueError("not adopted")))
    monkeypatch.setattr(conversation, "update_state", flaky_update)
    row = await asyncio.wait_for(bridge.service.send(bridge.identities["alpha"], incoming()), 3)
    assert attempts == 3
    assert row["status"] == "failed"
    # Two failures ~0.1s apart emit one warning; retries stay unlogged inside
    # the throttle window.
    assert caplog.text.count("exception=OperationalError, sqlite_errorname=SQLITE_BUSY_SNAPSHOT") == 1
    assert str(error) not in caplog.text


def _shrink_route_timeout(monkeypatch, app, endpoint, seconds):
    """Patch one route's bound ``_async_route`` timeout instead of waiting 15s."""
    view = app.view_functions[endpoint]
    cell = view.__closure__[view.__code__.co_freevars.index("timeout")]
    monkeypatch.setattr(cell, "cell_contents", seconds)


def _peer_client():
    import os

    import httpx

    return httpx.AsyncClient(
        headers={
            "Authorization": "Bearer " + os.environ["GENESIS_PEER_ALPHA_TOKEN"],
            "A2A-Version": "1.0",
            "Content-Type": "application/json",
        },
        trust_env=False,
        timeout=10,
    )


async def _receipt_count(bridge):
    return (
        await (
            await bridge.db.execute("SELECT count(*) FROM cc_sessions WHERE source_tag='agent_api'")
        ).fetchone()
    )[0]


async def test_send_timeout_503_names_accepted_task_and_same_id_retry_adopts_it(
    http_bridge, monkeypatch
):
    from genesis.peers.conversation import receipt_id

    b, app, base, _ = http_bridge
    app.config["GENESIS_CONVERSATION_SERVICE"] = b.service
    _shrink_route_timeout(monkeypatch, app, "agent_api.send_message", 0.3)
    reached, release = asyncio.Event(), asyncio.Event()
    original = b.runner.adopt

    async def delayed(sid, request):
        reached.set()
        await release.wait()
        return await original(sid, request)

    monkeypatch.setattr(b.runner, "adopt", delayed)
    payload = message()
    sid = receipt_id(b.identities["alpha"], incoming(payload))
    try:
        await _timed_out_send_then_retry(b, base, payload, sid, reached, release)
    finally:
        # A failed assertion must not strand the admission on the held adopt.
        release.set()
        await asyncio.gather(*tuple(b.service._admissions), return_exceptions=True)


async def _timed_out_send_then_retry(b, base, payload, sid, reached, release):
    async with _peer_client() as client:
        first = await client.post(base + ROOT + "/message:send", content=json.dumps(payload))
        # The owner decision keeps 503; the body now names the accepted task.
        assert first.status_code == 503
        body = first.json()
        assert body["code"] == "not_ready" and body["error"]["status"] == "UNAVAILABLE"
        assert body["taskId"] == sid
        assert reached.is_set()
        # The admission outlived the route: a same-messageId retry adopts it.
        retry = await client.post(base + ROOT + "/message:send", content=json.dumps(payload))
        assert retry.status_code == 200 and retry.json()["task"]["id"] == sid
        release.set()
        await asyncio.gather(*tuple(b.service._admissions))
        await settle(b, {"id": sid})
        assert await _receipt_count(b) == 1
        assert b.invoker.run_streaming.await_count == 1
        # A send that finishes inside the bound is still a plain 200 task.
        fast = await client.post(base + ROOT + "/message:send", content=json.dumps(message()))
        assert fast.status_code == 200 and "taskId" not in fast.json()
        assert fast.json()["task"]["id"] != sid


async def test_non_send_timeout_503_body_is_unchanged(http_bridge, monkeypatch):
    b, app, base, _ = http_bridge
    row = await b.service.send(b.identities["alpha"], incoming())
    await settle(b, row)
    app.config["GENESIS_CONVERSATION_SERVICE"] = b.service
    _shrink_route_timeout(monkeypatch, app, "agent_api.get_task", 0.3)
    release = asyncio.Event()

    async def slow(*args, **kwargs):
        await release.wait()
        return False

    # get_task sets g.peer_task_id before this await, then times out.
    monkeypatch.setattr(b.service, "has_pending_approval", slow)
    try:
        async with _peer_client() as client:
            response = await client.get(base + ROOT + f"/tasks/{row['id']}")
    finally:
        release.set()
    assert response.status_code == 503
    assert response.json() == {
        "code": "not_ready",
        "error": {"code": 503, "message": "Peer request refused", "status": "UNAVAILABLE"},
    }


def _hold_admission(b, level_after):
    """Hold the pre-commit authority read in ``_admit`` until released."""
    entered, release = asyncio.Event(), asyncio.Event()
    entered.held_calls = 0

    async def held(*args, **kwargs):
        # Calls made while held are admission work; the native run's own
        # authority read happens after release and is not counted.
        if not release.is_set():
            entered.held_calls += 1
        entered.set()
        await release.wait()
        return level_after

    b.runtime._autonomy_manager.effective_level = AsyncMock(side_effect=held)
    return entered, release


async def _post(client, base, payload):
    return await client.post(base + ROOT + "/message:send", content=json.dumps(payload))


async def test_pre_commit_timeout_retry_shares_the_original_admission(http_bridge, monkeypatch):
    from genesis.peers.conversation import receipt_id

    b, app, base, _ = http_bridge
    app.config["GENESIS_CONVERSATION_SERVICE"] = b.service
    _shrink_route_timeout(monkeypatch, app, "agent_api.send_message", 0.3)
    entered, release = _hold_admission(b, 3)
    payload = message()
    sid = receipt_id(b.identities["alpha"], incoming(payload))
    try:
        async with _peer_client() as client:
            first = await _post(client, base, payload)
            assert first.status_code == 503 and first.json()["taskId"] == sid
            assert entered.is_set() and await _receipt_count(b) == 0
            # Still pre-commit: the retry waits on the same shielded work and
            # can itself time out, naming the same receipt.
            retry = await _post(client, base, payload)
            assert retry.status_code == 503 and retry.json()["taskId"] == sid
            release.set()
            await asyncio.gather(*tuple(b.service._admissions))
            done = await _post(client, base, payload)
            assert done.status_code == 200 and done.json()["task"]["id"] == sid
    finally:
        release.set()
        await asyncio.gather(*tuple(b.service._admissions), return_exceptions=True)
    await settle(b, {"id": sid})
    assert await _receipt_count(b) == 1
    assert entered.held_calls == 1  # the retry did not start a second admission
    assert b.invoker.run_streaming.await_count == 1


async def test_timeout_then_refusal_names_receipt_that_never_commits(http_bridge, monkeypatch):
    from genesis.peers.conversation import receipt_id

    b, app, base, _ = http_bridge
    app.config["GENESIS_CONVERSATION_SERVICE"] = b.service
    _shrink_route_timeout(monkeypatch, app, "agent_api.send_message", 0.3)
    # Authority drops below the floor while the admission is held: refused 403.
    entered, release = _hold_admission(b, 0)
    payload = message()
    sid = receipt_id(b.identities["alpha"], incoming(payload))
    try:
        async with _peer_client() as client:
            first = await _post(client, base, payload)
            assert first.status_code == 503 and first.json()["taskId"] == sid
            release.set()
            await asyncio.gather(*tuple(b.service._admissions), return_exceptions=True)
            assert (await client.get(base + ROOT + f"/tasks/{sid}")).status_code == 404
            retry = await _post(client, base, payload)
            assert retry.status_code == 403 and retry.json()["code"] == "unauthorized"
            assert "taskId" not in retry.json()
    finally:
        release.set()
        await asyncio.gather(*tuple(b.service._admissions), return_exceptions=True)
    assert await _receipt_count(b) == 0
    b.invoker.run_streaming.assert_not_awaited()


async def test_failure_after_commit_names_receipt_on_send_only(http_bridge, monkeypatch):
    from genesis.peers.conversation import receipt_id

    b, app, base, _ = http_bridge
    app.config["GENESIS_CONVERSATION_SERVICE"] = b.service

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic projection failure")

    monkeypatch.setattr(b.service, "has_pending_approval", broken)
    payload = message()
    sid = receipt_id(b.identities["alpha"], incoming(payload))
    unchanged = {
        "code": "not_ready",
        "error": {"code": 503, "message": "Peer request refused", "status": "UNAVAILABLE"},
    }
    async with _peer_client() as client:
        # send() committed the receipt; only the projection afterwards failed.
        sent = await _post(client, base, payload)
        assert sent.status_code == 503 and sent.json() == {**unchanged, "taskId": sid}
        await settle(b, {"id": sid})
        assert await _receipt_count(b) == 1
        # The same generic failure on a read route carries no task id.
        read = await client.get(base + ROOT + f"/tasks/{sid}")
        assert read.status_code == 503 and read.json() == unchanged
