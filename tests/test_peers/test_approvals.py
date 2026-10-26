"""Individual consent and durable notification, using the existing approval store."""

import asyncio
import gc
import hashlib
import importlib
import json
import secrets
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from flask import Flask

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
from genesis.cc.peer_segment import PeerSegment
from genesis.db.schema import TABLES
from genesis.outreach.types import OutreachStatus
from genesis.peers.approvals import PeerApprovals
from genesis.peers.session import PeerSessionBinding
from genesis.peers.tasks import PeerTasks, TaskRefusal


@pytest.fixture
async def setup(registry, tmp_path):
    async with registry.connection() as db:
        for name in (
            "direct_session_queue",
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
            "approval_requests",
            "peer_operation_approvals",
        ):
            await db.execute(TABLES[name])
        await db.commit()
    await registry.register(
        "fixture", same_owner=True, daily_allowance=10, token_name="GENESIS_PEER_FIXTURE_TOKEN"
    )
    await registry.grant("fixture", "conversation", "ask")
    identity = await registry.get("fixture")
    task = await PeerTasks(registry).admit(
        identity, {"messageId": "one", "role": "ROLE_USER", "parts": [{"text": "Fixture."}]}
    )
    async with registry.transaction() as db:
        await db.execute("UPDATE peer_tasks SET state='working' WHERE id=?", (task["id"],))
    binding = PeerSessionBinding(
        task["id"],
        PeerSegment(uuid.uuid4().hex, time.time() + 60, ("mcp__genesis_peer__task_context",)),
        0,
        str(tmp_path / "facade.json"),
        str(tmp_path),
    )
    notifications = []

    async def submit(message, request, reply_markup):
        notifications.append((message, reply_markup))
        return SimpleNamespace(status=OutreachStatus.DELIVERED, delivery_id="fixture_delivery")

    runtime = SimpleNamespace(_outreach_pipeline=SimpleNamespace(submit_raw=submit))
    async with registry.connection() as db:
        manager = ApprovalManager(db=db)
        gate = AutonomousCliApprovalGate(runtime=runtime, approval_manager=manager)
        gate._policy = lambda: SimpleNamespace(reask_hours_for=lambda _: 0)
        service = PeerApprovals(registry, manager, gate)
        yield SimpleNamespace(
            registry=registry,
            identity=identity,
            binding=binding,
            manager=manager,
            gate=gate,
            service=service,
            runtime=runtime,
            notifications=notifications,
            digest=hashlib.sha256(b"Fixture exact operation.").hexdigest(),
        )


async def request(s):
    return await s.service.request(
        s.binding, "conversation", s.digest, "Read this fixture exchange."
    )


async def test_exact_request_retry_preserves_id_and_single_notification(setup):
    s = setup
    first = await request(s)
    assert await request(s) == first
    assert len(s.notifications) == 1
    message, markup = s.notifications[0]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        "cli_approve:" + first,
        "cli_reject:" + first,
    ]
    assert "Peer Operation Approval" in message and "fallback" not in message
    rows = await s.service.pending(s.identity)
    assert len(rows) == 1 and rows[0]["approval_id"] == first
    assert isinstance(rows[0]["timeout"], int) and 0 < rows[0]["timeout"] <= 86400
    assert set(rows[0]) == {
        "approval_id",
        "action_type",
        "description",
        "created_at",
        "timeout",
        "notification_error",
    }


async def test_notification_registry_collects_completed_and_missing_ids(setup):
    s = setup
    identifier = await request(s)
    assert not await s.service.deliver(identifier)
    for _ in range(100):
        assert not await s.service.deliver(str(uuid.uuid4()))
    gc.collect()
    assert not s.service._notification_locks
    assert (await s.manager.get_by_id(identifier))["status"] == "pending"
    assert await request(s) == identifier
    assert len(s.notifications) == 1


async def test_notification_lock_survives_waiter_cancel_and_new_arrival(setup, monkeypatch):
    s = setup
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = calls = 0

    async def controlled(identifier):
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        calls += 1
        entered.set()
        try:
            await release.wait()
            await asyncio.sleep(0)
            return True
        finally:
            active -= 1

    monkeypatch.setattr(s.service, "_deliver", controlled)
    holder = asyncio.create_task(s.service.deliver("shared"))
    await entered.wait()
    cancelled = asyncio.create_task(s.service.deliver("shared"))
    survivor = asyncio.create_task(s.service.deliver("shared"))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    late = asyncio.create_task(s.service.deliver("shared"))
    await asyncio.sleep(0)
    assert calls == maximum == 1
    release.set()
    assert await asyncio.gather(holder, survivor, late) == [True, True, True]
    assert calls == 3 and maximum == 1
    del holder, survivor, late, cancelled
    gc.collect()
    assert not s.service._notification_locks


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_notification_lock_releases_after_holder_failure(setup, monkeypatch, failure):
    s = setup

    async def interrupted(identifier):
        raise failure("Fixture interruption")

    monkeypatch.setattr(s.service, "_deliver", interrupted)
    with pytest.raises(failure):
        await s.service.deliver("retry")
    gc.collect()
    assert not s.service._notification_locks
    monkeypatch.setattr(s.service, "_deliver", AsyncMock(return_value=True))
    assert await s.service.deliver("retry")


