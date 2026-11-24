"""Registration coverage for the genesis.db drip-table retention jobs (D3).

Locks that ``_wire_drip_retention_jobs`` actually registers every prune job — if an
``add_job`` call were dropped or a job id changed, this fails (the crud prunes themselves
are covered separately). Uses a real AsyncIOScheduler + a stub runtime; no full runtime init.
"""

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from genesis.runtime.init.learning import _wire_drip_retention_jobs

pytestmark = pytest.mark.asyncio

_EXPECTED = (
    "execution_traces_prune",
    "cost_events_prune",
    "file_modifications_prune",
    "job_run_events_prune",
    "alert_events_prune",
    "deferred_work_prune",
    "graduation_events_prune",
    "recall_traces_prune",
    "graph_traverse_prune",
    "events_prune",
    "voice_hygiene",
    "tool_call_outcomes_prune",
)


class _StubRT:
    _db = None

    def record_job_success(self, *_a):
        pass

    def record_job_failure(self, *_a, **_kw):
        pass


async def test_wire_drip_retention_jobs_registers_all():
    sched = AsyncIOScheduler()
    _wire_drip_retention_jobs(sched, _StubRT())
    sched.start(paused=True)  # flush pending jobs into the jobstore without firing them
    try:
        for job_id in _EXPECTED:
            job = sched.get_job(job_id)
            assert job is not None, f"{job_id} not registered"
            assert isinstance(job.trigger, CronTrigger)
    finally:
        sched.shutdown(wait=False)


# NOTE: the daily deep `git fsck --full` (F.1) is NO LONGER a learning-scheduler
# job — it runs from the awareness loop (`_check_git_health_deep`) so it survives a
# router-degraded startup that skips learning init. Its coverage lives in
# tests/test_awareness/test_git_health_check.py.


async def test_graph_traverse_prune_removes_only_old_telemetry(db, tmp_path, monkeypatch):
    """The job prunes graph_traverse and census rows past retention and nothing
    else, and trims old lines from the local lost-writes file."""
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import j9_eval
    from genesis.memory import graph_telemetry as graph_mod

    def _ts(days_ago: int) -> str:
        return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    old = graph_mod.TELEMETRY_RETENTION_DAYS + 1
    await j9_eval.insert_event(
        db, dimension="system", event_type=graph_mod.TELEMETRY_EVENT_TYPE,
        metrics={"k": "old"}, timestamp=_ts(old),
    )
    await j9_eval.insert_event(
        db, dimension="system", event_type=graph_mod.TELEMETRY_EVENT_TYPE,
        metrics={"k": "recent"}, timestamp=_ts(1),
    )
    await j9_eval.insert_event(
        db, dimension="memory", event_type="recall_trace",
        metrics={"k": "other-type"}, timestamp=_ts(old),
    )
    from genesis.memory.graph_census import CENSUS_EVENT_TYPE

    for label, age in (("census-old", old), ("census-recent", 1)):
        await j9_eval.insert_event(
            db, dimension="system", event_type=CENSUS_EVENT_TYPE,
            metrics={"k": label}, timestamp=_ts(age),
        )

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    lost = graph_mod.lost_writes_path()
    lost.parent.mkdir(parents=True)
    lost.write_text('{"ts": "2000-01-01T00:00:00.000000Z"}\n{"ts": "2999-01-01T00:00:00.000000Z"}\n')

    rt = _StubRT()
    rt._db = db
    sched = AsyncIOScheduler()
    _wire_drip_retention_jobs(sched, rt)
    sched.start(paused=True)
    try:
        await sched.get_job("graph_traverse_prune").func()
    finally:
        sched.shutdown(wait=False)

    cur = await db.execute("SELECT event_type, metrics_json FROM eval_events ORDER BY event_type")
    left = [(r[0], r[1]) for r in await cur.fetchall()]
    assert left == [
        (graph_mod.TELEMETRY_EVENT_TYPE, '{"k": "recent"}'),
        (CENSUS_EVENT_TYPE, '{"k": "census-recent"}'),
        ("recall_trace", '{"k": "other-type"}'),
    ]
    assert lost.read_text() == '{"ts": "2999-01-01T00:00:00.000000Z"}\n'


async def test_codex_inventory_has_its_own_retention_cutoff(db, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.sentinel import codex_mcp_inventory as inventory

    now = datetime.now(UTC)
    clock = now - timedelta(hours=73)
    bus = GenesisEventBus(clock=lambda: clock)
    bus.enable_persistence(db)
    monkeypatch.delenv("GENESIS_CODEX_MCP_INVENTORY_DISABLED", raising=False)
    monkeypatch.setattr(inventory.codex_mcp_stop, "load_mode", lambda: "propose_only")
    monkeypatch.setattr(inventory, "_latest", None)
    collect = AsyncMock(side_effect=lambda: inventory.Inventory(
        clock.isoformat(), "complete", daemon=("boot", 1, 100),
        threads=frozenset({"fixture-thread"}), groups=frozenset({(99, 100)}),
    ))
    monkeypatch.setattr(inventory, "collect", collect)
    try:
        await inventory.sample(bus)
        await bus.emit(Subsystem.HEALTH, Severity.DEBUG, "ordinary_event", "Retain by normal policy")
        clock = now
        await inventory.sample(bus)
    finally:
        await bus.stop()
    assert collect.await_count == 2
    before = await db.execute_fetchall("SELECT details FROM events WHERE event_type = ?", (inventory.EVENT_TYPE,))
    assert len(before) == 2
    assert all("fixture-thread" not in row[0] for row in before)

    rt = _StubRT()
    rt._db = db
    sched = AsyncIOScheduler()
    _wire_drip_retention_jobs(sched, rt)
    sched.start(paused=True)
    try:
        await sched.get_job("events_prune").func()
    finally:
        sched.shutdown(wait=False)
    remaining = await db.execute_fetchall("SELECT event_type FROM events ORDER BY timestamp")
    assert [row[0] for row in remaining] == ["ordinary_event", inventory.EVENT_TYPE]
