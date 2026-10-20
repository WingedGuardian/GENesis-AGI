"""Durable incidents drive the real outreach pipeline with a fake Telegram adapter."""

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from genesis.content.types import FormattedContent
from genesis.db.crud import alert_events
from genesis.db.schema import create_all_tables
from genesis.observability.agent_api_alerts import (
    ALERT_ID,
    active_alerts,
    alert_observation,
    notify_pending,
)
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline


@pytest.fixture
async def setup_notice(tmp_path):
    db = await aiosqlite.connect(tmp_path / "notices.db")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    config = SimpleNamespace(delivery_routing={}, voice_hours=(9, 2),
                             engagement_timeout_hours=24)
    formatter = Mock()
    formatter.format.side_effect = lambda text, target: FormattedContent(
        text=text, target=target, truncated=False, original_length=len(text))
    adapter = AsyncMock()
    adapter.send_message.return_value = "notice-id"
    deferred = AsyncMock()
    pipeline = OutreachPipeline(governance=GovernanceGate(config, db), drafter=AsyncMock(),
                                formatter=formatter, channels={"telegram": adapter}, db=db,
                                config=config, recipients={"telegram": "fixture-owner"},
                                deferred_queue=deferred)
    try:
        yield db, pipeline, adapter, deferred
    finally:
        await db.close()


async def transition(db, status, *, now=None):
    now = now or datetime.now(UTC).isoformat()
    observation = dict(status=status, reasons=["fixture_fault"], pid=12345,
                       checked_at=now)
    alerts = await active_alerts(db, observation)
    await alert_events.reconcile_open_set(
        db, active=[dict(alert_id=a["id"], source="agent_api", severity=a["severity"],
                         message=a["message"]) for a in alerts],
        now=now,
        resolution_reasons={ALERT_ID: status} if status in {"healthy", "disabled"} else None,
    )


async def test_failure_unknown_recovery_and_new_episode(setup_notice):
    db, pipeline, adapter, _ = setup_notice
    await transition(db, "degraded")
    await notify_pending(db, pipeline)
    await transition(db, "unknown")
    assert len(await alert_events.list_open(db)) == 1
    await notify_pending(db, pipeline)
    assert adapter.send_message.await_count == 1
    await transition(db, "healthy")
    await notify_pending(db, pipeline)
    await notify_pending(db, pipeline)
    assert adapter.send_message.await_count == 2
    await transition(db, "degraded")
    await notify_pending(db, pipeline)
    assert adapter.send_message.await_count == 3
    pipeline._drafter.draft.assert_not_awaited()


async def test_disabled_cancels_undelivered_incident_after_reopen(setup_notice):
    db, pipeline, adapter, _ = setup_notice
    await transition(db, "degraded")
    await transition(db, "disabled")
    await transition(db, "healthy")
    await notify_pending(db, pipeline)
    adapter.send_message.assert_not_awaited()
    row = await (await db.execute("SELECT * FROM alert_events")).fetchone()
    assert row["resolution_reason"] == "disabled"


async def test_distinct_incidents_with_identical_messages_are_delivered(setup_notice):
    db, pipeline, adapter, _ = setup_notice
    now = datetime.now(UTC).isoformat()
    for state in ("degraded", "healthy", "degraded"):
        await transition(db, state, now=now)
        await notify_pending(db, pipeline)
    assert adapter.send_message.await_count == 3
    messages = [call.args[1] for call in adapter.send_message.await_args_list]
    assert messages[0] == messages[2]


async def test_restart_and_permanent_dedup_outlive_pipeline_window(setup_notice):
    db, pipeline, adapter, _ = setup_notice
    await transition(db, "degraded")
    await notify_pending(db, pipeline)
    await db.execute("UPDATE outreach_history SET delivered_at='2000-01-01T00:00:00+00:00'")
    await db.commit()
    path = (await (await db.execute("PRAGMA database_list")).fetchone())[2]
    async with aiosqlite.connect(path) as reopened:
        reopened.row_factory = aiosqlite.Row
        replacement = OutreachPipeline(
            governance=GovernanceGate(pipeline._config, reopened), drafter=AsyncMock(),
            formatter=pipeline._formatter, channels={"telegram": adapter}, db=reopened,
            config=pipeline._config, recipients={"telegram": "fixture-owner"},
        )
        await notify_pending(reopened, replacement)
        assert adapter.send_message.await_count == 1
        await transition(reopened, "healthy")
        await notify_pending(reopened, replacement)
        assert adapter.send_message.await_count == 2


