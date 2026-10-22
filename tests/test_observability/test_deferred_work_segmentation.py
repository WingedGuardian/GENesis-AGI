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
from unittest.mock import AsyncMock

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
        assert await q.count_stalled_worklist_pending() == 2  # counted directly

    @pytest.mark.asyncio
    async def test_stalled_worklist_ignores_recovery_items(self, db):
        await _seed_recovery(db, 4)
        await _seed_worklist(db, 3, age_days=STALE_WORKLIST_DAYS + 1)
        assert await _queue(db).count_stalled_worklist_pending() == 3


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
    @pytest.mark.parametrize("failed", [
        "count_pending", "count_recovery_pending", "count_worklist_pending",
    ])
    async def test_failed_deferred_query_leaves_every_split_none(self, db, failed):
        import importlib

        qmod = importlib.import_module("genesis.observability.snapshots.queues")
        q = AsyncMock()
        q.count_pending.return_value = 9
        q.count_recovery_pending.return_value = 4
        q.count_worklist_pending.return_value = 5
        getattr(q, failed).side_effect = RuntimeError("db unavailable")
        q.count_stalled_worklist_pending.return_value = 2  # its own try
        result = await qmod.queues(db, q, None)
        assert result["deferred_work"] is None
        assert result["deferred_recovery"] is None
        assert result["deferred_worklist"] is None  # never a preset 0
        assert result["deferred_worklist_stalled"] == 2
        assert "deferred_work: query failed" in result["errors"]

    @pytest.mark.asyncio
    async def test_failed_stalled_count_keeps_the_good_figures(self, db):
        import importlib

        qmod = importlib.import_module("genesis.observability.snapshots.queues")
        q = AsyncMock()
        q.count_pending.return_value = 9
        q.count_recovery_pending.return_value = 4
        q.count_worklist_pending.return_value = 5
        q.count_stalled_worklist_pending.side_effect = RuntimeError("db unavailable")
        result = await qmod.queues(db, q, None)
        assert (result["deferred_work"], result["deferred_recovery"]) == (9, 4)
        assert result["deferred_worklist"] == 5
        assert result["deferred_worklist_stalled"] is None
        assert any("stalled" in e for e in result["errors"])


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
