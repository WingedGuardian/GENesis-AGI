"""Startup visits both unfinished drains and unfinished aggregate disposition."""

import json
from unittest.mock import AsyncMock

import pytest

from genesis.cc.peer_segment import PeerSegment
from genesis.db.crud import cc_sessions
from genesis.peers.recovery import PeerRecovery
from tests.test_peers import test_artifacts

publication = test_artifacts.publication


async def recorded(s, **changes):
    metadata = (
        s.binding.metadata(2)
        | dict(
            cleanup_confirmed=True,
            peer_cancelled=False,
            peer_expired=False,
            error=None,
            result_artifact_path=str(s.path),
            tools_summary=s.result["tools_summary"],
        )
        | changes
    )
    async with s.registry.connection() as db:
        await db.execute(
            "UPDATE cc_sessions SET metadata=? WHERE id=?", (json.dumps(metadata), s.session)
        )
        await cc_sessions.update_status(db, s.session, status="completed")


async def snapshot(s):
    async with s.registry.connection() as db:
        task = dict(
            await (
                await db.execute("SELECT * FROM peer_tasks WHERE id=?", (s.task["id"],))
            ).fetchone()
        )
        segment = dict(
            await (
                await db.execute(
                    "SELECT * FROM peer_segments WHERE id=?", (s.binding.segment.segment_id,)
                )
            ).fetchone()
        )
        runtime = dict(
            await (
                await db.execute("SELECT * FROM peer_task_runtime WHERE task_id=?", (s.task["id"],))
            ).fetchone()
        )
        artifacts = (await (await db.execute("SELECT COUNT(*) FROM peer_artifacts")).fetchone())[0]
    return task, segment, runtime, artifacts


async def test_drained_success_before_publication_is_recovered_once(publication, monkeypatch):
    s = publication
    await recorded(s)
    drain = AsyncMock()
    monkeypatch.setattr(PeerSegment, "stop_and_drain", drain)
    recovery = PeerRecovery(s.registry, s.state.directory)
    assert await recovery.run()
    task, segment, _, count = await snapshot(s)
    assert task["state"] == "completed" and task["work_elapsed_s"] == 1 and count == 1
    assert segment["status"] == "drained"
    drain.assert_not_awaited()
    assert await recovery.run()
    repeated, _, _, count = await snapshot(s)
    assert repeated == task and count == 1
    async with s.registry.connection() as db:
        identifier = (await (await db.execute("SELECT id FROM peer_artifacts")).fetchone())[0]
    assert await s.service.fetch(s.identity, s.task["id"], identifier) == s.path.read_bytes()


@pytest.mark.parametrize(
    "changes",
    [
        {"cleanup_confirmed": False},
        {"cleanup_confirmed": 1},
        {"peer_cancelled": True},
        {"peer_expired": True},
        {"error": "failure"},
        {"peer_generation": 999},
        {"peer_generation": False},
        {"peer_task_id": "0" * 32},
        {"peer_segment_id": "0" * 32},
        {"caller_context": "foreground"},
        {"result_artifact_path": "/outside/result.md"},
        {"tools_summary": {"Bash": 1}},
    ],
)
async def test_invalid_success_record_never_publishes(publication, changes):
    s = publication
    await recorded(s, **changes)
    assert await PeerRecovery(s.registry, s.state.directory).run()
    task, _, _, count = await snapshot(s)
    assert task["state"] == "failed" and count == 0


async def test_drained_without_recorded_success_is_not_stranded(publication):
    s = publication
    assert await PeerRecovery(s.registry, s.state.directory).run()
    task, segment, _, count = await snapshot(s)
    assert task["state"] == "failed" and segment["status"] == "drained" and count == 0


