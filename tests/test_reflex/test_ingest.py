"""Tests for ReflexIngestor — filtering, enqueue gating, drain, containment."""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import aiosqlite
import pytest

import genesis.util.tasks as tasks_mod
from genesis.db.crud import reflex_signals as crud
from genesis.reflex.config import ReflexConfig
from genesis.reflex.ingest import ReflexIngestor

M70 = importlib.import_module("genesis.db.migrations.0070_reflex_arc")


class _FakeBus:
    """Minimal bus: records the subscribe call, satisfies tracked_task."""

    def __init__(self):
        self.subscribed = None

    def subscribe(self, listener, *, min_severity=None):
        self.subscribed = listener


@pytest.fixture(autouse=True)
def _reset_default_bus():
    tasks_mod.set_default_event_bus(None)
    yield
    tasks_mod.set_default_event_bus(None)


FIXED_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        await M70.up(conn)
        await conn.commit()
        yield conn


def _ingestor(db, *, enabled=True, **kwargs):
    ing = ReflexIngestor(
        db,
        config_loader=lambda: ReflexConfig(ingest_enabled=enabled),
        clock=lambda: FIXED_NOW,
        **kwargs,
    )
    ing.refresh_enabled()
    return ing


def _event(event_type="task.failed", subsystem="health", **details):
    base = {
        "task_name": "mem-sync",
        "error": "KeyError: 'x'",
        "error_type": "KeyError",
        "error_frames": ["memory/sync.py:_apply", "memory/store.py:get"],
    }
    base.update(details)
    return SimpleNamespace(event_type=event_type, subsystem=subsystem, details=base)


class TestHandler:
    async def test_enqueues_task_failed(self, db):
        ing = _ingestor(db)
        await ing.handle_event(_event())
        assert ing.stats["queued"] == 1

    async def test_ignores_other_event_types(self, db):
        ing = _ingestor(db)
        await ing.handle_event(_event(event_type="breaker.tripped"))
        await ing.handle_event(_event(event_type="heartbeat"))
        assert ing.stats["queued"] == 0

    async def test_disabled_does_not_enqueue(self, db):
        ing = _ingestor(db, enabled=False)
        await ing.handle_event(_event())
        assert ing.stats["queued"] == 0

    async def test_queue_full_drops_counted_no_raise(self, db):
        ing = _ingestor(db, queue_size=2)
        for _ in range(5):
            await ing.handle_event(_event())
        assert ing.stats["queued"] == 2
        assert ing.stats["dropped"] == 3

    async def test_malformed_event_never_raises(self, db):
        ing = _ingestor(db)
        await ing.handle_event(SimpleNamespace(event_type="task.failed", details=None))
        await ing.handle_event(SimpleNamespace(event_type="task.failed"))  # no details attr
        # handler swallowed everything; the well-formed-enough ones enqueued
        assert ing.stats["dropped"] == 0


class TestProcess:
    async def test_process_upserts_row(self, db):
        ing = _ingestor(db)
        row = await ing.process(
            {
                "task_name": "mem-sync",
                "error": "KeyError: 'x'",
                "error_type": "KeyError",
                "error_frames": ["memory/sync.py:_apply", "memory/store.py:get"],
                "subsystem": "health",
            }
        )
        assert row["status"] == "new"
        assert row["occurrence_count"] == 1
        # subsystem derived from deepest frame, NOT the event's HEALTH default
        assert row["subsystem"] == "memory"
        assert row["class_key"] == "KeyErrorxmemory"
        assert row["traceback_tail"] == "memory/sync.py:_apply>memory/store.py:get"

    async def test_burst_collapses_to_one_row(self, db):
        ing = _ingestor(db)
        item = {
            "task_name": "mem-sync",
            "error": "KeyError: 'x'",
            "error_type": "KeyError",
            "error_frames": ["memory/sync.py:_apply"],
            "subsystem": "health",
        }
        for _ in range(7):
            row = await ing.process(item)
        assert row["occurrence_count"] == 7
        cur = await db.execute("SELECT COUNT(*) FROM reflex_signals")
        assert (await cur.fetchone())[0] == 1

    async def test_empty_frames_falls_back_to_event_subsystem(self, db):
        ing = _ingestor(db)
        row = await ing.process(
            {
                "task_name": "old-proc-task",
                "error": "boom",
                "error_type": "ValueError",
                "error_frames": [],
                "subsystem": "awareness",
            }
        )
        assert row["subsystem"] == "awareness"
        assert row["traceback_tail"] is None

    async def test_recurrence_of_terminal_signal_reopens(self, db):
        ing = _ingestor(db)
        item = {
            "task_name": "mem-sync",
            "error": "KeyError: 'x'",
            "error_type": "KeyError",
            "error_frames": ["memory/sync.py:_apply"],
            "subsystem": "health",
        }
        row = await ing.process(item)
        await crud.set_status(
            db,
            signal_id=row["id"],
            expected_from="new",
            to="merged",
            now=FIXED_NOW.isoformat(),
        )
        row = await ing.process(item)
        assert row["status"] == "new"
        assert row["reopen_count"] == 1
        assert row["occurrence_count"] == 2


