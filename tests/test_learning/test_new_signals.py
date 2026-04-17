"""Tests for the awareness scoring overhaul signal collectors.

Covers: LightCascadeCollector, SentinelActivityCollector,
        GuardianActivityCollector, SurplusActivityCollector,
        AutonomyActivityCollector, and SEED weight validation.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from genesis.awareness.signals import SignalCollector
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        yield conn


def _uid() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── LightCascadeCollector ──────────────────────────────────────────────


class TestLightCascadeCollector:
    async def test_no_ticks_returns_zero(self, db):
        from genesis.learning.signals.light_cascade import LightCascadeCollector

        r = await LightCascadeCollector(db).collect()
        assert r.name == "light_count_since_deep"
        assert r.value == 0.0

    async def test_one_light_tick(self, db):
        from genesis.learning.signals.light_cascade import LightCascadeCollector

        await db.execute(
            "INSERT INTO awareness_ticks (id, source, classified_depth, signals_json, scores_json, created_at) "
            "VALUES (?, 'scheduled', 'Light', '[]', '[]', ?)",
            (_uid(), _now_iso()),
        )
        await db.commit()
        r = await LightCascadeCollector(db).collect()
        assert r.value == pytest.approx(1 / 3, abs=0.01)

    async def test_three_light_ticks_caps_at_one(self, db):
        from genesis.learning.signals.light_cascade import LightCascadeCollector

        for _ in range(3):
            await db.execute(
                "INSERT INTO awareness_ticks (id, source, classified_depth, signals_json, scores_json, created_at) "
                "VALUES (?, 'scheduled', 'Light', '[]', '[]', ?)",
                (_uid(), _now_iso()),
            )
        await db.commit()
        r = await LightCascadeCollector(db).collect()
        assert r.value == 1.0

    async def test_deep_tick_resets_count(self, db):
        from genesis.learning.signals.light_cascade import LightCascadeCollector

        # Insert some Light ticks
        old = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
        for _ in range(5):
            await db.execute(
                "INSERT INTO awareness_ticks (id, source, classified_depth, signals_json, scores_json, created_at) "
                "VALUES (?, 'scheduled', 'Light', '[]', '[]', ?)",
                (_uid(), old),
            )
        # Insert a Deep tick after them
        await db.execute(
            "INSERT INTO awareness_ticks (id, source, classified_depth, signals_json, scores_json, created_at) "
            "VALUES (?, 'scheduled', 'Deep', '[]', '[]', ?)",
            (_uid(), _now_iso()),
        )
        await db.commit()
        r = await LightCascadeCollector(db).collect()
        # No Light ticks after the Deep tick
        assert r.value == 0.0

    async def test_isinstance_protocol(self, db):
        from genesis.learning.signals.light_cascade import LightCascadeCollector

        assert isinstance(LightCascadeCollector(db), SignalCollector)


# ── SentinelActivityCollector ──────────────────────────────────────────


class TestSentinelActivityCollector:
    def _write_state(self, tmp_path: Path, state: str) -> Path:
        state_path = tmp_path / "sentinel_state.json"
        state_path.write_text(json.dumps({
            "current_state": state,
            "entered_at": _now_iso(),
        }))
        return state_path

    async def test_healthy_returns_zero(self, tmp_path):
        from genesis.learning.signals.sentinel_activity import SentinelActivityCollector

        path = self._write_state(tmp_path, "healthy")
        r = await SentinelActivityCollector(state_path=path).collect()
        assert r.name == "sentinel_activity"
        assert r.value == 0.0

    async def test_investigating_returns_03(self, tmp_path):
        from genesis.learning.signals.sentinel_activity import SentinelActivityCollector

        path = self._write_state(tmp_path, "investigating")
        r = await SentinelActivityCollector(state_path=path).collect()
        assert r.value == 0.3

    async def test_remediating_returns_07(self, tmp_path):
        from genesis.learning.signals.sentinel_activity import SentinelActivityCollector

        path = self._write_state(tmp_path, "remediating")
        r = await SentinelActivityCollector(state_path=path).collect()
        assert r.value == 0.7

    async def test_escalated_returns_10(self, tmp_path):
        from genesis.learning.signals.sentinel_activity import SentinelActivityCollector

        path = self._write_state(tmp_path, "escalated")
        r = await SentinelActivityCollector(state_path=path).collect()
        assert r.value == 1.0

    async def test_missing_file_returns_zero(self, tmp_path):
        from genesis.learning.signals.sentinel_activity import SentinelActivityCollector

        path = tmp_path / "nonexistent.json"
        r = await SentinelActivityCollector(state_path=path).collect()
        assert r.value == 0.0  # load_state returns HEALTHY default

    async def test_isinstance_protocol(self, tmp_path):
        from genesis.learning.signals.sentinel_activity import SentinelActivityCollector

        assert isinstance(SentinelActivityCollector(), SignalCollector)


# ── GuardianActivityCollector ──────────────────────────────────────────


class TestGuardianActivityCollector:
    async def test_fresh_heartbeat(self, tmp_path):
        from genesis.learning.signals.guardian_activity import GuardianActivityCollector

        hb = tmp_path / "guardian_heartbeat.json"
        hb.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": datetime.now(UTC).isoformat(),
            "uptime_s": 100,
        }))
        r = await GuardianActivityCollector(heartbeat_path=hb).collect()
        assert r.name == "guardian_activity"
        assert r.value == 0.0

    async def test_stale_heartbeat(self, tmp_path):
        from genesis.learning.signals.guardian_activity import GuardianActivityCollector

        hb = tmp_path / "guardian_heartbeat.json"
        stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        hb.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": stale,
            "uptime_s": 600,
        }))
        r = await GuardianActivityCollector(heartbeat_path=hb).collect()
        assert r.value == 0.5

    async def test_very_stale_heartbeat(self, tmp_path):
        from genesis.learning.signals.guardian_activity import GuardianActivityCollector

        hb = tmp_path / "guardian_heartbeat.json"
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        hb.write_text(json.dumps({
            "guardian_alive": True,
            "timestamp": old,
            "uptime_s": 7200,
        }))
        r = await GuardianActivityCollector(heartbeat_path=hb).collect()
        assert r.value == 1.0

    async def test_missing_file_returns_zero(self, tmp_path):
        from genesis.learning.signals.guardian_activity import GuardianActivityCollector

        hb = tmp_path / "nonexistent.json"
        r = await GuardianActivityCollector(heartbeat_path=hb).collect()
        assert r.value == 0.0

    async def test_corrupt_json_returns_05(self, tmp_path):
        from genesis.learning.signals.guardian_activity import GuardianActivityCollector

        hb = tmp_path / "guardian_heartbeat.json"
        hb.write_text("not valid json{{{")
        r = await GuardianActivityCollector(heartbeat_path=hb).collect()
        assert r.value == 0.5

    async def test_isinstance_protocol(self, tmp_path):
        from genesis.learning.signals.guardian_activity import GuardianActivityCollector

        assert isinstance(GuardianActivityCollector(), SignalCollector)


# ── SurplusActivityCollector ───────────────────────────────────────────


class TestSurplusActivityCollector:
    async def _insert_task(self, db, status: str, *, hours_ago: float = 0, started_hours_ago: float | None = None):
        created = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
        started = (datetime.now(UTC) - timedelta(hours=started_hours_ago)).isoformat() if started_hours_ago is not None else None
        await db.execute(
            "INSERT INTO surplus_tasks (id, task_type, compute_tier, priority, drive_alignment, "
            "status, created_at, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_uid(), "brainstorm", "micro", 0.5, "curiosity", status, created, started),
        )
        await db.commit()

    async def test_idle_returns_zero(self, db):
        from genesis.learning.signals.surplus_activity import SurplusActivityCollector

        r = await SurplusActivityCollector(db).collect()
        assert r.name == "surplus_activity"
        assert r.value == 0.0

    async def test_healthy_tasks(self, db):
        from genesis.learning.signals.surplus_activity import SurplusActivityCollector

        for _ in range(8):
            await self._insert_task(db, "completed")
        await self._insert_task(db, "failed")
        r = await SurplusActivityCollector(db).collect()
        # 1/9 = 11% failure < 20% → healthy
        assert r.value == 0.0

    async def test_concerning_failure_rate(self, db):
        from genesis.learning.signals.surplus_activity import SurplusActivityCollector

        for _ in range(6):
            await self._insert_task(db, "completed")
        for _ in range(3):
            await self._insert_task(db, "failed")
        r = await SurplusActivityCollector(db).collect()
        # 3/9 = 33% → concerning
        assert r.value == 0.5

    async def test_broken_failure_rate(self, db):
        from genesis.learning.signals.surplus_activity import SurplusActivityCollector

        await self._insert_task(db, "completed")
        for _ in range(3):
            await self._insert_task(db, "failed")
        r = await SurplusActivityCollector(db).collect()
        # 3/4 = 75% → broken
        assert r.value == 1.0

    async def test_stuck_tasks(self, db):
        from genesis.learning.signals.surplus_activity import SurplusActivityCollector

        await self._insert_task(db, "running", started_hours_ago=3)
        r = await SurplusActivityCollector(db).collect()
        assert r.value == 0.8

    async def test_old_tasks_excluded(self, db):
        from genesis.learning.signals.surplus_activity import SurplusActivityCollector

        # 48h old completed tasks — outside 24h window
        for _ in range(5):
            await self._insert_task(db, "failed", hours_ago=48)
        r = await SurplusActivityCollector(db).collect()
        assert r.value == 0.0

    async def test_isinstance_protocol(self, db):
        from genesis.learning.signals.surplus_activity import SurplusActivityCollector

        assert isinstance(SurplusActivityCollector(db), SignalCollector)


# ── AutonomyActivityCollector ──────────────────────────────────────────


class TestAutonomyActivityCollector:
    async def _insert_autonomy_state(
        self, db, category: str, *,
        current_level: int = 4,
        earned_level: int = 4,
        consecutive_corrections: int = 0,
    ):
        await db.execute(
            "INSERT INTO autonomy_state (id, category, current_level, earned_level, "
            "consecutive_corrections, total_successes, total_corrections, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 10, 0, ?)",
            (_uid(), category, current_level, earned_level, consecutive_corrections, _now_iso()),
        )
        await db.commit()

    async def test_no_categories_returns_zero(self, db):
        from genesis.learning.signals.autonomy_activity import AutonomyActivityCollector

        r = await AutonomyActivityCollector(db).collect()
        assert r.name == "autonomy_activity"
        assert r.value == 0.0

    async def test_stable_returns_zero(self, db):
        from genesis.learning.signals.autonomy_activity import AutonomyActivityCollector

        await self._insert_autonomy_state(db, "background_cognitive")
        r = await AutonomyActivityCollector(db).collect()
        assert r.value == 0.0

    async def test_corrections_accumulating(self, db):
        from genesis.learning.signals.autonomy_activity import AutonomyActivityCollector

        await self._insert_autonomy_state(db, "background_cognitive", consecutive_corrections=1)
        r = await AutonomyActivityCollector(db).collect()
        assert r.value == 0.3

    async def test_near_regression(self, db):
        from genesis.learning.signals.autonomy_activity import AutonomyActivityCollector

        # _REGRESSION_THRESHOLD is 3, so threshold-1 = 2
        await self._insert_autonomy_state(db, "background_cognitive", consecutive_corrections=2)
        r = await AutonomyActivityCollector(db).collect()
        assert r.value == 0.7

    async def test_active_regression(self, db):
        from genesis.learning.signals.autonomy_activity import AutonomyActivityCollector

        await self._insert_autonomy_state(
            db, "background_cognitive",
            current_level=2, earned_level=4,
        )
        r = await AutonomyActivityCollector(db).collect()
        assert r.value == 1.0

    async def test_worst_category_wins(self, db):
        from genesis.learning.signals.autonomy_activity import AutonomyActivityCollector

        await self._insert_autonomy_state(db, "background_cognitive")  # stable
        await self._insert_autonomy_state(
            db, "outreach",
            current_level=2, earned_level=4,
        )  # regression
        r = await AutonomyActivityCollector(db).collect()
        assert r.value == 1.0

    async def test_isinstance_protocol(self, db):
        from genesis.learning.signals.autonomy_activity import AutonomyActivityCollector

        assert isinstance(AutonomyActivityCollector(db), SignalCollector)


# ── SEED Weight Validation ─────────────────────────────────────────────


class TestSeedWeights:
    def test_critical_failure_micro_only(self):
        from genesis.db.schema._tables import SIGNAL_WEIGHTS_SEED

        for row in SIGNAL_WEIGHTS_SEED:
            if row[0] == "critical_failure":
                assert json.loads(row[6]) == ["Micro"]
                assert row[2] == 0.70  # current_weight
                break
        else:
            pytest.fail("critical_failure not in SEED")

    def test_software_error_spike_micro_only(self):
        from genesis.db.schema._tables import SIGNAL_WEIGHTS_SEED

        for row in SIGNAL_WEIGHTS_SEED:
            if row[0] == "software_error_spike":
                assert json.loads(row[6]) == ["Micro"]
                break
        else:
            pytest.fail("software_error_spike not in SEED")

    def test_cc_version_changed_micro_only(self):
        from genesis.db.schema._tables import SIGNAL_WEIGHTS_SEED

        for row in SIGNAL_WEIGHTS_SEED:
            if row[0] == "cc_version_changed":
                assert json.loads(row[6]) == ["Micro"]
                assert row[2] == 0.50
                break
        else:
            pytest.fail("cc_version_changed not in SEED")

    def test_new_signals_present(self):
        from genesis.db.schema._tables import SIGNAL_WEIGHTS_SEED

        names = {row[0] for row in SIGNAL_WEIGHTS_SEED}
        expected = {
            "light_count_since_deep",
            "sentinel_activity",
            "guardian_activity",
            "surplus_activity",
            "autonomy_activity",
            "stale_pending_items",
        }
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_light_count_feeds_deep(self):
        from genesis.db.schema._tables import SIGNAL_WEIGHTS_SEED

        for row in SIGNAL_WEIGHTS_SEED:
            if row[0] == "light_count_since_deep":
                assert json.loads(row[6]) == ["Deep"]
                break
        else:
            pytest.fail("light_count_since_deep not in SEED")


# ── Citation Formatting ────────────────────────────────────────────────


class TestCitationFormatting:
    def test_format_observations_grouped_basic(self):
        from genesis.perception.context import _format_observations_grouped

        obs = [
            {"id": "abc12345-full-id", "source": "sentinel", "type": "alert",
             "priority": "high", "content": "circuit breaker open",
             "created_at": _now_iso()},
            {"id": "def67890-full-id", "source": "recon", "type": "finding",
             "priority": "medium", "content": "SDK update",
             "created_at": _now_iso()},
        ]
        lines = _format_observations_grouped(obs)
        text = "\n".join(lines)
        assert "### Sentinel" in text
        assert "### Recon" in text
        assert "[#abc12345]" in text
        assert "[#def67890]" in text

    def test_format_observations_grouped_empty(self):
        from genesis.perception.context import _format_observations_grouped

        assert _format_observations_grouped([]) == []

    def test_unknown_source_goes_to_other(self):
        from genesis.perception.context import _format_observations_grouped

        obs = [
            {"id": "xyz", "source": "unknown_source", "type": "event",
             "priority": "low", "content": "test",
             "created_at": _now_iso()},
        ]
        lines = _format_observations_grouped(obs)
        text = "\n".join(lines)
        assert "### Other" in text

    def test_prompts_format_observations_grouped(self):
        from genesis.cc.reflection_bridge._prompts import _format_observations_grouped

        obs = [
            {"id": "abc12345-full", "source": "sentinel", "type": "alert",
             "priority": "high", "content": "test",
             "created_at": _now_iso()},
        ]
        result = _format_observations_grouped(obs)
        assert "### Sentinel" in result
        assert "[#abc12345]" in result