async def test_concurrent_request_retries_notify_once_with_collectible_locks(setup):
    s = setup
    identifiers = await asyncio.gather(*(request(s) for _ in range(8)))
    assert len(set(identifiers)) == 1
    assert len(s.notifications) == 1
    assert (await s.manager.get_by_id(identifiers[0]))["status"] == "pending"
    gc.collect()
    assert not s.service._notification_locks


@pytest.mark.parametrize(
    "actor",
    [
        "user",
        "voice:s2s",
        "telegram:bare_text:1",
        "telegram:reply",
        "telegram:batch:1",
        "dashboard:batch",
        "genesis:fixture",
        "system",
        "manual:fixture",
        "telegram:button:0",
    ],
)
async def test_peer_consent_rejects_every_nonindividual_resolver(setup, actor):
    s = setup
    identifier = await request(s)
    assert not await s.manager.resolve(identifier, status="approved", resolved_by=actor)
    assert not await s.gate.resolve_request(identifier, decision="approved", resolved_by=actor)
    assert (await s.manager.get_by_id(identifier))["status"] == "pending"


@pytest.mark.parametrize("actor", ["dashboard", "telegram:button:123"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_named_dashboard_telegram_resolution_is_single_use(setup, actor, decision):
    s = setup
    identifier = await request(s)
    assert await s.gate.resolve_request(identifier, decision=decision, resolved_by=actor)
    assert not await s.gate.resolve_request(identifier, decision=decision, resolved_by=actor)
    assert await s.service.pending(s.identity) == []


async def test_batch_sweep_and_trigger_cannot_approve_peer_operation(setup):
    s = setup
    identifier = await request(s)
    assert not await s.gate.resolve_request(
        identifier, decision="approved", resolved_by="telegram:batch:123"
    )
    assert await s.gate.approve_all_pending(resolved_by="dashboard:batch") == 0
    assert (await s.manager.get_by_id(identifier))["status"] == "pending"


async def test_notification_missing_or_failed_remains_visible_and_retryable(setup):
    s = setup
    pipeline = s.runtime._outreach_pipeline
    s.runtime._outreach_pipeline = None
    identifier = await request(s)
    assert (await s.service.pending(s.identity))[0][
        "notification_error"
    ] == "notification_unavailable"
    s.runtime._outreach_pipeline = pipeline
    assert await s.service.deliver(identifier)
    assert (await s.service.pending(s.identity))[0]["notification_error"] is None
    assert len(s.notifications) == 1


async def test_creation_intent_recovers_same_id_after_request_creation_failure(setup, monkeypatch):
    s = setup
    create = s.manager.request_approval

    async def interrupted(**kwargs):
        raise RuntimeError("Fixture unavailable")

    monkeypatch.setattr(s.manager, "request_approval", interrupted)
    with pytest.raises(RuntimeError):
        await request(s)
    async with s.registry.connection() as db:
        row = await (
            await db.execute("SELECT approval_id FROM peer_operation_approvals")
        ).fetchone()
    monkeypatch.setattr(s.manager, "request_approval", create)
    assert await request(s) == row[0]
    assert len(s.notifications) == 1


@pytest.mark.parametrize("change", ["cancel", "generation", "revoke", "grant", "budget"])
async def test_invalidation_blocks_notification_retry_and_owned_summary(setup, change):
    s = setup
    identifier = await request(s)
    if change == "revoke":
        await s.registry.revoke("fixture")
    elif change == "grant":
        await s.registry.grant("fixture", "conversation", "deny")
    else:
        assignment = {
            "cancel": "cancel_requested=1",
            "generation": "generation=generation+1",
            "budget": "work_elapsed_s=work_limit_s",
        }[change]
        async with s.registry.transaction() as db:
            await db.execute(f"UPDATE peer_tasks SET {assignment} WHERE id=?", (s.binding.task_id,))
    with pytest.raises(TaskRefusal):
        await s.service.deliver(identifier)
    assert await s.service.pending(s.identity) == []


@pytest.mark.parametrize(
    "name", ["GENESIS_PEER_FIXTURE_TOKEN", "GENESIS_PEER_BACKEND_TOKEN", "GENESIS_AGENT_TOKEN"]
)
@pytest.mark.parametrize("resolver", ["individual", "batch"])
async def test_agent_token_cannot_resolve_approval_even_with_owner_cookie(
    setup, monkeypatch, name, resolver
):
    from genesis.dashboard import auth
    from genesis.dashboard.routes import state
    from genesis.runtime import GenesisRuntime

    identifier = await request(setup)
    monkeypatch.setattr(auth, "has_internal_bearer", lambda: False)
    monkeypatch.setattr(
        GenesisRuntime,
        "instance",
        lambda: SimpleNamespace(is_bootstrapped=True, _autonomous_cli_approval_gate=setup.gate),
    )
    monkeypatch.setenv(name, secrets.token_urlsafe(32))
    from genesis.env import bearer_token

    header = {"Authorization": "Bearer " + bearer_token(name)}
    app = Flask(__name__)
    app.secret_key = secrets.token_urlsafe(32)
    with app.test_request_context("/", method="POST", headers=header):
        from flask import session

        session["authenticated"] = True
        function = (
            state.resolve_approval if resolver == "individual" else state.approve_all_approvals
        )
        args = (identifier,) if resolver == "individual" else ()
        response, status = await function.__wrapped__(*args)
        assert status == 401 and response.get_json()["code"] == "unauthorized"
    assert (await setup.manager.get_by_id(identifier))["status"] == "pending"


@pytest.mark.parametrize("decision", ["approved", "rejected"])
@pytest.mark.parametrize(
    "password,cookie,origin,credential,auth_off,expected",
    [
        (False, False, "same-origin", "absent", False, 401),
        (False, True, "same-origin", "empty", False, 401),
        (False, True, "same-origin", "absent", True, 401),
        (True, False, "same-origin", "absent", True, 401),
        (True, True, "cross-site", "absent", True, 403),
        (True, True, None, "absent", True, 403),
        (True, True, "same-origin", "absent", False, 200),
        (True, True, "same-origin", "empty", True, 200),
        (False, False, None, "internal", False, 200),
        (True, True, "cross-site", "internal", True, 200),
        (True, True, "same-origin", "peer", True, 401),
        (True, True, "same-origin", "invalid", True, 401),
        (True, True, "same-origin", "whitespace", True, 401),
        (True, True, "same-origin", "wrong_scheme", True, 401),
        (True, "true", "same-origin", "absent", True, 401),
        (True, True, "same-site", "absent", True, 403),
        (True, True, "none", "absent", True, 200),
        (True, True, "origin", "absent", True, 200),
        (True, True, "referer", "absent", True, 200),
    ],
)
async def test_peer_dashboard_requires_positive_owner_proof(
    setup, monkeypatch, tmp_path, decision, password, cookie, origin,
    credential, auth_off, expected,
):
    from genesis.dashboard import auth
    from genesis.dashboard.routes import state
    from genesis.runtime import GenesisRuntime

    identifier = await request(setup)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    if password:
        monkeypatch.setenv("DASHBOARD_PASSWORD", secrets.token_urlsafe(32))
    monkeypatch.setenv("GENESIS_DASHBOARD_API_AUTH", "off" if auth_off else "on")
    monkeypatch.setenv("GENESIS_DASHBOARD_NETWORK_SCOPE", "on")
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: tmp_path / "internal_api_token")
    monkeypatch.setattr(auth, "_internal_token_cache", None)
    monkeypatch.setattr(
        GenesisRuntime, "instance",
        lambda: SimpleNamespace(is_bootstrapped=True, _autonomous_cli_approval_gate=setup.gate),
    )
    app = Flask(__name__)
    app.secret_key = secrets.token_urlsafe(32)
    app.config["GENESIS_EVENT_LOOP"] = asyncio.get_running_loop()
    app.add_url_rule(
        "/api/genesis/approvals/<request_id>/resolve",
        view_func=state.resolve_approval, methods=["POST"],
    )
    auth.apply_api_mutation_gate(app)
    client = app.test_client()
    if cookie:
        with client.session_transaction() as owner_session:
            owner_session["authenticated"] = cookie
    headers = {} if origin is None else {"Sec-Fetch-Site": origin}
    if origin in {"origin", "referer"}:
        headers = {"Origin" if origin == "origin" else "Referer": "http://localhost/"}
    if credential == "empty":
        headers["Authorization"] = ""
    elif credential == "whitespace":
        headers["Authorization"] = " "
    elif credential != "absent":
        value = secrets.token_urlsafe(32)
        if credential == "internal":
            value = auth.get_or_create_internal_api_token()
        elif credential == "peer":
            monkeypatch.setenv("GENESIS_PEER_FIXTURE_TOKEN", value)
        headers["Authorization"] = ("Basic " if credential == "wrong_scheme" else "Bearer ") + value
    response = await asyncio.to_thread(
        client.post, f"/api/genesis/approvals/{identifier}/resolve",
        json={"decision": decision, "action_type": "fixture"}, headers=headers,
    )
    assert response.status_code == expected
    row = await setup.manager.get_by_id(identifier)
    assert row["status"] == (decision if expected == 200 else "pending")


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_passwordless_ordinary_resolution_and_peer_batch_exclusion(
    setup, monkeypatch, tmp_path, decision,
):
    from genesis.dashboard import auth
    from genesis.dashboard.routes import state
    from genesis.runtime import GenesisRuntime

    peer_id = await request(setup)
    ordinary_id = await setup.manager.request_approval(
        action_type="fixture", action_class="reversible", description="Ordinary fixture.",
    )
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("GENESIS_DASHBOARD_API_AUTH", "on")
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: tmp_path / "internal_api_token")
    monkeypatch.setattr(auth, "_internal_token_cache", None)
    monkeypatch.setattr(
        GenesisRuntime, "instance",
        lambda: SimpleNamespace(is_bootstrapped=True, _autonomous_cli_approval_gate=setup.gate),
    )
    app = Flask(__name__)
    app.secret_key = secrets.token_urlsafe(32)
    app.config["GENESIS_EVENT_LOOP"] = asyncio.get_running_loop()
    app.add_url_rule(
        "/api/genesis/approvals/<request_id>/resolve",
        view_func=state.resolve_approval, methods=["POST"],
    )
    app.add_url_rule(
        "/api/genesis/approvals/approve-all",
        view_func=state.approve_all_approvals, methods=["POST"],
    )
    auth.apply_api_mutation_gate(app)
    client = app.test_client()
    response = await asyncio.to_thread(
        client.post, f"/api/genesis/approvals/{ordinary_id}/resolve", json={"decision": decision},
    )
    assert response.status_code == 200
    assert (await setup.manager.get_by_id(ordinary_id))["status"] == decision
    missing = await asyncio.to_thread(
        client.post, f"/api/genesis/approvals/{uuid.uuid4()}/resolve", json={"decision": decision},
    )
    assert missing.status_code == 404
    batch = await asyncio.to_thread(client.post, "/api/genesis/approvals/approve-all")
    assert batch.status_code == 200 and batch.get_json()["approved"] == 0
    assert (await setup.manager.get_by_id(peer_id))["status"] == "pending"


