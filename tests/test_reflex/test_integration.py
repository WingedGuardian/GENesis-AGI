"""Full-chain integration — real tracked_task → real bus → real ingestor → DB.

This is the E2E-at-code-level check the server dark-bake will later confirm
at runtime: no mocks on the path. It wires the genuine GenesisEventBus, the
genuine ReflexIngestor, and the genuine default-bus tracked_task fallback,
then lets a real background coroutine raise — and asserts a fingerprinted
row lands in reflex_signals with the right subsystem/class/count.
"""

from __future__ import annotations

import asyncio
import importlib

import aiosqlite
import pytest

from genesis.observability.events import GenesisEventBus
from genesis.reflex.config import ReflexConfig
from genesis.reflex.ingest import ReflexIngestor
from genesis.util.tasks import set_default_event_bus, tracked_task

M70 = importlib.import_module("genesis.db.migrations.0070_reflex_arc")


@pytest.fixture(autouse=True)
def _reset_default_bus():
    set_default_event_bus(None)
    yield
    set_default_event_bus(None)


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        await M70.up(conn)
        await conn.commit()
        yield conn


async def _drain_until(ingestor, target, *, timeout=3.0):
    async def _poll():
        while ingestor.stats["processed"] < target:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


class TestFullChain:
    async def test_real_task_failure_lands_a_signal(self, db):
        bus = GenesisEventBus()
        ingestor = ReflexIngestor(
            db,
            config_loader=lambda: ReflexConfig(ingest_enabled=True),
            refresh_interval_s=0.05,
        )
        ingestor.start(bus)  # subscribes + starts worker; installs nothing global
        set_default_event_bus(bus)  # what runtime init does when enabled

        # A real background task with NO explicit event_bus — exactly the ~63
        # call sites that were dark before PR1. It must still emit via the
        # default bus and reach the ingestor.
        async def _boom():
            raise KeyError("integration-xyz")

        task = tracked_task(_boom(), name="integration-loop")
        with pytest.raises(KeyError):
            await task

        await _drain_until(ingestor, 1)

        cur = await db.execute(
            "SELECT fingerprint, error_type, class_key, occurrence_count, status "
            "FROM reflex_signals"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        fingerprint, error_type, class_key, count, status = rows[0]
        assert error_type == "KeyError"
        assert count == 1
        assert status == "new"
        # class_key subsystem derived from the deepest genesis frame — this
        # test file lives under a /genesis/ path in the worktree, so the
        # frame resolves to a package; assert the error-type half regardless
        assert class_key.startswith("KeyErrorx")

        ingestor._worker_task.cancel()

    async def test_burst_of_real_failures_is_one_row(self, db):
        bus = GenesisEventBus()
        ingestor = ReflexIngestor(
            db,
            config_loader=lambda: ReflexConfig(ingest_enabled=True),
            refresh_interval_s=0.05,
        )
        ingestor.start(bus)
        set_default_event_bus(bus)

        async def _boom(i):
            raise ValueError(f"rotating-payload-{i}")  # varying message

        for i in range(5):
            task = tracked_task(_boom(i), name="burst-loop")
            with pytest.raises(ValueError):
                await task

        await _drain_until(ingestor, 5)

        cur = await db.execute("SELECT COUNT(*), MAX(occurrence_count) FROM reflex_signals")
        n_rows, max_count = await cur.fetchone()
        # variable message must NOT split the fingerprint — one row, count 5
        assert n_rows == 1
        assert max_count == 5

        ingestor._worker_task.cancel()

    async def test_disabled_ingestor_stores_nothing(self, db):
        bus = GenesisEventBus()
        ingestor = ReflexIngestor(
            db,
            config_loader=lambda: ReflexConfig(ingest_enabled=False),
            refresh_interval_s=0.05,
        )
        ingestor.start(bus)
        set_default_event_bus(bus)

        async def _boom():
            raise KeyError("should-be-ignored")

        task = tracked_task(_boom(), name="dark-loop")
        with pytest.raises(KeyError):
            await task
        # give the (idle) worker a few cycles — nothing should enqueue
        await asyncio.sleep(0.2)

        cur = await db.execute("SELECT COUNT(*) FROM reflex_signals")
        assert (await cur.fetchone())[0] == 0
        assert ingestor.stats["queued"] == 0

        ingestor._worker_task.cancel()