@pytest.mark.parametrize("clean", [True, False])
async def test_running_unknown_elapsed_charges_original_reservation(
    publication, monkeypatch, clean
):
    s = publication
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_segments SET status='running',charged_s=0,completed_at=NULL,execution_elapsed_s=NULL"
        )
        await db.execute("UPDATE peer_tasks SET work_elapsed_s=0,slot_reserved=1")
    drain = AsyncMock(side_effect=None if clean else RuntimeError("Unconfirmed fixture scope"))
    monkeypatch.setattr(PeerSegment, "stop_and_drain", drain)
    recovery = PeerRecovery(s.registry, s.state.directory)
    assert await recovery.run() is clean
    task, segment, runtime, _ = await snapshot(s)
    assert task["work_elapsed_s"] == segment["reserved_s"] == segment["charged_s"]
    assert segment["status"] == ("drained" if clean else "blocked")
    assert task["state"] == ("failed" if clean else "working")
    if not clean:
        assert runtime["hold_reason"] == "reconciliation" and task["slot_reserved"] == 1
        assert not await recovery.run()
        repeated, _, _, _ = await snapshot(s)
        assert repeated["work_elapsed_s"] == task["work_elapsed_s"]


async def test_disabled_recovery_still_drains_without_runner_or_manager(publication, monkeypatch):
    s = publication
    await s.registry.configure("disabled")
    async with s.registry.transaction() as db:
        await db.execute("UPDATE peer_segments SET status='running'")
    drain = AsyncMock()
    monkeypatch.setattr(PeerSegment, "stop_and_drain", drain)
    assert await PeerRecovery(s.registry, s.state.directory).run()
    drain.assert_awaited_once()
    task, segment, _, _ = await snapshot(s)
    assert task["state"] == "failed" and segment["status"] == "drained"


async def test_prepared_orphan_is_proven_unstarted_not_replayed(publication, monkeypatch):
    s = publication
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_segments SET status='prepared',started_at=NULL,session_id=NULL,charged_s=0,completed_at=NULL,execution_elapsed_s=NULL"
        )
        await db.execute("UPDATE peer_tasks SET work_elapsed_s=0,slot_reserved=1")
        await db.execute(
            "UPDATE cc_sessions SET metadata=? WHERE id=?",
            (json.dumps(s.binding.metadata(2)), s.session),
        )
    monkeypatch.setattr(PeerSegment, "stop_and_drain", AsyncMock())
    assert await PeerRecovery(s.registry, s.state.directory).run()
    task, segment, _, count = await snapshot(s)
    assert task["state"] == "failed" and task["work_elapsed_s"] == 0 and segment["charged_s"] == 0
    assert count == 0
    async with s.registry.connection() as db:
        session = await cc_sessions.get_by_id(db, s.session)
    assert session["status"] == "failed" and session["completed_at"]


async def test_executing_consequential_receipt_stays_reconciled(publication):
    s = publication
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_operations SET immutable_read=0,status='executing',result_json=NULL"
        )
    await recorded(s)
    assert await PeerRecovery(s.registry, s.state.directory).run()
    task, _, runtime, count = await snapshot(s)
    assert task["state"] == "working" and runtime["hold_reason"] == "reconciliation" and count == 0
    async with s.registry.connection() as db:
        status = (await (await db.execute("SELECT status FROM peer_operations")).fetchone())[0]
    assert status == "unknown"


async def test_unstarted_proof_survives_failed_drain_retry(publication, monkeypatch):
    s = publication
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_segments SET status='prepared',started_at=NULL,charged_s=0,completed_at=NULL,execution_elapsed_s=NULL"
        )
        await db.execute("UPDATE peer_tasks SET work_elapsed_s=0,slot_reserved=1")
    recovery = PeerRecovery(s.registry, s.state.directory)
    monkeypatch.setattr(
        PeerSegment, "stop_and_drain", AsyncMock(side_effect=RuntimeError("fixture"))
    )
    assert not await recovery.run()
    first, segment, _, _ = await snapshot(s)
    assert first["work_elapsed_s"] == 0 and segment["status"] == "blocked"
    monkeypatch.setattr(PeerSegment, "stop_and_drain", AsyncMock())
    assert await recovery.run()
    second, segment, _, _ = await snapshot(s)
    assert second["work_elapsed_s"] == 0 and segment["charged_s"] == 0
    assert segment["status"] == "drained" and segment["started_at"] is None