@pytest.mark.parametrize("status", ["approved", "expired", "cancelled"])
@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_terminal_peer_rows_do_not_waive_owner_proof(
    setup, monkeypatch, status, owner, decision,
):
    from flask import session

    from genesis.dashboard.routes import state
    from genesis.runtime import GenesisRuntime

    identifier = await request(setup)
    assert await setup.manager.resolve(identifier, status=status, resolved_by="dashboard")
    monkeypatch.setenv("DASHBOARD_PASSWORD", secrets.token_urlsafe(32))
    monkeypatch.setattr(
        GenesisRuntime, "instance",
        lambda: SimpleNamespace(is_bootstrapped=True, _autonomous_cli_approval_gate=setup.gate),
    )
    app = Flask(__name__)
    app.secret_key = secrets.token_urlsafe(32)
    with app.test_request_context(
        "/", method="POST", json={"decision": decision},
        headers={"Sec-Fetch-Site": "same-origin"},
    ):
        session["authenticated"] = owner
        _, result_status = await state.resolve_approval.__wrapped__(identifier)
    assert result_status == (404 if owner else 401)
    assert (await setup.manager.get_by_id(identifier))["status"] == status


async def test_manager_fixed_uuid_and_default_uuid_and_invalid_input(setup):
    s = setup
    fields = dict(action_type="fixture", action_class="reversible", description="Fixture.")
    identifier = str(uuid.uuid4())
    assert await s.manager.request_approval(request_id=identifier, **fields) == identifier
    assert str(uuid.UUID(await s.manager.request_approval(**fields)))
    with pytest.raises(ValueError):
        await s.manager.request_approval(request_id="invalid", **fields)


