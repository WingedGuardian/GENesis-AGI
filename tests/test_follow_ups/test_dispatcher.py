"""Tests for the follow-up dispatcher."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import follow_ups, surplus_tasks
from genesis.db.schema import INDEXES, TABLES
from genesis.follow_ups.dispatcher import FollowUpDispatcher
from genesis.surplus.queue import SurplusQueue


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for table in ("follow_ups", "surplus_tasks", "drive_weights"):
            await conn.execute(TABLES[table])
        for idx in INDEXES:
            if "follow_ups" in idx or "surplus_tasks" in idx:
                await conn.execute(idx)
        # Seed drive_weights so priority calculation works
        await conn.execute(
            "INSERT INTO drive_weights VALUES ('cooperation', 0.5, 0.5, 0.1, 0.5)"
        )
        await conn.commit()
        yield conn


@pytest.fixture
def queue(db):
    return SurplusQueue(db)


@pytest.fixture
def dispatcher(db, queue):
    return FollowUpDispatcher(db, queue)


class TestFollowUpDispatcher:
    async def test_dispatches_surplus_task(self, dispatcher, db):
        fid = await follow_ups.create(
            db, content="benchmark gemini",
            source="test", strategy="surplus_task",
        )

        summary = await dispatcher.run_cycle()
        assert summary["surplus_dispatched"] == 1

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["status"] in ("scheduled", "in_progress")
        assert fu["linked_task_id"] is not None

    async def test_dispatches_scheduled_due(self, dispatcher, db):
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        fid = await follow_ups.create(
            db, content="run eval",
            source="test", strategy="scheduled_task",
            scheduled_at=past,
        )

        summary = await dispatcher.run_cycle()
        assert summary["scheduled_dispatched"] == 1

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["linked_task_id"] is not None

    async def test_skips_future_scheduled(self, dispatcher, db):
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        await follow_ups.create(
            db, content="future task",
            source="test", strategy="scheduled_task",
            scheduled_at=future,
        )

        summary = await dispatcher.run_cycle()
        assert summary["scheduled_dispatched"] == 0

    async def test_tracks_completed_task(self, dispatcher, db):
        fid = await follow_ups.create(
            db, content="something",
            source="test", strategy="surplus_task",
        )

        # First cycle: dispatch
        await dispatcher.run_cycle()
        fu = await follow_ups.get_by_id(db, fid)
        task_id = fu["linked_task_id"]

        # Simulate surplus task completion
        await surplus_tasks.mark_running(db, task_id, started_at=datetime.now(UTC).isoformat())
        await surplus_tasks.mark_completed(db, task_id, completed_at=datetime.now(UTC).isoformat())

        # Second cycle: track
        summary = await dispatcher.run_cycle()
        assert summary["completions_tracked"] == 1

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["status"] == "completed"

    async def test_tracks_failed_task(self, dispatcher, db):
        fid = await follow_ups.create(
            db, content="something",
            source="test", strategy="surplus_task",
        )

        await dispatcher.run_cycle()
        fu = await follow_ups.get_by_id(db, fid)
        task_id = fu["linked_task_id"]

        # Simulate surplus task failure
        await surplus_tasks.mark_running(db, task_id, started_at=datetime.now(UTC).isoformat())
        await surplus_tasks.mark_failed(db, task_id, failure_reason="provider error")

        summary = await dispatcher.run_cycle()
        assert summary["failures_detected"] == 1

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["status"] == "failed"
        assert "provider error" in fu["blocked_reason"]

    async def test_ego_judgment_not_dispatched(self, dispatcher, db):
        await follow_ups.create(
            db, content="think about this",
            source="ego_cycle", strategy="ego_judgment",
        )

        summary = await dispatcher.run_cycle()
        assert summary["surplus_dispatched"] == 0
        assert summary["scheduled_dispatched"] == 0

    async def test_empty_cycle(self, dispatcher):
        summary = await dispatcher.run_cycle()
        assert all(v == 0 for v in summary.values())

    async def test_structured_reason_routes_model_eval(self, dispatcher, db):
        """Follow-up with structured reason routes to correct task type."""
        import json

        from genesis.surplus.types import TaskType

        payload = {
            "task_type": "model_eval",
            "compute_tier": "free_api",
            "payload": {
                "model_id": "test/new-model",
                "name": "Test Model",
                "source": "openrouter_free_scan",
            },
        }
        fid = await follow_ups.create(
            db,
            content="Benchmark new free model: test/new-model (Test Model)",
            source="recon_pipeline",
            strategy="surplus_task",
            reason=json.dumps(payload),
        )

        summary = await dispatcher.run_cycle()
        assert summary["surplus_dispatched"] == 1

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["linked_task_id"] is not None

        # Verify the surplus task has the right type
        task = await surplus_tasks.get_by_id(db, fu["linked_task_id"])
        assert task["task_type"] == str(TaskType.MODEL_EVAL)

        # Verify payload includes model info with source provenance
        task_payload = json.loads(task["payload"])
        assert task_payload["model_id"] == "test/new-model"
        assert task_payload["source"] == "follow_up"
        assert task_payload["original_source"] == "openrouter_free_scan"

    async def test_structured_reason_invalid_json_falls_back(self, dispatcher, db):
        """Malformed reason JSON falls back to keyword matching."""
        fid = await follow_ups.create(
            db,
            content="benchmark something",
            source="test",
            strategy="surplus_task",
            reason="{invalid json",
        )

        summary = await dispatcher.run_cycle()
        assert summary["surplus_dispatched"] == 1

        fu = await follow_ups.get_by_id(db, fid)
        task = await surplus_tasks.get_by_id(db, fu["linked_task_id"])
        # Falls back to keyword "benchmark" → BRAINSTORM_SELF (not MODEL_EVAL,
        # which requires model_id that keyword matching can't provide)
        from genesis.surplus.types import TaskType
        assert task["task_type"] == str(TaskType.BRAINSTORM_SELF)
