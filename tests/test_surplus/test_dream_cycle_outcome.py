"""The scheduled job's health reports whether weekly dream work completed."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import QdrantClient, models

from genesis.db.connection import SerializedConnection
from genesis.memory import dream_cycle
from genesis.resilience.deferred_work import DeferredWorkQueue
from genesis.runtime import GenesisRuntime
from genesis.surplus.jobs.dream import run_dream_cycle, run_dream_synthesis_drain


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


@pytest.mark.parametrize(
    ("phase", "detail"),
    [
        ("clustering", "1 item error(s)"),
        ("cross_wing", "1 item error(s)"),
        ("shield", "failed"),
        ("worklist_persist", "failed"),
        ("writer_close", "failed"),
        ("entity_resolution", "failed"),
    ],
)
async def test_weekly_publication_and_health_on_phase_failure(file_db, monkeypatch, phase, detail):
    db = file_db
    points = [
        SimpleNamespace(id=point_id, payload={"wing": "memory", "room": "store"})
        for point_id in ("a", "b")
    ]
    qdrant = MagicMock()
    qdrant.scroll.return_value = (points, None)
    qdrant.retrieve.return_value = [
        SimpleNamespace(id=point.id, vector=[1.0, 0.0]) for point in points
    ]
    runtime = _scheduled_runtime(db, qdrant, monkeypatch)

    await run_dream_cycle()
    first_success = runtime.job_health["dream_cycle"]["last_success"]
    prior_cluster = [
        {"id": p.id, "wing": "memory", "room": "store", "payload": p.payload} for p in points
    ]
    await dream_cycle._persist_worklist(db, [prior_cluster], weekly_run_id="previous")
    prior_queue = await db.execute_fetchall("SELECT id, payload_json FROM deferred_work_queue")
    if phase == "clustering":
        qdrant.retrieve.side_effect = TimeoutError
    elif phase == "cross_wing":
        monkeypatch.setattr(
            dream_cycle, "_cross_wing_scan", AsyncMock(return_value=([], {"search/TimeoutError": 1})),
        )
    elif phase == "shield":
        monkeypatch.setattr(
            "genesis.memory.dream_shield.compute_shield_state", AsyncMock(side_effect=RuntimeError),
        )
    elif phase in {"worklist_persist", "writer_close"}:
        # Keep the tiny cluster, which ties at the activation percentile.
        monkeypatch.setenv("GENESIS_DREAM_SHIELD_DISABLED", "1")
        monkeypatch.setattr(dream_cycle, "_cluster_bucket", AsyncMock(return_value=[prior_cluster]))
    if phase == "worklist_persist":
        await db.execute(
            """CREATE TRIGGER fail_fixture_enqueue BEFORE INSERT ON deferred_work_queue
               BEGIN SELECT RAISE(ABORT, 'fixture enqueue failure'); END""",
        )
        await db.commit()
    if phase == "entity_resolution":
        monkeypatch.setattr(
            "genesis.memory.dream_entity_scan.run_entity_resolution",
            AsyncMock(side_effect=TimeoutError),
        )

    close = SerializedConnection.close

    async def close_then_raise(connection):
        await close(connection)
        if connection is not db:
            raise RuntimeError("fixture close failure")

    with monkeypatch.context() as patcher:
        if phase == "writer_close":
            patcher.setattr(SerializedConnection, "close", close_then_raise)
        await run_dream_cycle()

    new_queue = await db.execute_fetchall("SELECT id, payload_json FROM deferred_work_queue")
    if phase == "writer_close":
        assert len(new_queue) == 1
        assert new_queue[0]["id"] != prior_queue[0]["id"]
    else:
        assert new_queue == ([] if phase == "entity_resolution" else prior_queue)
    health = runtime.job_health["dream_cycle"]
    assert health["last_success"] == first_success
    reported_phase = "worklist_persist" if phase == "writer_close" else phase
    assert health["last_error"] == f"partial: {reported_phase} ({detail})"
    assert health["consecutive_failures"] == 1
    rows = await db.execute_fetchall(
        "SELECT content FROM observations WHERE type = 'dream_cycle_report' ORDER BY created_at",
    )
    assert len(rows) == 2
    assert "weekly clustering, complete" in rows[0][0]
    assert "weekly clustering, partial" in rows[1][0]
    assert ("prior worklist kept" in rows[1][0]) is (phase in {"clustering", "cross_wing", "shield"})
    if reported_phase == "worklist_persist":
        assert "worklist publication error" in rows[1][0]


def _scheduled_runtime(db, qdrant, monkeypatch):
    runtime = GenesisRuntime()
    runtime._db = db
    runtime._router = MagicMock()
    runtime._memory_store = SimpleNamespace(qdrant_client=qdrant)
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: runtime)
    monkeypatch.setattr(runtime, "_persist_job_health", MagicMock())
    monkeypatch.setattr("genesis.runtime._job_health._append_job_run_event", MagicMock())
    monkeypatch.setattr("genesis.runtime._job_health._persist_job_start", MagicMock())
    monkeypatch.setattr(dream_cycle, "_read_mem_available_mb", lambda: None)
    for phase_path in (
        "dream_link_repair.run_link_repair",
        "dream_entity_scan.run_entity_resolution",
        "dream_orphan_detection.run_orphan_detection",
        "dream_centrality.run_centrality_recompute",
    ):
        monkeypatch.setattr(f"genesis.memory.{phase_path}", AsyncMock(return_value={}))

    return runtime


@pytest.mark.parametrize("failure", ["timeout", "omitted_points", "no_vector"])
async def test_weekly_failure_leaves_work_for_the_scheduled_daily_drain(file_db, monkeypatch, failure):
    qdrant = QdrantClient(location=":memory:", force_disable_check_same_thread=True)
    try:
        collection = dream_cycle.COLLECTION
        qdrant.create_collection(
            collection, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
        )
        qdrant.upsert(collection, points=[
            models.PointStruct(
                id=f"00000000-0000-4000-8000-{point_id:012d}",
                vector=[1.0, 0.0],
                payload={"wing": "memory", "room": "store", "content": f"fixture {point_id}"},
            )
            for point_id in (1, 2)
        ])
        monkeypatch.setenv("GENESIS_DREAM_SHIELD_DISABLED", "1")
        monkeypatch.delenv("GENESIS_DREAM_CYCLE_LIVE", raising=False)
        runtime = _scheduled_runtime(file_db, qdrant, monkeypatch)
        queue = DeferredWorkQueue(file_db)

        await run_dream_cycle()
        before = await queue.next_pending(work_type=dream_cycle.WORKLIST_WORK_TYPE)
        assert before is not None
        first_success = runtime.job_health["dream_cycle"]["last_success"]
        assert first_success
        if failure == "no_vector":
            vectorless = "00000000-0000-4000-8000-000000000003"
            qdrant.upsert(collection, points=[
                models.PointStruct(
                    id=vectorless, vector={}, payload={"wing": "memory", "room": "store"},
                ),
            ])
            await run_dream_cycle()
            qdrant.delete(collection, points_selector=[vectorless])
        else:
            with monkeypatch.context() as patcher:
                timeout = TimeoutError if failure == "timeout" else None
                patcher.setattr(qdrant, "retrieve", MagicMock(side_effect=timeout, return_value=[]))
                await run_dream_cycle()
        after = await queue.next_pending(work_type=dream_cycle.WORKLIST_WORK_TYPE)
        assert after["id"] == before["id"]
        health = runtime.job_health["dream_cycle"]
        assert health["last_success"] == first_success
        assert health["last_error"] == (
            "partial: clustering (2 item error(s))" if failure == "omitted_points"
            else "partial: clustering (1 item error(s))"
        )
        assert health["consecutive_failures"] == 1

        await run_dream_synthesis_drain()
        rows = await file_db.execute_fetchall(
            "SELECT status, attempts FROM deferred_work_queue WHERE id = ?", (before["id"],),
        )
        assert [tuple(row) for row in rows] == [("completed", 1)]
        reports = await file_db.execute_fetchall(
            "SELECT content FROM observations WHERE type = 'dream_synthesis_drain_report'",
        )
        assert "SHADOW: 1 drained, 1 would merge" in reports[0][0]
        runtime.router.route_call.assert_not_called()

        await run_dream_cycle()
        replacement = await queue.next_pending(work_type=dream_cycle.WORKLIST_WORK_TYPE)
        assert replacement["id"] != before["id"]
        assert json.loads(replacement["payload_json"])["weekly_run_id"] != json.loads(
            before["payload_json"],
        )["weekly_run_id"]
        assert runtime.job_health["dream_cycle"]["consecutive_failures"] == 0
    finally:
        qdrant.close()