@pytest.mark.parametrize("failure", ["no_recipient", "adapter"])
@pytest.mark.parametrize("invalidation", ["cancel", "grant"])
async def test_peer_notification_has_one_durable_retry_owner(setup, failure, invalidation):
    from genesis.content.formatter import ContentFormatter
    from genesis.outreach.config import _DEFAULTS
    from genesis.outreach.governance import GovernanceGate
    from genesis.outreach.pipeline import OutreachPipeline

    s = setup
    fail_send = failure == "adapter"
    callbacks = []

    async def send_message(recipient, text, *, message_thread_id, reply_markup):
        if fail_send:
            raise RuntimeError("Fixture delivery failed")
        callbacks.append([b.callback_data for row in reply_markup.inline_keyboard for b in row])
        return "fixture_delivery"

    deferred = SimpleNamespace(has_open=AsyncMock(return_value=False), enqueue=AsyncMock())
    recipients = {} if failure == "no_recipient" else {"telegram": "fixture_owner"}
    pipeline = OutreachPipeline(
        GovernanceGate(_DEFAULTS, s.manager._db),
        None,
        ContentFormatter(),
        {"telegram": SimpleNamespace(send_message=send_message)},
        config=_DEFAULTS,
        recipients=recipients,
        deferred_queue=deferred,
    )
    s.runtime._outreach_pipeline = pipeline
    identifier = await request(s)
    deferred.enqueue.assert_not_awaited()
    assert (await s.service.pending(s.identity))[0][
        "notification_error"
    ] == "notification_unavailable"
    fail_send = False
    pipeline._recipients["telegram"] = "fixture_owner"
    assert await s.service.deliver(identifier)
    assert callbacks == [["cli_approve:" + identifier, "cli_reject:" + identifier]]
    assert not await s.service.deliver(identifier)

    fail_send = True
    other = await s.service.request(
        s.binding, "conversation", hashlib.sha256(b"Other operation.").hexdigest(), "Other fixture."
    )
    if invalidation == "grant":
        await s.registry.grant("fixture", "conversation", "deny")
    else:
        async with s.registry.transaction() as db:
            await db.execute(
                "UPDATE peer_tasks SET cancel_requested=1 WHERE id=?", (s.binding.task_id,)
            )
    fail_send = False
    with pytest.raises(TaskRefusal):
        await s.service.deliver(other)
    assert len(callbacks) == 1
    deferred.enqueue.assert_not_awaited()


