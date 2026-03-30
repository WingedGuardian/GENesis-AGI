"""Tests for Phase 6 real signal collectors."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.awareness.signals import SignalCollector
from genesis.db.schema import create_all_tables
from genesis.learning.signals import (
    BudgetCollector,
    CriticalFailureCollector,
    ErrorSpikeCollector,
    MemoryBacklogCollector,
    TaskQualityCollector,
)
from genesis.learning.signals.recon_findings import ReconFindingsCollector
from genesis.observability.types import ProbeResult, ProbeStatus


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        yield conn


# ── helpers ───────────────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _insert_cost_event(db, cost_usd: float, created_at: str | None = None):
    await db.execute(
        "INSERT INTO cost_events (id, event_type, cost_usd, created_at) VALUES (?, ?, ?, ?)",
        (_uid(), "llm_call", cost_usd, created_at or _now_iso()),
    )
    await db.commit()


async def _insert_observation(db, source: str = "error", created_at: str | None = None, retrieved_count: int = 0):
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, retrieved_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_uid(), source, "event", "test", "medium", retrieved_count, created_at or _now_iso()),
    )
    await db.commit()


async def _insert_trace(db, outcome_class: str, created_at: str | None = None):
    await db.execute(
        "INSERT INTO execution_traces (id, user_request, plan, sub_agents, outcome_class, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (_uid(), "test", "[]", "[]", outcome_class, created_at or _now_iso()),
    )
    await db.commit()


# ── Protocol conformance ─────────────────────────────────────────────────────

class TestProtocolConformance:
    def test_budget_is_signal_collector(self, db):
        # Can't use db fixture here directly but we can check structurally
        assert hasattr(BudgetCollector, "signal_name")
        assert hasattr(BudgetCollector, "collect")

    def test_all_collectors_are_runtime_checkable(self):
        """All 5 collectors satisfy SignalCollector protocol structurally."""
        # We just verify the class attributes exist; runtime_checkable
        # needs instances which need db — covered in functional tests.
        for cls in (BudgetCollector, ErrorSpikeCollector, TaskQualityCollector, MemoryBacklogCollector):
            assert hasattr(cls, "signal_name")
        assert hasattr(CriticalFailureCollector, "signal_name")


# ── BudgetCollector ──────────────────────────────────────────────────────────

class TestBudgetCollector:
    async def test_no_events_returns_zero(self, db):
        c = BudgetCollector(db)
        r = await c.collect()
        assert r.name == "budget_pct_consumed"
        assert r.value == 0.0
        assert 0.0 <= r.value <= 1.0

    async def test_partial_spend(self, db):
        await _insert_cost_event(db, 2.5)
        r = await BudgetCollector(db).collect()
        assert r.value == pytest.approx(0.5)

    async def test_over_budget_capped(self, db):
        await _insert_cost_event(db, 10.0)
        r = await BudgetCollector(db).collect()
        assert r.value == 1.0

    async def test_custom_daily_budget(self, db):
        await _insert_cost_event(db, 5.0)
        r = await BudgetCollector(db, daily_budget=10.0).collect()
        assert r.value == pytest.approx(0.5)

    async def test_old_events_excluded(self, db):
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        await _insert_cost_event(db, 5.0, created_at=yesterday)
        r = await BudgetCollector(db).collect()
        # Yesterday's events still have ISO prefix >= today's date? No — they have yesterday's date.
        # The query uses >= today's date string, so yesterday is excluded.
        assert r.value == 0.0

    async def test_source_field(self, db):
        r = await BudgetCollector(db).collect()
        assert r.source == "cost_events"

    async def test_isinstance_protocol(self, db):
        assert isinstance(BudgetCollector(db), SignalCollector)


# ── ErrorSpikeCollector ──────────────────────────────────────────────────────

class TestErrorSpikeCollector:
    async def test_no_events_returns_zero(self, db):
        r = await ErrorSpikeCollector(db).collect()
        assert r.name == "software_error_spike"
        assert r.value == 0.0

    async def test_spike_detected(self, db):
        # Insert 24 errors spread over 24h (1/hr baseline) then 10 in last hour
        now = datetime.now(UTC)
        for i in range(24):
            t = (now - timedelta(hours=23 - i)).isoformat()
            await _insert_observation(db, source="error", created_at=t)
        # Add 9 more in the last hour (total 10 in last hour, baseline=25/24≈1.04)
        for _ in range(9):
            await _insert_observation(db, source="error", created_at=now.isoformat())
        r = await ErrorSpikeCollector(db).collect()
        # hourly=10, baseline=33/24≈1.375, threshold=4.125, value=10/4.125≈2.42 → capped 1.0
        assert r.value > 0.5

    async def test_no_spike(self, db):
        # 1 error in last hour, 24 total — no spike
        now = datetime.now(UTC)
        for i in range(24):
            t = (now - timedelta(hours=23 - i)).isoformat()
            await _insert_observation(db, source="error", created_at=t)
        r = await ErrorSpikeCollector(db).collect()
        # hourly=1, baseline=24/24=1, threshold=3, value=1/3≈0.33
        assert r.value == pytest.approx(1 / 3, abs=0.05)

    async def test_non_error_observations_ignored(self, db):
        await _insert_observation(db, source="reflection")
        r = await ErrorSpikeCollector(db).collect()
        assert r.value == 0.0

    async def test_isinstance_protocol(self, db):
        assert isinstance(ErrorSpikeCollector(db), SignalCollector)


# ── CriticalFailureCollector ─────────────────────────────────────────────────

def _make_probe(status: ProbeStatus):
    async def probe():
        return ProbeResult(name="test", status=status, latency_ms=1.0)
    return probe


class TestCriticalFailureCollector:
    async def test_no_probes_returns_zero(self):
        r = await CriticalFailureCollector([]).collect()
        assert r.value == 0.0

    async def test_all_healthy(self):
        c = CriticalFailureCollector([_make_probe(ProbeStatus.HEALTHY)])
        r = await c.collect()
        assert r.value == 0.0

    async def test_degraded(self):
        c = CriticalFailureCollector([
            _make_probe(ProbeStatus.HEALTHY),
            _make_probe(ProbeStatus.DEGRADED),
        ])
        r = await c.collect()
        assert r.value == 0.5

    async def test_down_overrides_degraded(self):
        c = CriticalFailureCollector([
            _make_probe(ProbeStatus.DEGRADED),
            _make_probe(ProbeStatus.DOWN),
        ])
        r = await c.collect()
        assert r.value == 1.0

    async def test_signal_name(self):
        r = await CriticalFailureCollector([_make_probe(ProbeStatus.HEALTHY)]).collect()
        assert r.name == "critical_failure"

    async def test_isinstance_protocol(self):
        assert isinstance(CriticalFailureCollector([]), SignalCollector)


# ── TaskQualityCollector ─────────────────────────────────────────────────────

class TestTaskQualityCollector:
    async def test_no_traces_returns_zero(self, db):
        r = await TaskQualityCollector(db).collect()
        assert r.name == "task_completion_quality"
        assert r.value == 0.0

    async def test_all_success(self, db):
        for _ in range(5):
            await _insert_trace(db, "success")
        r = await TaskQualityCollector(db).collect()
        assert r.value == 0.0

    async def test_all_failures(self, db):
        for _ in range(3):
            await _insert_trace(db, "approach_failure")
        r = await TaskQualityCollector(db).collect()
        assert r.value == 1.0

    async def test_mixed(self, db):
        await _insert_trace(db, "success")
        await _insert_trace(db, "approach_failure")
        r = await TaskQualityCollector(db).collect()
        assert r.value == pytest.approx(0.5)

    async def test_workaround_success_not_failure(self, db):
        await _insert_trace(db, "workaround_success")
        r = await TaskQualityCollector(db).collect()
        assert r.value == 0.0

    async def test_old_traces_excluded(self, db):
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        await _insert_trace(db, "approach_failure", created_at=old)
        r = await TaskQualityCollector(db).collect()
        assert r.value == 0.0

    async def test_isinstance_protocol(self, db):
        assert isinstance(TaskQualityCollector(db), SignalCollector)


# ── MemoryBacklogCollector ───────────────────────────────────────────────────

class TestMemoryBacklogCollector:
    async def test_no_observations_returns_zero(self, db):
        r = await MemoryBacklogCollector(db).collect()
        assert r.name == "unprocessed_memory_backlog"
        assert r.value == 0.0

    async def test_some_unprocessed(self, db):
        for _ in range(50):
            await _insert_observation(db, source="reflection", retrieved_count=0)
        r = await MemoryBacklogCollector(db).collect()
        assert r.value == pytest.approx(0.5)

    async def test_ceiling_cap(self, db):
        for _ in range(150):
            await _insert_observation(db, source="reflection", retrieved_count=0)
        r = await MemoryBacklogCollector(db).collect()
        assert r.value == 1.0

    async def test_retrieved_excluded(self, db):
        for _ in range(50):
            await _insert_observation(db, source="reflection", retrieved_count=1)
        r = await MemoryBacklogCollector(db).collect()
        assert r.value == 0.0

    async def test_old_observations_excluded(self, db):
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        for _ in range(50):
            await _insert_observation(db, source="reflection", retrieved_count=0, created_at=old)
        r = await MemoryBacklogCollector(db).collect()
        assert r.value == 0.0

    async def test_isinstance_protocol(self, db):
        assert isinstance(MemoryBacklogCollector(db), SignalCollector)


# ── ReconFindingsCollector ───────────────────────────────────────────────────


async def _insert_recon_finding(db, resolved: int = 0):
    await db.execute(
        "INSERT INTO observations (id, source, type, category, content, priority, resolved, created_at) "
        "VALUES (?, 'recon', 'finding', 'github_landscape', 'test finding', 'medium', ?, ?)",
        (_uid(), resolved, _now_iso()),
    )
    await db.commit()


class TestReconFindingsCollector:
    async def test_no_findings_returns_zero(self, db):
        r = await ReconFindingsCollector(db).collect()
        assert r.name == "recon_findings_pending"
        assert r.value == 0.0

    async def test_some_findings(self, db):
        for _ in range(5):
            await _insert_recon_finding(db)
        r = await ReconFindingsCollector(db).collect()
        assert r.value == pytest.approx(0.5)

    async def test_ceiling_cap(self, db):
        for _ in range(15):
            await _insert_recon_finding(db)
        r = await ReconFindingsCollector(db).collect()
        assert r.value == 1.0

    async def test_resolved_excluded(self, db):
        for _ in range(5):
            await _insert_recon_finding(db, resolved=1)
        r = await ReconFindingsCollector(db).collect()
        assert r.value == 0.0

    async def test_isinstance_protocol(self, db):
        assert isinstance(ReconFindingsCollector(db), SignalCollector)


# ── PendingItemCollector ───────────────────────────────────────────────────


class TestPendingItemCollector:
    async def _insert_cognitive_state(self, db, content: str, days_ago: float = 0):
        created = datetime.now(UTC) - timedelta(days=days_ago)
        await db.execute(
            "INSERT INTO cognitive_state (id, section, content, created_at) VALUES (?, ?, ?, ?)",
            (_uid(), "active_context", content, created.isoformat()),
        )
        await db.commit()

    async def test_no_state_returns_zero(self, db):
        from genesis.learning.signals.pending_items import PendingItemCollector
        r = await PendingItemCollector(db).collect()
        assert r.name == "stale_pending_items"
        assert r.value == 0.0

    async def test_recent_items_return_zero(self, db):
        from genesis.learning.signals.pending_items import PendingItemCollector
        await self._insert_cognitive_state(db, "**Pending Actions**\n1. Fix something\n", days_ago=1)
        r = await PendingItemCollector(db).collect()
        assert r.value == 0.0

    async def test_stale_items_return_nonzero(self, db):
        from genesis.learning.signals.pending_items import PendingItemCollector
        await self._insert_cognitive_state(db, "**Pending Actions**\n1. Fix something\n2. Fix other\n", days_ago=5)
        r = await PendingItemCollector(db).collect()
        assert r.value > 0.0
        assert "2_items" in r.source
        assert "STALE" in r.source

    async def test_very_old_items_cap_at_one(self, db):
        from genesis.learning.signals.pending_items import PendingItemCollector
        await self._insert_cognitive_state(db, "**Pending Actions**\n1. Ancient bug\n", days_ago=30)
        r = await PendingItemCollector(db).collect()
        assert r.value == 1.0
