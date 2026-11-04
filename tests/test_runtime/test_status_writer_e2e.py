"""Real status publication, consumer readback, and runtime teardown on scratch state."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.autonomy.watchdog import WatchdogChecker
from genesis.mcp.standalone_health import StandaloneHealthDataService
from genesis.resilience.state import ResilienceStateMachine
from genesis.resilience.status_writer import StatusFileWriter
from genesis.runtime import GenesisRuntime
from genesis.runtime.init.memory import run_status_writer_loop


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("bootstrapped", [False, True])
async def test_real_periodic_writer_drains_before_runtime_db_teardown(
    monkeypatch, tmp_path, bootstrapped,
):
    rt = GenesisRuntime()
    db = await aiosqlite.connect(":memory:")
    await db.execute("CREATE TABLE status_work (value INTEGER)")
    await db.execute("INSERT INTO status_work VALUES (1)")
    rt._db = db
    rt._bootstrapped = bootstrapped
    loop_thread = threading.get_ident()

    async def count():
        assert threading.get_ident() == loop_thread
        async with db.execute("SELECT COUNT(*) FROM status_work") as cursor:
            return (await cursor.fetchone())[0]

    writer = StatusFileWriter(
        state_machine=ResilienceStateMachine(), path=str(tmp_path / "status.json"),
        deferred_queue=SimpleNamespace(count_pending=count),
    )
    rt._status_writer = writer
    entered, release = threading.Event(), threading.Event()
    publish = StatusFileWriter._publish

    def slow_publish(path, payload):
        entered.set()
        assert release.wait(5)
        publish(path, payload)

    monkeypatch.setattr(StatusFileWriter, "_publish", staticmethod(slow_publish))
    rt._status_writer_task = asyncio.create_task(run_status_writer_loop(rt, interval_s=0.001))
    shutdown = None
    try:
        await until(entered.is_set)
        shutdown = asyncio.create_task(rt.shutdown())
        await until(lambda: rt._status_writer_task.done())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not shutdown.done(), "shutdown returned while publication was still active"
        async with db.execute("SELECT 1") as cursor:
            assert await cursor.fetchone() == (1,), "DB closed before writer drained"
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        else:
            await rt.shutdown()
        await db.close()
    assert rt._status_writer_task.done()
    assert writer._publication is None
    status = json.loads(writer._path.read_text())
    assert status["human_summary"] == "All systems normal. 1 items queued."
    assert status["queue_depths"]["deferred_work"] == 1
    with pytest.raises(RuntimeError, match="closed"):
        await writer.write()


async def test_periodic_publication_reaches_health_reader_and_watchdog(monkeypatch, tmp_path):
    # Exercise the real file-format consumers, without systemd probing, a full
    # server bootstrap, a live database, or watchdog actions against this host.
    monkeypatch.setattr("genesis.observability.service_status.collect_service_status", lambda: {})
    monkeypatch.setattr("genesis.mcp.standalone_health._load_mcp_crash_status", lambda: {})
    monkeypatch.setattr(WatchdogChecker, "_detect_target_service", lambda self: "test.service")
    path = tmp_path / "status.json"
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), path=str(path))
    await writer.write()  # same initial write performed by runtime memory init
    reader = StandaloneHealthDataService(status_path=path)
    checker = WatchdogChecker(
        status_file=path, state_file=tmp_path / "watchdog-state.json",
        secrets_path=tmp_path / "unused.env",
    )
    before = await reader.snapshot()
    assert before["resilience_state"]["cloud"] == "NORMAL"
    writer.set_extra_data("human_summary", "Updated through periodic loop.")
    rt = MagicMock()
    rt._status_writer = writer
    loop = asyncio.create_task(run_status_writer_loop(rt, interval_s=0.001))
    try:
        await until(lambda: rt.record_job_success.called)
        after = await reader.snapshot()
        assert after["human_summary"] == "Updated through periodic loop."
        status = checker._read_status()
        assert status["timestamp"] == after["timestamp"]
        assert checker._compute_staleness(status) is not None
        assert after["timestamp"] >= before["timestamp"]
    finally:
        loop.cancel()
        await loop
        await writer.aclose()