async def test_transient_delivery_failure_has_one_retry_owner(setup_notice):
    db, pipeline, adapter, deferred = setup_notice
    await transition(db, "degraded")
    adapter.send_message.side_effect = RuntimeError("fixture transport unavailable")
    await notify_pending(db, pipeline)
    deferred.enqueue.assert_not_awaited()
    await transition(db, "healthy")
    await notify_pending(db, pipeline)
    assert adapter.send_message.await_count == 2  # recovery has not overtaken outage
    adapter.send_message.side_effect = None
    await notify_pending(db, pipeline)
    assert adapter.send_message.await_count == 4  # pending outage, then recovery


async def test_unconfigured_and_ignored_channels_do_not_retry_storm(setup_notice):
    from genesis.channels.base import ChannelNotConfiguredError

    db, pipeline, adapter, _ = setup_notice
    await transition(db, "degraded")
    pipeline._recipients.clear()
    await notify_pending(db, pipeline)
    adapter.send_message.assert_not_awaited()
    pipeline._recipients["telegram"] = "fixture-owner"
    adapter.send_message.side_effect = ChannelNotConfiguredError("fixture unconfigured")
    await notify_pending(db, pipeline)
    await notify_pending(db, pipeline)
    assert adapter.send_message.await_count == 1
    adapter.send_message.side_effect = None
    replacement = OutreachPipeline(
        governance=GovernanceGate(pipeline._config, db), drafter=AsyncMock(),
        formatter=pipeline._formatter, channels={"telegram": adapter}, db=db,
        config=pipeline._config, recipients={"telegram": "fixture-owner"},
    )
    await notify_pending(db, replacement)
    assert adapter.send_message.await_count == 2


def test_grace_preserves_unknown_semantics():
    now = datetime.now(UTC)
    assert alert_observation({"status": "degraded"}, boot_at=now, now=now)["status"] == "unknown"
    assert alert_observation({"status": "degraded"}, boot_at=now-timedelta(seconds=90),
                             now=now)["status"] == "degraded"


async def test_migration_preserves_legacy_rows_and_is_idempotent():
    migration = importlib.import_module("genesis.db.migrations.20261016222815_alert_resolution_reason")
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("CREATE TABLE alert_events(id TEXT, resolved_at TEXT)")
        await db.execute("INSERT INTO alert_events VALUES('legacy', 'earlier')")
        await migration.up(db)
        await migration.up(db)
        assert tuple(await (await db.execute("SELECT * FROM alert_events")).fetchone()) == (
            "legacy", "earlier", None,
        )


async def test_awareness_uses_one_captured_observation(setup_notice, monkeypatch):
    from genesis.awareness.loop import _persist_health_alerts
    from genesis.runtime import GenesisRuntime

    db, pipeline, adapter, _ = setup_notice
    runtime = SimpleNamespace(_outreach_pipeline=pipeline)
    monkeypatch.setattr(GenesisRuntime, "peek", classmethod(lambda cls: runtime))
    calls = []

    async def compute(*, agent_api_observation):
        state = ("degraded", "unknown", "healthy")[len(calls)]
        calls.append(state)
        agent_api_observation.update(status=state, reasons=["fixture_fault"], pid=12345)
        return [], set()

    monkeypatch.setattr("genesis.mcp.health.errors._compute_alerts", compute)
    await _persist_health_alerts(db)
    await _persist_health_alerts(db)
    assert len(await alert_events.list_open(db)) == 1
    await _persist_health_alerts(db)
    assert calls == ["degraded", "unknown", "healthy"]
    row = await (await db.execute("SELECT * FROM alert_events")).fetchone()
    assert row["resolution_reason"] == "healthy"
    assert adapter.send_message.await_count == 2
