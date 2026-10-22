"""Deferred-work alarm segmentation.

The `deferred_work_queue` table backs BOTH genuine resilience-recovery work AND
the scheduled dream-synthesis worklist (~500 slices enqueued weekly, drained a
bounded budget/day). Counting the raw total against the `>100` depth alarm fired
`queue:deferred_work` WARNING on every awareness tick. These tests pin the
split: the alarm watches the recovery-only subset, batch worklists are excluded
until they stall past a full drain cycle, and the raw total stays honest for
display.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from genesis.db.crud import deferred_work as crud
from genesis.db.schema import create_all_tables
from genesis.resilience.deferred_work import (
    BATCH_WORK_TYPES,
    DRAIN,
    MEMORY_OPS,
    REFLECTION,
    STALE_WORKLIST_DAYS,
    DeferredWorkQueue,
)

WORKLIST_TYPE = "dream_synthesis_slice"
_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _queue(db) -> DeferredWorkQueue:
    return DeferredWorkQueue(db, clock=lambda: _NOW)


async def _seed_recovery(db, n: int) -> None:
    q = _queue(db)
    for _ in range(n):
        await q.enqueue("reflection", None, REFLECTION, "{}", "degraded", DRAIN)


async def _seed_worklist(db, n: int, *, age_days: float = 0.0) -> None:
    """Seed batch worklist rows with a controllable deferred_at age."""
    deferred_at = (_NOW - timedelta(days=age_days)).isoformat()
    for i in range(n):
        await crud.create(
            db,
            id=f"wl-{age_days}-{i}",
            work_type=WORKLIST_TYPE,
            priority=MEMORY_OPS,
            payload_json="{}",
            deferred_at=deferred_at,
            deferred_reason="dream_cycle weekly synthesis worklist",
            created_at=deferred_at,
            staleness_policy=DRAIN,
        )


class TestQueueCounts:
    @pytest.mark.asyncio
    async def test_recovery_excludes_fresh_worklist(self, db):
        await _seed_recovery(db, 3)
        await _seed_worklist(db, 5, age_days=0.0)

        q = _queue(db)
        assert await q.count_pending() == 8  # raw total, honest
        assert await q.count_worklist_pending() == 5  # batch subset
        assert await q.count_recovery_pending() == 3  # alarm-eligible subset

    @pytest.mark.asyncio
    async def test_stale_worklist_folds_into_recovery(self, db):
        # A worklist that has sat past a full drain cycle means the drain broke —
        # it should re-count as recovery backlog so the alarm surfaces it.
        await _seed_recovery(db, 2)
        await _seed_worklist(db, 4, age_days=STALE_WORKLIST_DAYS + 1)

        q = _queue(db)
        assert await q.count_worklist_pending() == 4
        # 2 recovery + 4 stale worklist folded back in
        assert await q.count_recovery_pending() == 6

    @pytest.mark.asyncio
    async def test_fresh_and_stale_worklist_mix(self, db):
        await _seed_worklist(db, 3, age_days=1.0)  # fresh, excluded
        await _seed_worklist(db, 2, age_days=STALE_WORKLIST_DAYS + 2)  # stale, folded

        q = _queue(db)
        assert await q.count_worklist_pending() == 5  # all batch, display
        assert await q.count_recovery_pending() == 2  # only the stale ones
        assert (await q.count_pending_split())["stalled"] == 2

    @pytest.mark.asyncio
    async def test_stalled_worklist_ignores_recovery_items(self, db):
        await _seed_recovery(db, 4)
        await _seed_worklist(db, 3, age_days=STALE_WORKLIST_DAYS + 1)
        assert (await _queue(db).count_pending_split())["stalled"] == 3


class TestSnapshotSplit:
    @pytest.mark.asyncio
    async def test_queues_snapshot_exposes_split(self, db):
        import importlib

        # The snapshots package re-exports the ``queues`` FUNCTION, which shadows
        # the submodule on attribute access — import the module object explicitly.
        qmod = importlib.import_module("genesis.observability.snapshots.queues")

        await _seed_recovery(db, 2)
        await _seed_worklist(db, 6, age_days=0.0)

        # Pass the fixed-clock queue so the stale-worklist cutoff is deterministic
        # (fresh age-0 worklist is excluded from recovery regardless of wall clock).
        result = await qmod.queues(db, _queue(db), None)

        assert result["deferred_work"] == 8  # raw total
        assert result["deferred_worklist"] == 6  # batch, display-only
        assert result["deferred_recovery"] == 2  # alarm-eligible
        assert result["deferred_worklist_stalled"] == 0  # all fresh

    @pytest.mark.asyncio
    async def test_deferred_items_sample_excludes_batch_worklists(self, db):
        # The "Deferred review" card renders `deferred_items`. Batch worklists
        # drain on a daily budget and sit pending for days by design, so they
        # must not crowd the sample or surface as items "needing review" — even
        # when they dominate the pending set (400 dream slices was the live
        # trigger). Only genuine recovery items belong here.
        import importlib

        qmod = importlib.import_module("genesis.observability.snapshots.queues")

        await _seed_recovery(db, 2)
        await _seed_worklist(db, 50, age_days=0.0)  # batch flood

        result = await qmod.queues(db, _queue(db), None)

        sampled_types = {item["type"] for item in result["deferred_items"]}
        assert WORKLIST_TYPE not in sampled_types
        assert sampled_types == {"reflection"}
        assert len(result["deferred_items"]) == 2

    @pytest.mark.asyncio
    async def test_missing_queue_with_persisted_work_is_unavailable(self, db):
        import importlib

        from genesis.outreach.morning_report import MorningReportGenerator

        qmod = importlib.import_module("genesis.observability.snapshots.queues")

        await _seed_recovery(db, 2)
        await _seed_worklist(db, 3)
        result = await qmod.queues(db, None, None)
        for key in (
            "deferred_work", "deferred_recovery", "deferred_worklist",
            "deferred_worklist_stalled",
        ):
            assert result[key] is None
        assert "deferred_work: queue unavailable" in result["errors"]
        assert MorningReportGenerator._format_deferred_line(result) == "- Deferred work: unavailable"
        assert await crud.count_pending(db) == 5

    @pytest.mark.asyncio
    async def test_missing_dead_letter_wrapper_does_not_certify_an_empty_queue(self, db):
        import importlib

        from genesis.db.crud import dead_letter
        from genesis.outreach.morning_report import MorningReportGenerator

        qmod = importlib.import_module("genesis.observability.snapshots.queues")
        await dead_letter.create(
            db, id="missing-wrapper", operation_type="test", payload="{}",
            target_provider="test", failure_reason="test", created_at=_NOW.isoformat(),
        )
        resolve = AsyncMock()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(qmod, "_resolve_dead_letter_alerts", resolve)
            result = await qmod.queues(db, None, None)
        assert result["dead_letters"] is None
        assert "dead_letters: queue unavailable" in result["errors"]
        report = MorningReportGenerator(AsyncMock(), db, AsyncMock())
        assert "dead_letters=unavailable" in report._format_health({"queues": result})
        resolve.assert_not_awaited()
        assert await dead_letter.count_pending(db) == 1

    @pytest.mark.asyncio
    async def test_fully_absent_sources_keep_the_zero_convention(self):
        import importlib

        qmod = importlib.import_module("genesis.observability.snapshots.queues")
        result = await qmod.queues(None, None, None)
        assert result["deferred_work"] == 0
        assert result["deferred_recovery"] == 0
        assert result["deferred_worklist"] == 0
        assert result["deferred_worklist_stalled"] == 0
        assert result["dead_letters"] == 0

    @pytest.mark.asyncio
    async def test_present_empty_queues_still_measure_zero(self, db):
        import importlib

        qmod = importlib.import_module("genesis.observability.snapshots.queues")
        dead = AsyncMock()
        dead.get_pending_count.return_value = 0
        dead.get_stuck_count.return_value = 0
        result = await qmod.queues(db, _queue(db), dead)
        assert result["deferred_work"] == 0
        assert result["deferred_recovery"] == 0
        assert result["deferred_worklist"] == 0
        assert result["deferred_worklist_stalled"] == 0
        assert result["dead_letters"] == 0
        assert not any("queue unavailable" in error for error in result.get("errors", []))

    @pytest.mark.asyncio
    async def test_failed_deferred_query_leaves_every_split_none(self, db):
        import importlib

        qmod = importlib.import_module("genesis.observability.snapshots.queues")
        q = AsyncMock()
        q.count_pending_split.side_effect = RuntimeError("db unavailable")
        result = await qmod.queues(db, q, None)
        assert result["deferred_work"] is None
        assert result["deferred_recovery"] is None
        assert result["deferred_worklist"] is None  # never a preset 0
        assert result["deferred_worklist_stalled"] is None
        assert "deferred_work: query failed" in result["errors"]

async def _isolated_queue_alerts(db, monkeypatch, tmp_path):
    import importlib

    import genesis.mcp.health.errors as errors_mod
    import genesis.mcp.health_mcp as health_mcp_mod
    from genesis.runtime import GenesisRuntime

    home = tmp_path / "private-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_HOME", str(home / ".genesis"))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(GenesisRuntime, "peek", lambda: None)
    queues_mod = importlib.import_module("genesis.observability.snapshots.queues")
    depths = await queues_mod.queues(db, None, None)
    snap = {
        "queues": depths, "call_sites": {}, "services": {}, "cc_sessions": {},
        "infrastructure": {}, "scheduler": {}, "agent_api": {"status": "healthy"},
    }
    service = SimpleNamespace(_db=db, snapshot=AsyncMock(return_value=snap))
    monkeypatch.setattr(health_mcp_mod, "_service", service)
    monkeypatch.setattr(health_mcp_mod, "_activity_tracker", None)
    monkeypatch.setattr(health_mcp_mod, "_job_retry_registry", None)
    monkeypatch.setattr(health_mcp_mod, "_alert_history", {})
    return errors_mod, health_mcp_mod, snap


class TestUnknownQueueIncidents:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("other_firing", [False, True])
    async def test_preserve_is_not_insertion_or_a_healthy_sibling_hold(self, db, other_firing):
        from genesis.db.crud import alert_events
        from genesis.observability.agent_api_alerts import ALERT_ID

        held, cleared = "queue:dead_letters", "queue:deferred_recovery"
        active = [
            {"alert_id": aid, "source": "fixture", "severity": "WARNING", "message": aid}
            for aid in (held, cleared, ALERT_ID)
        ]
        await alert_events.reconcile_open_set(db, active=active, now=_NOW.isoformat())
        before = next(r for r in await alert_events.list_open(db) if r["alert_id"] == held)
        firing = active[:0]
        if other_firing:
            firing = [{"alert_id": "fixture:active", "source": "fixture", "severity": "WARNING", "message": "active"}]
        for _ in range(2):
            await alert_events.reconcile_open_set(
                db, active=firing, now=(_NOW + timedelta(seconds=10)).isoformat(),
                preserve_ids={held, "queue:never-open"}, resolution_reasons={ALERT_ID: "healthy"},
            )
            rows = {r["alert_id"]: dict(r) for r in await (await db.execute("SELECT * FROM alert_events")).fetchall()}
            assert rows[held]["resolved_at"] is None
            assert rows[cleared]["resolved_at"] is not None
            assert rows[ALERT_ID]["resolution_reason"] == "healthy"
            assert "queue:never-open" not in rows
            assert next(r for r in await alert_events.list_open(db) if r["alert_id"] == held) == before
        await alert_events.reconcile_open_set(db, active=[], now=_NOW.isoformat(), preserve_ids=set())
        assert not await alert_events.list_open(db)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["deferred_recovery", "dead_letters"])
    @pytest.mark.parametrize("unknown", ["wrapper", "section", "service"])
    async def test_unknown_preserves_existing_durable_incident(
        self, db, monkeypatch, tmp_path, field, unknown,
    ):
        from genesis.awareness.loop import _persist_health_alerts
        from genesis.db.crud import alert_events

        _errors, health, snap = await _isolated_queue_alerts(db, monkeypatch, tmp_path)
        sibling = "dead_letters" if field == "deferred_recovery" else "deferred_recovery"
        snap["queues"][sibling] = 0
        if unknown == "section":
            snap["queues"] = {"status": "error"}
        elif unknown == "service":
            monkeypatch.setattr(health, "_service", None)
        active = [
            {"alert_id": f"queue:{key}", "source": "queue", "severity": "WARNING", "message": key}
            for key in (field, sibling)
        ]
        await alert_events.reconcile_open_set(db, active=active, now=_NOW.isoformat())
        before = {r["alert_id"]: r for r in await alert_events.list_open(db)}
        for _ in range(2):
            await _persist_health_alerts(db)
            after = {r["alert_id"]: r for r in await alert_events.list_open(db)}
            assert f"queue:{field}" in after
            assert after[f"queue:{field}"] == before[f"queue:{field}"]
            if unknown == "wrapper":
                assert f"queue:{sibling}" not in after

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["deferred_recovery", "dead_letters"])
    async def test_unknown_does_not_render_or_forget_local_recovery(
        self, db, monkeypatch, tmp_path, field,
    ):
        errors, health, snap = await _isolated_queue_alerts(db, monkeypatch, tmp_path)
        aid = f"queue:{field}"
        health._alert_history = {aid: _NOW.isoformat()}
        for _ in range(2):
            alerts = await errors._impl_health_alerts(active_only=False)
            assert not any(a["id"] == aid and a["severity"] == "RESOLVED" for a in alerts)
            assert aid in health._alert_history
        snap["queues"][field] = 0
        alerts = await errors._impl_health_alerts(active_only=False)
        assert sum(a["id"] == aid and a["severity"] == "RESOLVED" for a in alerts) == 1
        assert aid not in health._alert_history

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["deferred_recovery", "dead_letters"])
    async def test_never_open_queue_does_not_invent_an_incident(
        self, db, monkeypatch, tmp_path, field,
    ):
        from genesis.awareness.loop import _persist_health_alerts
        from genesis.db.crud import alert_events

        errors, health, snap = await _isolated_queue_alerts(db, monkeypatch, tmp_path)
        aid = f"queue:{field}"
        for _ in range(2):
            await _persist_health_alerts(db)
            assert not any(r["alert_id"] == aid for r in await alert_events.list_open(db))
            assert not any(a["id"] == aid for a in await errors._impl_health_alerts(active_only=False))
            assert aid not in health._alert_history
        snap["queues"][field] = 0
        assert not any(a["id"] == aid for a in await errors._impl_health_alerts(active_only=False))


class TestCoherentSnapshot:
    @staticmethod
    def _assert_relations(result):
        total, recovery, worklist, stalled = (
            result[key] for key in (
                "deferred_work", "deferred_recovery", "deferred_worklist", "deferred_worklist_stalled",
            )
        )
        assert stalled <= recovery <= total
        assert stalled <= worklist <= total
        assert total == recovery + worklist - stalled

    @pytest.mark.asyncio
    @pytest.mark.parametrize("after_read", ["count_pending", "count_recovery_pending"])
    async def test_transition_between_old_reads_cannot_contradict_split(self, db, monkeypatch, after_read):
        import importlib

        await _seed_worklist(db, 1, age_days=STALE_WORKLIST_DAYS + 1)
        await db.execute("UPDATE deferred_work_queue SET status='processing'")
        await db.commit()
        queue = _queue(db)
        old_read = getattr(queue, after_read)

        async def transition(*args, **kwargs):
            count = await old_read(*args, **kwargs)
            await db.execute("UPDATE deferred_work_queue SET status='pending'")
            await db.commit()
            return count

        monkeypatch.setattr(queue, after_read, transition)
        queues_mod = importlib.import_module("genesis.observability.snapshots.queues")
        self._assert_relations(await queues_mod.queues(db, queue, None))

    @pytest.mark.asyncio
    async def test_crossing_cutoff_cannot_contradict_split(self, db):
        import importlib

        await _seed_worklist(db, 1, age_days=STALE_WORKLIST_DAYS)
        await db.execute(
            "UPDATE deferred_work_queue SET deferred_at=?",
            ((_NOW - timedelta(days=STALE_WORKLIST_DAYS) + timedelta(seconds=1)).isoformat(),),
        )
        await db.commit()
        readings = iter((_NOW, _NOW + timedelta(seconds=2)))
        queue = DeferredWorkQueue(db, clock=lambda: next(readings))
        queues_mod = importlib.import_module("genesis.observability.snapshots.queues")
        self._assert_relations(await queues_mod.queues(db, queue, None))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("work_type", sorted(BATCH_WORK_TYPES) + ["reflection"])
    @pytest.mark.parametrize("status", ["pending", "processing", "completed"])
    @pytest.mark.parametrize("offset", [-1, 0, 1])
    async def test_summary_membership_and_strict_boundary(self, db, work_type, status, offset):
        stamp = (_NOW - timedelta(days=STALE_WORKLIST_DAYS) + timedelta(seconds=offset)).isoformat()
        await crud.create(
            db, id="summary-matrix", work_type=work_type, priority=MEMORY_OPS,
            payload_json="{}", deferred_at=stamp, deferred_reason="fixture", created_at=stamp,
            staleness_policy=DRAIN,
        )
        await db.execute("UPDATE deferred_work_queue SET status=?", (status,))
        await db.commit()
        pending, batch, stale = status == "pending", work_type in BATCH_WORK_TYPES, offset < 0
        assert await _queue(db).count_pending_split() == {
            "total": int(pending), "recovery": int(pending and (not batch or stale)),
            "worklist": int(pending and batch), "stalled": int(pending and batch and stale),
        }

    @pytest.mark.asyncio
    async def test_summary_uses_one_statement_and_clock_read(self, db):
        await _seed_worklist(db, 2, age_days=STALE_WORKLIST_DAYS + 1)
        clock = Mock(return_value=_NOW)
        queue = DeferredWorkQueue(db, clock=clock)
        statements = []
        await db.set_trace_callback(statements.append)
        try:
            assert await queue.count_pending_split() == {
                "total": 2, "recovery": 2, "worklist": 2, "stalled": 2,
            }
        finally:
            await db.set_trace_callback(None)
        assert clock.call_count == 1
        assert len(statements) == 1 and statements[0].lstrip().startswith("WITH pending")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [0, 2])
    async def test_empty_batch_types_keep_nonbatch_recovery(self, db, count):
        await _seed_worklist(db, count)
        assert await crud.count_pending_split(
            db, work_types=frozenset(), stale_cutoff_iso=_NOW.isoformat(),
        ) == {"total": count, "recovery": count, "worklist": 0, "stalled": 0}


class TestDriftGuard:
    def test_batch_work_types_matches_dream_cycle(self):
        # If dream_cycle renames its worklist work_type, the exclusion silently
        # stops matching and the alarm regresses — pin them together.
        from genesis.memory.dream_cycle import WORKLIST_WORK_TYPE

        assert WORKLIST_WORK_TYPE in BATCH_WORK_TYPES

    def test_batch_work_types_includes_entity_adjudication(self):
        # The reconcile sweep parks deep-but-healthy backlogs; they must stay
        # excluded from the deferred_recovery depth alarm.
        from genesis.memory.entity_adjudication import WORK_TYPE

        assert WORK_TYPE in BATCH_WORK_TYPES


class TestAlarmSwap:
    """errors.py must threshold on `deferred_recovery`, never the raw
    `deferred_work` total."""

    async def _alerts_for_queues(self, monkeypatch, queues_dict: dict) -> list[dict]:
        import genesis.mcp.health.errors as errors_mod
        import genesis.mcp.health_mcp as health_mcp_mod

        snap = {
            "call_sites": {},
            "services": {},
            "queues": queues_dict,
            "cc_sessions": {},
            "infrastructure": {},
            "scheduler": {},
        }
        service = AsyncMock()
        service.snapshot = AsyncMock(return_value=snap)
        monkeypatch.setattr(health_mcp_mod, "_service", service)
        monkeypatch.setattr(health_mcp_mod, "_activity_tracker", None)
        monkeypatch.setattr(health_mcp_mod, "_job_retry_registry", None)
        monkeypatch.setattr(health_mcp_mod, "_alert_history", {})
        return await errors_mod._impl_health_alerts(active_only=True)

    @pytest.mark.asyncio
    async def test_raw_total_does_not_alarm(self, monkeypatch):
        # 400 raw / 5 recovery: the old behavior fired; the new behavior stays silent.
        alerts = await self._alerts_for_queues(
            monkeypatch, {"deferred_work": 400, "deferred_recovery": 5, "deferred_worklist": 395}
        )
        ids = {a["id"] for a in alerts}
        assert "queue:deferred_work" not in ids
        assert "queue:deferred_recovery" not in ids  # 5 <= 100

    @pytest.mark.asyncio
    async def test_recovery_backlog_alarms(self, monkeypatch):
        alerts = await self._alerts_for_queues(
            monkeypatch, {"deferred_work": 400, "deferred_recovery": 150, "deferred_worklist": 250}
        )
        ids = {a["id"] for a in alerts}
        assert "queue:deferred_recovery" in ids  # 150 > 100
        assert "queue:deferred_work" not in ids  # raw total never alarms
