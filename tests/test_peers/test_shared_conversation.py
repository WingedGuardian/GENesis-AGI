"""Restricted foreign reasoning never inherits private owner authority/history."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from genesis.peers.conversation import ConversationRefusal
from genesis.peers.outbox import Outbox, owned_context
from tests.test_peers.test_conversation import bridge as bridge
from tests.test_peers.test_conversation import incoming, message, settle


@pytest.fixture(autouse=True)
async def configure_readiness(bridge, monkeypatch):
    from dataclasses import replace

    from genesis.cc import roster
    from genesis.cc.provider_readiness import READY
    from genesis.cc.types import PROBE_CALLER_TAG, StreamEvent

    monkeypatch.setattr("genesis.cc.direct_session._BG_CC_TMP_ROOT", bridge.home / "sessions")
    monkeypatch.setenv("GENESIS_BIG_TMP", str(bridge.home / "probes"))
    monkeypatch.setattr(roster, "load_roster", lambda: {
        "default": "claude", "models": {"claude": {"native_subscription": True}},
    })
    answer = bridge.invoker.run_streaming.return_value
    async def execute(invocation, on_event=None):
        if invocation.caller_tag == PROBE_CALLER_TAG:
            await on_event(StreamEvent("init", raw={"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}))
            return replace(answer, text=READY, tools_used=())
        if invocation.on_spawn:
            await invocation.on_spawn(424215)
        return answer
    bridge.invoker.run_streaming.side_effect = execute


async def foreign(b, *, grant="allow"):
    async with b.registry.transaction() as db:
        await db.execute("UPDATE peers SET same_owner=0 WHERE peer_id='alpha'")
        if grant is not None:
            await db.execute("INSERT INTO peer_grants(peer_id,capability,decision) VALUES('alpha','conversation_shared',?)", (grant,))
    # Deliberately stale authenticated ownership; authorization reads the DB.
    return b.identities["alpha"]


async def test_owner_can_explicitly_grant_shared_capability(bridge):
    await bridge.registry.grant("alpha", "conversation_shared", "allow")
    assert (await bridge.registry.grants("alpha"))["conversation_shared"] == "allow"


async def test_shared_receipt_is_contained_atomic_and_retryable(bridge, monkeypatch):
    b = bridge
    identity = await foreign(b)
    def private(*args, **kwargs):
        raise AssertionError("Foreign request reached private owner builder")
    monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", private)
    b.runtime._context_injector = type("Injector", (), {"inject": AsyncMock(side_effect=private)})()
    monkeypatch.setattr(b.runner, "_build_invocation", private)
    payload = message(timeout_s=30)
    first = await b.service.send(identity, incoming(payload))
    done = await settle(b, first)
    meta = json.loads(done["metadata"])
    assert meta["conversation_trust"] == "conversation_shared"
    assert done["status"] == "completed" and meta["transcript_path"] == ""
    invocation = b.invoker.run_streaming.call_args.args[0]
    assert invocation.peer_segment.mode == "conversation" and invocation.disable_tools
    assert invocation.mcp_config is None and invocation.no_session_persistence
    notices = await (await b.db.execute("SELECT * FROM pending_outreach")).fetchall()
    assert len(notices) == 1 and notices[0]["id"] == meta["owner_notice_id"]
    assert payload["message"]["parts"][0]["text"] not in notices[0]["message"]
    assert (await b.service.send(identity, incoming(payload)))["id"] == done["id"]
    assert b.invoker.run_streaming.await_count == 2  # One readiness diagnostic and one actual task.
    assert (await (await b.db.execute("SELECT count(*) FROM pending_outreach")).fetchone())[0] == 1


@pytest.mark.parametrize("grant", [None, "ask", "deny"])
async def test_shared_requires_explicit_allow(bridge, grant):
    identity = await foreign(bridge, grant=grant)
    with pytest.raises(ConversationRefusal) as caught:
        await bridge.service.send(identity, incoming())
    assert caught.value.status == 403
    bridge.invoker.run_streaming.assert_not_awaited()


@pytest.mark.parametrize("reader", ["get", "list", "context", "retry", "outbox_context"])
async def test_foreign_receipt_never_upgrades_after_owner_reclassification(bridge, reader):
    b = bridge
    payload = message()
    first = await b.service.send(b.identities["alpha"], incoming(payload))
    done = await settle(b, first)
    meta = json.loads(done["metadata"])
    meta["conversation_trust"] = "conversation_shared"
    await b.db.execute("UPDATE cc_sessions SET metadata=? WHERE id=?", (json.dumps(meta), done["id"]))
    await b.db.commit()
    identity = b.identities["alpha"]
    if reader == "list":
        rows, _, total = await b.service.list(identity, 20)
        assert rows == [] and total == 0
    elif reader == "outbox_context":
        async with b.registry.connection() as db:
            assert not await owned_context(db, "alpha", identity.peer["epoch"], meta["context_id"])
    else:
        with pytest.raises(ConversationRefusal) as caught:
            if reader == "get":
                await b.service.get(identity, done["id"])
            elif reader == "context":
                await b.service.send(identity, incoming(message(context=meta["context_id"])))
            else:
                await b.service.send(identity, incoming(payload))
        assert caught.value.status == (409 if reader == "retry" else 404)


@pytest.mark.parametrize("operation", ["prepare", "poll", "ack"])
async def test_foreign_outbox_is_refused_even_with_shared_grant(bridge, operation):
    identity = await foreign(bridge)
    outbox = Outbox(bridge.registry)
    with pytest.raises(ConversationRefusal) as caught:
        if operation == "prepare":
            await outbox.prepare("alpha", "An owner-initiated message")
        elif operation == "poll":
            await outbox.poll(identity)
        else:
            await outbox.ack(identity, str(uuid4()))
    assert caught.value.status == 403


async def test_pre_adopt_commit_ack_loss_is_not_left_pending(bridge, monkeypatch):
    b = bridge
    transaction = b.registry.transaction
    injected = False
    @asynccontextmanager
    async def lost_ack(**kwargs):
        nonlocal injected
        async with transaction(**kwargs) as db:
            yield db
        if not injected:
            injected = True
            raise RuntimeError("Synthetic acknowledgement lost after durable commit")
    monkeypatch.setattr(b.registry, "transaction", lost_ack)
    with pytest.raises(RuntimeError):
        await b.service.send(b.identities["alpha"], incoming())
    rows = await (await b.db.execute("SELECT * FROM cc_sessions WHERE source_tag='agent_api'")).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    meta = json.loads(rows[0]["metadata"])
    assert meta["native_state"] == "failed" and meta["error"] == "not_started"
    b.invoker.run_streaming.assert_not_awaited()
    assert not b.runner._peer_admissions


@pytest.mark.parametrize("fault", ["notice", "receipt", "commit_before", "commit_after"])
async def test_shared_receipt_and_notice_commit_together(bridge, monkeypatch, fault):
    from genesis.peers import conversation
    b = bridge
    identity = await foreign(b)
    if fault in {"notice", "receipt"}:
        module, name = (conversation.pending_outreach, "enqueue") if fault == "notice" else (conversation.cc_sessions, "create")
        monkeypatch.setattr(module, name, AsyncMock(side_effect=RuntimeError("Synthetic pair write failure")))
    else:
        transaction = b.registry.transaction
        @asynccontextmanager
        async def broken(**kwargs):
            async with transaction(**kwargs) as db:
                yield db
                if fault == "commit_before":
                    raise RuntimeError("Synthetic precommit failure")
            raise RuntimeError("Synthetic postcommit acknowledgement failure")
        monkeypatch.setattr(b.registry, "transaction", broken)
    with pytest.raises(RuntimeError):
        await b.service.send(identity, incoming())
    tasks = await (await b.db.execute("SELECT * FROM cc_sessions WHERE source_tag='agent_api'")).fetchall()
    notices = await (await b.db.execute("SELECT * FROM pending_outreach")).fetchall()
    assert len(tasks) == len(notices) == (1 if fault == "commit_after" else 0)
    if tasks:
        assert tasks[0]["status"] == "failed" and json.loads(tasks[0]["metadata"])["error"] == "not_started"
    b.invoker.run_streaming.assert_not_awaited()
    assert not b.runner._peer_admissions


@pytest.mark.parametrize("option", [{"profile": "interact"}, {"inReplyTo": str(uuid4())}])
async def test_foreign_authority_options_refused_before_public_construction(bridge, monkeypatch, option):
    identity = await foreign(bridge)
    payload = message(context=str(uuid4()), **option)
    builder = AsyncMock(side_effect=AssertionError("Private builder reached"))
    monkeypatch.setattr("genesis.peers.conversation.SystemPromptAssembler", builder)
    with pytest.raises(ConversationRefusal) as caught:
        await bridge.service.send(identity, incoming(payload))
    assert caught.value.status == 403
    builder.assert_not_called()
    bridge.invoker.run_streaming.assert_not_awaited()


@pytest.mark.parametrize("reader", ["get", "list", "context", "retry", "approval"])
async def test_owner_history_is_unavailable_after_foreign_reclassification(bridge, reader):
    b = bridge
    payload = message()
    first = await b.service.send(b.identities["alpha"], incoming(payload))
    done = await settle(b, first)
    context = json.loads(done["metadata"])["context_id"]
    identity = await foreign(b)
    if reader == "list":
        rows, _, total = await b.service.list(identity, 20)
        assert rows == [] and total == 0
    elif reader == "approval":
        assert not await b.service.has_pending_approval(identity, done["id"])
        with pytest.raises(ConversationRefusal) as caught:
            await b.service.approvals(identity)
        assert caught.value.status == 403
    else:
        with pytest.raises(ConversationRefusal) as caught:
            if reader == "get":
                await b.service.get(identity, done["id"])
            elif reader == "context":
                await b.service.send(identity, incoming(message(context=context)))
            else:
                await b.service.send(identity, incoming(payload))
        assert caught.value.status == (409 if reader == "retry" else 404)


@pytest.mark.parametrize("change", ["ownership", "grant", "epoch", "inactive"])
async def test_authority_change_during_preparation_cannot_adopt(bridge, change):
    b = bridge
    identity = await foreign(b)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(*args):
        entered.set()
        await release.wait()
        return 3
    b.runtime._autonomy_manager.effective_level = held
    task = asyncio.create_task(b.service.send(identity, incoming()))
    await asyncio.wait_for(entered.wait(), 3)
    async with b.registry.transaction() as db:
        if change == "ownership":
            await db.execute("UPDATE peers SET same_owner=1 WHERE peer_id='alpha'")
        elif change == "grant":
            await db.execute("UPDATE peer_grants SET decision='deny' WHERE peer_id='alpha' AND capability='conversation_shared'")
        elif change == "epoch":
            await db.execute("UPDATE peers SET epoch=? WHERE peer_id='alpha'", (uuid4().hex,))
        else:
            await db.execute("UPDATE peers SET active=0 WHERE peer_id='alpha'")
    release.set()
    with pytest.raises(ConversationRefusal) as caught:
        await asyncio.wait_for(task, 3)
    assert caught.value.status == (409 if change == "ownership" else 403)
    b.invoker.run_streaming.assert_not_awaited()
    assert (await (await b.db.execute("SELECT count(*) FROM pending_outreach")).fetchone())[0] == 0
    assert not b.runner._peer_admissions


@pytest.mark.parametrize("missing", ["absent", "empty", "invalid_utf8"])
async def test_public_identity_fails_closed_without_private_fallback(bridge, monkeypatch, missing):
    identity = await foreign(bridge)
    path = bridge.home / "public-identity.md"
    if missing == "empty":
        path.write_text("   ")
    elif missing == "invalid_utf8":
        path.write_bytes(b"\xff")
    monkeypatch.setattr("genesis.peers.conversation._PUBLIC_IDENTITY", path)
    with pytest.raises(ConversationRefusal) as caught:
        await bridge.service.send(identity, incoming())
    assert caught.value.status == 503
    bridge.invoker.run_streaming.assert_not_awaited()


async def test_pre_adopt_partial_terminal_persistence_keeps_claim(bridge, monkeypatch):
    from genesis.peers import conversation
    b = bridge
    transaction = b.registry.transaction
    @asynccontextmanager
    async def lost_ack(**kwargs):
        async with transaction(**kwargs) as db:
            yield db
        raise RuntimeError("Synthetic durable commit acknowledgement lost")
    monkeypatch.setattr(b.registry, "transaction", lost_ack)
    update = conversation.cc_sessions.update_status
    calls = []
    async def status(db, sid, **kwargs):
        calls.append(True)
        assert sid in b.runner._peer_admissions
        if len(calls) == 1:
            raise RuntimeError("Synthetic status write refused after metadata commit")
        return await update(db, sid, **kwargs)
    monkeypatch.setattr(conversation.cc_sessions, "update_status", status)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await b.service.send(b.identities["alpha"], incoming())
    rows = await (await b.db.execute("SELECT status,metadata FROM cc_sessions WHERE source_tag='agent_api'")).fetchall()
    assert len(calls) == 2 and rows[0]["status"] == "failed"
    assert json.loads(rows[0]["metadata"])["error"] == "not_started"
    b.invoker.run_streaming.assert_not_awaited()


async def test_retry_cannot_join_different_inflight_trust_mode(bridge):
    b = bridge
    identity = await foreign(b)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(*args):
        entered.set()
        await release.wait()
        return 3
    b.runtime._autonomy_manager.effective_level = held
    payload = message()
    first = asyncio.create_task(b.service.send(identity, incoming(payload)))
    await asyncio.wait_for(entered.wait(), 3)
    async with b.registry.transaction() as db:
        await db.execute("UPDATE peers SET same_owner=1 WHERE peer_id='alpha'")
    with pytest.raises(ConversationRefusal) as caught:
        await asyncio.wait_for(b.service.send(identity, incoming(payload)), 3)
    assert caught.value.status == 409
    release.set()
    with pytest.raises(ConversationRefusal):
        await first
    b.invoker.run_streaming.assert_not_awaited()


@pytest.mark.parametrize("sink", ["put", "enqueue", "adapter", "queued_replay"])
async def test_internal_foreign_send_sinks_recheck_owner_boundary(bridge, sink):
    from genesis.channels.agent_adapter import AgentChannelAdapter
    from genesis.channels.base import ChannelNotConfiguredError
    from genesis.outreach.scheduler import OutreachScheduler
    from tests.test_peers.test_outbox import pipeline
    b = bridge
    box = Outbox(b.registry)
    envelope, _ = await box.prepare("alpha", "Owner-originated fixture message")
    if sink == "queued_replay":
        await box.enqueue(envelope)
    await foreign(b)
    if sink == "adapter":
        with pytest.raises(ChannelNotConfiguredError):
            await AgentChannelAdapter(b.db).send_message("alpha", "ignored", peer_envelope=envelope)
    elif sink == "queued_replay":
        p = pipeline(b)
        scheduler = OutreachScheduler(p, AsyncMock(), AsyncMock(), p._config, b.db)
        scheduler._record_job_result = AsyncMock()
        await scheduler._drain_pending_job()
    else:
        with pytest.raises(ConversationRefusal) as caught:
            await getattr(box, sink)(envelope)
        assert caught.value.status == 403
    assert (await (await b.db.execute("SELECT count(*) FROM peer_outbox")).fetchone())[0] == 0


@pytest.mark.parametrize("marker", [None, "", 0, True, [], {}, "unknown"])
async def test_malformed_receipt_modes_cannot_default_to_owner(bridge, marker):
    b = bridge
    row = await b.service.send(b.identities["alpha"], incoming())
    done = await settle(b, row)
    meta = json.loads(done["metadata"])
    meta["conversation_trust"] = marker
    await b.db.execute("UPDATE cc_sessions SET metadata=? WHERE id=?", (json.dumps(meta), row["id"]))
    await b.db.commit()
    with pytest.raises(ConversationRefusal) as caught:
        await b.service.get(b.identities["alpha"], row["id"])
    assert caught.value.status == 404
    assert (await b.service.list(b.identities["alpha"], 20))[0] == []