async def test_canonical_and_upgrade_approval_intent_ddl_identical():
    migration = importlib.import_module(
        "genesis.db.migrations.20261008082201_peer_operation_approvals"
    )
    statements = []

    class Capture:
        async def execute(self, sql):
            statements.append(sql.strip())

    await migration.up(Capture())
    assert statements == [TABLES["peer_operation_approvals"].strip()]


@pytest.mark.parametrize("receipt", [None, "", 0, False, [], {}, "fixture_delivery"])
async def test_committed_receipt_recovers_lost_notification_bookkeeping(setup, receipt):
    s = setup
    identifier = await request(s)
    persisted = json.loads((await s.manager.get_by_id(identifier))["context"])
    persisted["delivery_id"] = receipt
    await s.manager.update_context(identifier, context=json.dumps(persisted))
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_operation_approvals SET notified_at=NULL,"
            "notification_error='notification_unavailable' WHERE approval_id=?",
            (identifier,),
        )
    assert await s.service.deliver(identifier)
    assert len(s.notifications) == (1 if isinstance(receipt, str) and receipt else 2)
    assert (await s.service.pending(s.identity))[0]["notification_error"] is None
    assert (await s.manager.get_by_id(identifier))["status"] == "pending"
    assert not await s.service.deliver(identifier)
    async with s.registry.connection() as db:
        row = await (await db.execute(
            "SELECT notification_attempts,notified_at FROM peer_operation_approvals "
            "WHERE approval_id=?", (identifier,),
        )).fetchone()
    assert row[0] == 2 and row[1] is not None


async def test_committed_receipt_cannot_override_mismatched_intent(setup):
    s = setup
    identifier = await request(s)
    persisted = json.loads((await s.manager.get_by_id(identifier))["context"])
    persisted["extra"]["operation_digest"] = "different_operation"
    await s.manager.update_context(identifier, context=json.dumps(persisted))
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_operation_approvals SET notified_at=NULL WHERE approval_id=?",
            (identifier,),
        )
    with pytest.raises(TaskRefusal):
        await s.service.deliver(identifier)
    assert len(s.notifications) == 1
    assert (await s.manager.get_by_id(identifier))["status"] == "pending"
