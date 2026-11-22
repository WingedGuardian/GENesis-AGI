"""The scheduled job's health reports whether weekly dream work completed."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory import dream_cycle
from genesis.runtime import GenesisRuntime
from genesis.surplus.jobs.dream import run_dream_cycle


@pytest.mark.parametrize(
    ("report", "expected_marker"),
    [
        ({"errors": []}, None),
        ({"errors": [{"phase": "entity_resolution", "error": "ReadTimeout"}]}, "entity_resolution"),
        ({"errors": [], "centrality": {"error": "worker unavailable"}}, "centrality"),
        ({"errors": [], "aborted": "low_memory"}, "low_memory"),
    ],
)
async def test_dream_cycle_job_health(monkeypatch, report, expected_marker):
    runtime = MagicMock(paused=False)
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: runtime)
    monkeypatch.setattr(dream_cycle, "run", AsyncMock(return_value=report))
    observed = AsyncMock()
    monkeypatch.setattr("genesis.db.crud.observations.create", observed)

    await run_dream_cycle()

    if expected_marker is None:
        runtime.record_job_success.assert_called_once_with("dream_cycle")
        runtime.record_job_failure.assert_not_called()
        expected_outcome = "complete"
    else:
        runtime.record_job_success.assert_not_called()
        runtime.record_job_failure.assert_called_once()
        assert expected_marker in str(runtime.record_job_failure.call_args)
        expected_outcome = "aborted" if "aborted" in report else "partial"
    assert expected_outcome in observed.call_args.kwargs["content"]
    assert runtime._heavy_workload is None


async def test_qdrant_retrieval_failure_reaches_weekly_health_and_report(db, monkeypatch):
    points = [
        SimpleNamespace(id=point_id, payload={"wing": "memory", "room": "store"})
        for point_id in ("a", "b")
    ]
    qdrant = MagicMock()
    qdrant.scroll.return_value = (points, None)
    qdrant.retrieve.return_value = []
    runtime = GenesisRuntime()
    runtime._db = db
    runtime._router = MagicMock()
    runtime._memory_store = SimpleNamespace(qdrant_client=qdrant)
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: runtime)
    monkeypatch.setattr(runtime, "_persist_job_health", MagicMock())
    monkeypatch.setattr(dream_cycle, "_read_mem_available_mb", lambda: None)
    for phase in (
        "dream_link_repair.run_link_repair",
        "dream_entity_scan.run_entity_resolution",
        "dream_orphan_detection.run_orphan_detection",
        "dream_centrality.run_centrality_recompute",
    ):
        monkeypatch.setattr(f"genesis.memory.{phase}", AsyncMock(return_value={}))

    await run_dream_cycle()
    first_success = runtime.job_health["dream_cycle"]["last_success"]
    qdrant.retrieve.side_effect = TimeoutError
    await run_dream_cycle()

    health = runtime.job_health["dream_cycle"]
    assert health["last_success"] == first_success
    assert health["last_error"] == "partial: clustering (1 item error(s))"
    assert health["consecutive_failures"] == 1
    rows = await db.execute_fetchall(
        "SELECT content FROM observations WHERE type = 'dream_cycle_report' ORDER BY created_at",
    )
    assert len(rows) == 2
    assert "weekly clustering, complete" in rows[0][0]
    assert "weekly clustering, partial" in rows[1][0]