class TestWorker:
    async def test_worker_drains_queue_end_to_end(self, db):
        ing = _ingestor(db, refresh_interval_s=0.05)
        await ing.handle_event(_event())
        await ing.handle_event(_event(task_name="other-task"))
        worker = asyncio.create_task(ing._worker())
        try:
            for _ in range(100):
                if ing.stats["processed"] >= 2:
                    break
                await asyncio.sleep(0.01)
            assert ing.stats["processed"] == 2
            cur = await db.execute("SELECT COUNT(*) FROM reflex_signals")
            assert (await cur.fetchone())[0] == 2
        finally:
            worker.cancel()

    async def test_worker_survives_poison_item(self, db):
        ing = _ingestor(db, refresh_interval_s=0.05)
        ing._queue.put_nowait({"bad": "payload"})  # missing required keys
        await ing.handle_event(_event())
        worker = asyncio.create_task(ing._worker())
        try:
            for _ in range(100):
                if ing.stats["processed"] >= 1:
                    break
                await asyncio.sleep(0.01)
            # the good item after the poison one still processed
            assert ing.stats["processed"] == 1
        finally:
            worker.cancel()

    async def test_worker_refresh_disables_live(self, db):
        flag = {"enabled": True}
        ing = ReflexIngestor(
            db,
            config_loader=lambda: ReflexConfig(ingest_enabled=flag["enabled"]),
            clock=lambda: FIXED_NOW,
            refresh_interval_s=0.02,
        )
        ing.refresh_enabled()
        assert ing.stats["enabled"] is True
        flag["enabled"] = False
        worker = asyncio.create_task(ing._worker())
        try:
            for _ in range(100):
                if ing.stats["enabled"] is False:
                    break
                await asyncio.sleep(0.01)
            assert ing.stats["enabled"] is False
            # and the handler now refuses new events
            await ing.handle_event(_event())
            assert ing.stats["queued"] == 0
        finally:
            worker.cancel()


class TestDefaultBusLifecycle:
    """start() installs the default bus when enabled; live-disable and stop()
    unwind it so tracked_task stops emitting at the source."""

    async def test_start_enabled_installs_default_bus(self, db):
        bus = _FakeBus()
        ing = ReflexIngestor(
            db, config_loader=lambda: ReflexConfig(ingest_enabled=True), clock=lambda: FIXED_NOW
        )
        ing.start(bus)
        try:
            assert tasks_mod._default_event_bus is bus
        finally:
            ing._worker_task.cancel()

    async def test_start_disabled_leaves_default_bus_clear(self, db):
        bus = _FakeBus()
        ing = ReflexIngestor(
            db, config_loader=lambda: ReflexConfig(ingest_enabled=False), clock=lambda: FIXED_NOW
        )
        ing.start(bus)
        try:
            assert tasks_mod._default_event_bus is None
        finally:
            ing._worker_task.cancel()

    async def test_live_disable_clears_default_bus(self, db):
        flag = {"enabled": True}
        bus = _FakeBus()
        ing = ReflexIngestor(
            db,
            config_loader=lambda: ReflexConfig(ingest_enabled=flag["enabled"]),
            clock=lambda: FIXED_NOW,
            refresh_interval_s=0.02,
        )
        ing.start(bus)
        try:
            assert tasks_mod._default_event_bus is bus
            flag["enabled"] = False
            for _ in range(100):
                if tasks_mod._default_event_bus is None:
                    break
                await asyncio.sleep(0.01)
            # kill switch fully unwound: emission stops at the source
            assert tasks_mod._default_event_bus is None
        finally:
            ing._worker_task.cancel()

    async def test_stop_cancels_worker_and_clears_bus(self, db):
        bus = _FakeBus()
        ing = ReflexIngestor(
            db, config_loader=lambda: ReflexConfig(ingest_enabled=True), clock=lambda: FIXED_NOW
        )
        ing.start(bus)
        assert tasks_mod._default_event_bus is bus
        await ing.stop()
        assert tasks_mod._default_event_bus is None
        assert ing._worker_task.cancelled() or ing._worker_task.done()

    async def test_unit_ingestor_without_start_does_not_touch_global(self, db):
        # tests that drive process()/handle_event directly must never mutate
        # the process-global default bus (isolation guard)
        ing = _ingestor(db, enabled=True)
        assert tasks_mod._default_event_bus is None
        await ing.handle_event(_event())
        assert tasks_mod._default_event_bus is None
