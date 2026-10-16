"""Real durable queues, governed delivery and HTTP collection/reply cycle."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from genesis.channels.agent_adapter import AgentChannelAdapter
from genesis.content.formatter import ContentFormatter
from genesis.outreach.config import QUIET_HOURS_DISABLED, OutreachConfig
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.scheduler import OutreachScheduler
from genesis.peers.conversation import ConversationRefusal
from genesis.peers.outbox import Outbox, PeerEnvelope
from tests.test_peers.test_conversation import (
    ROOT,
    message,
    sdk_client,
    sdk_send,
    sdk_settle,
)
from tests.test_peers.test_conversation import bridge as bridge
from tests.test_peers.test_conversation import http_bridge as http_bridge


def pipeline(b, **changes):
    config = OutreachConfig(
        quiet_hours=QUIET_HOURS_DISABLED,
        channel_preferences={"default": "telegram"},
        thresholds={"notification": 0},
        max_daily=999,
        surplus_daily=999,
        content_daily=999,
        notification_daily=999,
        morning_report_time="07:00",
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )
    config = replace(config, **changes)
    return OutreachPipeline(
        GovernanceGate(config, b.db),
        AsyncMock(),
        ContentFormatter(),
        {"agent": AgentChannelAdapter(b.db)},
        db=b.db,
        config=config,
    )


async def test_store_retry_ack_retains_anchor_and_owner_scope(bridge):
    outbox = Outbox(bridge.registry)
    envelope, existing = await outbox.prepare("alpha", "What do you think?")
    assert existing is None
    await outbox.put(envelope)
    assert await outbox.put(envelope) == envelope.message_id
    rows, _ = await outbox.poll(bridge.identities["alpha"])
    assert rows[0]["message"] == envelope.message
    assert await outbox.poll(bridge.identities["beta"]) == ([], "")
    with pytest.raises(ConversationRefusal, match="not_found"):
        await outbox.ack(bridge.identities["beta"], envelope.message_id)
    with pytest.raises(ConversationRefusal, match="state_conflict"):
        await outbox.prepare("alpha", "Changed retry", message_id=envelope.message_id)
    await outbox.ack(bridge.identities["alpha"], envelope.message_id)
    await outbox.ack(bridge.identities["alpha"], envelope.message_id)
    assert await outbox.poll(bridge.identities["alpha"]) == ([], "")
    _, anchor = await outbox.prepare("alpha", "What do you think?", message_id=envelope.message_id)
    assert anchor["acknowledged_at"]
    assert (await (await bridge.db.execute("SELECT count(*) FROM peer_outbox")).fetchone())[0] == 1


async def test_poll_pages_do_not_consume_and_keep_acked_cursor(bridge):
    box = Outbox(bridge.registry)
    sent = []
    for i in range(3):
        env, _ = await box.prepare("alpha", f"Question {i}")
        await box.put(env)
        sent.append(env)
    first, token = await box.poll(bridge.identities["alpha"], 1)
    assert first[0]["message"]["messageId"] == token == sent[0].message_id
    assert await box.poll(bridge.identities["alpha"], 1) == (first, token)
    await box.ack(bridge.identities["alpha"], token)
    second, _ = await box.poll(bridge.identities["alpha"], 1, token)
    assert second[0]["message"]["messageId"] == sent[1].message_id
    assert (await box.poll(bridge.identities["alpha"], 1))[0] == second
    for size in (0, -1, 101):
        with pytest.raises(ConversationRefusal):
            await box.poll(bridge.identities["alpha"], size)


async def test_revocation_and_disabled_mode_block_store_and_disclosure(bridge):
    box = Outbox(bridge.registry)
    env, _ = await box.prepare("alpha", "Question")
    await box.put(env)
    await bridge.registry.revoke("alpha")
    for call in (
        box.put(env),
        box.poll(bridge.identities["alpha"]),
        box.ack(bridge.identities["alpha"], env.message_id),
    ):
        with pytest.raises(ConversationRefusal, match="unauthorized"):
            await call
    await bridge.registry.configure("disabled")
    with pytest.raises(ConversationRefusal, match="not_configured"):
        await box.prepare("beta", "Question")


async def test_scans_whole_outbound_envelope_before_store(bridge):
    box = Outbox(bridge.registry)
    unsafe = "prefix " + json.dumps({"to" + "ken": "synthetic"})
    with pytest.raises(ConversationRefusal, match="outbound_refused"):
        await box.prepare("alpha", unsafe)
    with pytest.raises(ConversationRefusal, match="outbound_refused"):
        await box.prepare("alpha", "x" * (256 * 1024))
    assert (await (await bridge.db.execute("SELECT count(*) FROM peer_outbox")).fetchone())[0] == 0


async def test_governed_delivery_preserves_unformatted_typed_payload_and_identity(bridge):
    box = Outbox(bridge.registry)
    p = pipeline(bridge)
    # GENERIC formatter can shorten this; the typed A2A payload must stay exact.
    text = "Same prefix " * 1000
    for peer in ("alpha", "beta", "alpha"):
        env, _ = await box.prepare(peer, text)
        result = await p.submit(env.request())
        assert result.status.value == "delivered"
    rows, _ = await box.poll(bridge.identities["alpha"])
    assert len(rows) == 2 and all(r["message"]["parts"][0]["text"] == text for r in rows)
    duplicate = await p.submit(env.request())
    assert duplicate.status.value == "rejected"
    # Raw generic outreach cannot manufacture an addressed envelope.
    raw = await p.submit(replace(env.request(), peer_envelope=None))
    assert raw.status.value in {"rejected", "ignored"}


async def test_real_http_native_mcp_queue_poll_ack_correlated_reply_restart(
    http_bridge, monkeypatch
):
    from genesis.mcp import outreach_mcp

    b, app, base, owner = http_bridge
    p = pipeline(b)
    monkeypatch.setattr(outreach_mcp, "_db", b.db)
    monkeypatch.setattr(outreach_mcp, "_pipeline", None)  # actual native MCP subprocess path
    mid = str(uuid4())
    response = json.loads(
        await outreach_mcp.agent_send.fn("alpha", "Genesis asks for an opinion", message_id=mid)
    )
    assert response["status"] == "queued_for_governance"
    retry = json.loads(
        await outreach_mcp.agent_send.fn(
            "alpha",
            "Genesis asks for an opinion",
            message_id=mid,
            context_id=response["context_id"],
        )
    )
    assert retry == response
    assert (await (await b.db.execute("SELECT count(*) FROM pending_outreach")).fetchone())[0] == 1
    for peer, text, context in (
        ("beta", "Genesis asks for an opinion", response["context_id"]),
        ("alpha", "Changed retry", response["context_id"]),
        ("alpha", "Genesis asks for an opinion", str(uuid4())),
    ):
        refused = json.loads(
            await outreach_mcp.agent_send.fn(peer, text, message_id=mid, context_id=context)
        )
        assert refused == {"code": "outbound_refused", "status": "refused"}
    assert (await Outbox(b.registry).poll(b.identities["alpha"]))[0] == []
    scheduler = OutreachScheduler(p, AsyncMock(), AsyncMock(), p._config, b.db)
    scheduler._record_job_result = AsyncMock()
    await scheduler._drain_pending_job()
    async with sdk_client(base) as (client, transport):
        page = (await client.get(base + ROOT + "/outbox")).json()
        item = page["messages"][0]["message"]
        assert item["messageId"] == mid and item["contextId"] == response["context_id"]
        assert item["parts"][0]["text"] == "Genesis asks for an opinion"
        # Recreate service (restart projection); DB anchor survives repeated polling.
        app.config.pop("GENESIS_CONVERSATION_SERVICE", None)
        assert (await client.get(base + ROOT + "/outbox")).json() == page
        for _ in range(2):
            ack = await client.post(base + ROOT + f"/outbox/{mid}:ack", json={})
            assert ack.status_code == 200
        assert not (await client.get(base + ROOT + "/outbox")).json()["messages"]
        payload = message(context=item["contextId"], inReplyTo=mid)
        task = await sdk_send(transport, payload)
        await sdk_settle(transport, task.id)
        assert "Genesis asks for an opinion" in b.invoker.run_streaming.call_args.args[0].prompt
        assert (
            json.loads((await b.service.get(b.identities["alpha"], task.id))["metadata"])[
                "in_reply_to"
            ]
            == mid
        )
        assert (await sdk_send(transport, payload)).id == task.id
        another = await client.post(
            base + ROOT + "/message:send", json=message(context=item["contextId"], inReplyTo=mid)
        )
        assert another.status_code == 409
        follow = await sdk_send(transport, message(context=item["contextId"]))
        await sdk_settle(transport, follow.id)
        assert b.invoker.run_streaming.await_count == 2
        oversized = await client.post(
            base + ROOT + f"/outbox/{mid}:ack", content=b" " * (256 * 1024 + 1)
        )
        assert oversized.status_code == 413


async def test_reply_correlation_rejects_other_peer_or_context_before_effects(bridge):
    box = Outbox(bridge.registry)
    env, _ = await box.prepare("alpha", "Question")
    await box.put(env)
    from genesis.peers.protocol import parse_send

    for peer, context in (("beta", env.context_id), ("alpha", str(uuid4()))):
        with pytest.raises(ConversationRefusal, match="not_found"):
            await bridge.service.send(
                bridge.identities[peer],
                parse_send(json.dumps(message(context=context, inReplyTo=env.message_id)).encode()),
            )
    bridge.invoker.run_streaming.assert_not_awaited()


async def test_pending_queue_atomic_identity_changed_retry_and_owner_cancellation(bridge):
    box = Outbox(bridge.registry)
    env, _ = await box.prepare("alpha", "A question")
    answers = await asyncio.gather(*(box.enqueue(env) for _ in range(4)))
    assert len(set(answers)) == 1
    assert (
        await (
            await bridge.db.execute("SELECT count(*) FROM pending_outreach WHERE channel='agent'")
        ).fetchone()
    )[0] == 1
    changed = env.message
    changed["parts"][0]["text"] = "Different question"
    with pytest.raises(ConversationRefusal, match="state_conflict"):
        await box.enqueue(replace(env, message_json=json.dumps(changed)))
    from genesis.db.crud import pending_outreach

    assert (await pending_outreach.cancel(bridge.db, answers[0][0]))[0]
    assert (await box.enqueue(env))[1] == "queue_closed"
    assert not (await box.poll(bridge.identities["alpha"]))[0]


async def test_deferred_retry_preserves_typed_identity_and_rechecks_governance(bridge, monkeypatch):
    from genesis.db.crud import deferred_work
    from genesis.resilience.deferred_work import DeferredWorkQueue
    from genesis.resilience.outreach_recovery import OutreachRecoveryWorker

    box = Outbox(bridge.registry)
    env, _ = await box.prepare("alpha", "Delayed question")
    p = pipeline(bridge)
    queue = DeferredWorkQueue(bridge.db)
    p._deferred_queue = queue
    adapter = p._channels["agent"]
    original = adapter.send_message
    monkeypatch.setattr(
        adapter, "send_message", AsyncMock(side_effect=RuntimeError("synthetic transient"))
    )
    assert (await p.submit(env.request())).status.value == "failed"
    items = await deferred_work.query_pending(bridge.db, work_type="outreach_delivery", limit=10)
    assert len(items) == 1
    recovered = PeerEnvelope.decode(json.loads(items[0]["payload_json"])["peer_envelope"])
    assert recovered == env
    monkeypatch.setattr(adapter, "send_message", original)
    worker = OutreachRecoveryWorker(queue=queue, pipeline=p, db=bridge.db)
    await worker._retry(items[0])
    assert (await box.poll(bridge.identities["alpha"]))[0][0]["message"] == env.message
    assert not await deferred_work.query_pending(bridge.db, work_type="outreach_delivery", limit=10)
    other, _ = await box.prepare("alpha", "Other delayed question")
    await p._defer(
        "synthetic", "agent", "Other delayed question", other.request(), "synthetic failure"
    )
    items = await deferred_work.query_pending(bridge.db, work_type="outreach_delivery", limit=10)
    p.reload_config(replace(p._config, max_daily=0))
    await worker._retry(items[0])
    assert len((await box.poll(bridge.identities["alpha"]))[0]) == 1
    remaining = await deferred_work.query_pending(
        bridge.db, work_type="outreach_delivery", limit=10
    )
    assert len(remaining) == 1 and remaining[0]["id"] == items[0]["id"]
    p.reload_config(replace(p._config, max_daily=999))
    await worker._retry(remaining[0])
    assert len((await box.poll(bridge.identities["alpha"]))[0]) == 2


@pytest.mark.parametrize("closure", ["open", "cancel", "dequeue"])
async def test_live_mcp_and_stale_adapter_respect_pending_anchor(bridge, monkeypatch, closure):
    from genesis.db.crud import pending_outreach
    from genesis.mcp import outreach_mcp

    box = Outbox(bridge.registry)
    monkeypatch.setattr(outreach_mcp, "_db", bridge.db)
    monkeypatch.setattr(outreach_mcp, "_pipeline", None)
    mid = str(uuid4())
    first = json.loads(
        await outreach_mcp.agent_send.fn("alpha", "Original question", message_id=mid)
    )
    env, _ = await box.prepare("alpha", "Original question", first["context_id"], mid)
    if closure == "cancel":
        assert (await pending_outreach.cancel(bridge.db, first["pending_id"]))[0]
    elif closure == "dequeue":
        assert await pending_outreach.mark_delivered(
            bridge.db, first["pending_id"], delivered_at="synthetic"
        )
    live = pipeline(bridge)
    live.submit = AsyncMock(wraps=live.submit)
    monkeypatch.setattr(outreach_mcp, "_pipeline", live)
    changed = json.loads(
        await outreach_mcp.agent_send.fn("alpha", "Changed question", first["context_id"], mid)
    )
    assert changed == {"code": "outbound_refused", "status": "refused"}
    retry = json.loads(
        await outreach_mcp.agent_send.fn("alpha", "Original question", first["context_id"], mid)
    )
    assert retry["status"] == ("queued_for_governance" if closure == "open" else "queue_closed")
    assert retry["pending_id"] == first["pending_id"]
    live.submit.assert_not_awaited()
    altered = env.message
    altered["parts"][0]["text"] = "Changed question"
    with pytest.raises(ConversationRefusal, match="state_conflict"):
        await box.put(replace(env, message_json=json.dumps(altered)))
    if closure == "open":
        await box.put(env)
        await pending_outreach.mark_delivered(
            bridge.db, first["pending_id"], delivered_at="synthetic"
        )
        assert await box.put(env) == mid  # retained collection beats later queue closure
    else:
        with pytest.raises(ConversationRefusal, match="state_conflict"):
            await box.put(env)
        assert not (await box.poll(bridge.identities["alpha"]))[0]


async def test_enqueue_rechecks_outbox_committed_after_prepare(bridge):
    box = Outbox(bridge.registry)
    env, _ = await box.prepare("alpha", "Original question")
    await box.put(env)
    assert await box.enqueue(env) == ("", "queued_for_collection")
    altered = env.message
    altered["parts"][0]["text"] = "Changed question"
    with pytest.raises(ConversationRefusal, match="state_conflict"):
        await box.enqueue(replace(env, message_json=json.dumps(altered)))
    assert (await (await bridge.db.execute("SELECT count(*) FROM pending_outreach")).fetchone())[
        0
    ] == 0


async def test_backup_and_normal_restore_keep_anchor_but_clear_authority(bridge, tmp_path):
    import aiosqlite

    from genesis.db.crud.peer_restore import reset_peer_authority
    from genesis.peers.registry import PeerRegistry

    box = Outbox(bridge.registry)
    env, _ = await box.prepare("alpha", "Retained question")
    await box.put(env)
    target = tmp_path / "restored.db"
    async with aiosqlite.connect(target) as copied:
        await bridge.db.backup(copied)
    assert await asyncio.to_thread(reset_peer_authority, target)
    restored = PeerRegistry(target)
    assert (await restored.settings())["mode"] == "disabled"
    assert (await restored.get("alpha"))["epoch"] != env.epoch
    assert await restored.grants("alpha") == {}
    async with restored.connection() as db:
        assert (await (await db.execute("SELECT count(*) FROM peer_outbox")).fetchone())[0] == 1
    with pytest.raises(ConversationRefusal, match="not_configured"):
        await Outbox(restored).poll(bridge.identities["alpha"])


async def test_registered_send_tool_and_native_profile_boundary():
    from genesis.cc.direct_session import PROFILES
    from genesis.mcp.outreach_mcp import mcp

    assert "agent_send" in await mcp.get_tools()
    tool = "mcp__genesis-outreach__agent_send"
    assert tool in PROFILES["observe"]
    assert tool in PROFILES["mail"]
    assert tool in PROFILES["campaign"]
    assert tool not in PROFILES["interact"]


async def test_ack_invalid_id_never_enters_structured_audit(http_bridge, caplog):
    import logging

    b, app, base, owner = http_bridge
    opaque = "synthetic-private-invalid-id"
    with caplog.at_level(logging.INFO, logger="genesis.dashboard.routes.agent_api"):
        async with sdk_client(base) as (client, _):
            response = await client.post(base + ROOT + f"/outbox/{opaque}:ack", json={})
    assert response.status_code == 400
    audits = [
        r.getMessage() for r in caplog.records if r.name == "genesis.dashboard.routes.agent_api"
    ]
    assert audits and all(opaque not in entry for entry in audits)


async def test_machine_collection_does_not_grade_human_engagement_or_throttle_digest(bridge):
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud.outreach import get_engagement_stats
    from genesis.outreach.engagement import EngagementTracker
    from genesis.outreach.types import OutreachCategory, OutreachRequest

    box = Outbox(bridge.registry)
    p = pipeline(bridge)
    for i in range(5):
        env, _ = await box.prepare("alpha", f"Synthetic question {i}")
        assert (await p.submit(env.request())).status.value == "delivered"
        await box.ack(bridge.identities["alpha"], env.message_id)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await bridge.db.execute("UPDATE outreach_history SET delivered_at=?", (old,))
    await bridge.db.commit()
    await EngagementTracker(bridge.db).check_timeouts()
    assert (await get_engagement_stats(bridge.db))["total"] == 0
    assert (await (await bridge.db.execute("SELECT count(*) FROM ledger_predictions")).fetchone())[
        0
    ] == 0
    digest = OutreachRequest(
        category=OutreachCategory.DIGEST,
        topic="Synthetic owner digest",
        context="Synthetic",
        salience_score=1,
    )
    assert (await p._governance.check(digest)).verdict.value == "allow"


async def test_collection_and_ack_survive_missing_runner(http_bridge):
    b, app, base, owner = http_bridge
    env, _ = await Outbox(b.registry).prepare("alpha", "A retained question")
    await Outbox(b.registry).put(env)
    async with sdk_client(base) as (client, transport):
        b.runtime._direct_session_runner = None
        response = await client.get(base + ROOT + "/outbox")
        assert response.status_code == 200
        assert response.json()["messages"][0]["message"]["messageId"] == env.message_id
        response = await client.post(base + ROOT + f"/outbox/{env.message_id}:ack", json={})
        assert response.status_code == 200
    assert (await Outbox(b.registry).poll(b.identities["alpha"]))[0] == []


@pytest.mark.parametrize("live", [False, True])
async def test_generic_agent_channel_refused_without_queueing(bridge, monkeypatch, live):
    from genesis.mcp import outreach_mcp

    p = pipeline(bridge)
    monkeypatch.setattr(outreach_mcp, "_db", bridge.db)
    monkeypatch.setattr(outreach_mcp, "_pipeline", p if live else None)
    monkeypatch.setattr(
        outreach_mcp, "_server_rpc", AsyncMock(side_effect=AssertionError("No server RPC allowed"))
    )
    if live:
        from genesis.outreach.types import OutreachCategory, OutreachRequest
        from genesis.resilience.deferred_work import DeferredWorkQueue

        p._deferred_queue = DeferredWorkQueue(bridge.db)
        untyped = OutreachRequest(
            category=OutreachCategory.NOTIFICATION,
            topic="Untyped",
            context="Untyped text",
            salience_score=1,
            channel="agent",
            verbatim=True,
        )
        assert (await p.submit_urgent(untyped)).status.value == "rejected"
    result = json.loads(
        await outreach_mcp.outreach_send.fn("Untyped text", "notification", "agent")
    )
    assert result["status"] == "refused"
    waiting = json.loads(
        await outreach_mcp.outreach_send_and_wait.fn(
            "Untyped text", "notification", "agent", timeout_seconds=1
        )
    )
    assert waiting["status"] == "refused"
    outreach_mcp._server_rpc.assert_not_awaited()
    assert (await (await bridge.db.execute("SELECT count(*) FROM pending_outreach")).fetchone())[
        0
    ] == 0
    assert (await (await bridge.db.execute("SELECT count(*) FROM deferred_work_queue")).fetchone())[
        0
    ] == 0


async def test_dedup_after_retained_delivery_closes_pending_row(bridge, monkeypatch):
    from genesis.mcp import outreach_mcp

    p = pipeline(bridge)
    monkeypatch.setattr(outreach_mcp, "_db", bridge.db)
    monkeypatch.setattr(outreach_mcp, "_pipeline", None)
    answer = json.loads(await outreach_mcp.agent_send.fn("alpha", "A retained question"))
    scheduler = OutreachScheduler(p, AsyncMock(), AsyncMock(), p._config, bridge.db)
    scheduler._record_job_result = AsyncMock()
    real_mark = scheduler._mark_row_delivered
    scheduler._mark_row_delivered = AsyncMock(side_effect=OSError("Synthetic interruption"))
    await scheduler._drain_pending_job()
    assert len((await Outbox(bridge.registry).poll(bridge.identities["alpha"]))[0]) == 1
    scheduler._mark_row_delivered = real_mark
    await scheduler._drain_pending_job()
    row = await (
        await bridge.db.execute(
            "SELECT delivered_at FROM pending_outreach WHERE id=?", (answer["pending_id"],)
        )
    ).fetchone()
    assert row[0] is not None
    assert len((await Outbox(bridge.registry).poll(bridge.identities["alpha"]))[0]) == 1
