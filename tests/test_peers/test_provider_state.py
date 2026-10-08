"""Atomic peer provider holds, lineage and terminal park retirement."""

import asyncio
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.cc import rate_limit_resume_config as config
from genesis.cc.exceptions import CCRateLimitError
from genesis.cc.peer_segment import PeerSegment
from genesis.cc.rate_limit_resume import _redispatch, run_resume_tick
from genesis.db.crud import cc_rate_limit_parks as parks
from genesis.db.crud import cc_sessions
from genesis.db.schema import INDEXES, TABLES
from genesis.peers.lifecycle_state import PeerLifecycleState, utcnow
from genesis.peers.operation_state import PeerOperationState
from genesis.peers.provider_state import PeerProviderState
from genesis.peers.registry import PeerRegistry
from genesis.peers.session import PeerSessionBinding
from genesis.peers.tasks import PeerTasks


async def fixture(tmp_path):
    root = tmp_path
    path = root / "state.db"
    async with aiosqlite.connect(path) as db:
        for name in (
            "peer_settings",
            "peers",
            "peer_grants",
            "cc_sessions",
            "direct_session_queue",
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
            "approval_requests",
            "cc_rate_limit_parks",
            "peer_operation_approvals",
            "peer_segments",
            "peer_task_runtime",
            "peer_task_consents",
            "peer_operations",
            "peer_resources",
        ):
            await db.execute(TABLES[name])
        for statement in INDEXES:
            if " ON cc_rate_limit_parks" in statement:
                await db.execute(statement)
        await db.commit()
    registry = PeerRegistry(path)
    await registry.register(
        "fixture", same_owner=True, daily_allowance=10, token_name="GENESIS_PEER_FIXTURE_TOKEN"
    )
    await registry.grant("fixture", "conversation", "allow")
    await registry.grant("fixture", "research", "allow")
    task = await PeerTasks(registry).admit(
        await registry.get("fixture"),
        {
            "messageId": "provider",
            "role": "ROLE_USER",
            "parts": [{"text": "Scratch accepted request."}],
        },
    )
    state = PeerLifecycleState(registry, root / "work")
    provider = PeerProviderState(registry)

    async def claim():
        row = await state.claim()
        binding = PeerSessionBinding(
            task["id"],
            PeerSegment(
                row["segment_id"], row["deadline_at"], ("mcp__genesis_peer__task_context",)
            ),
            row["generation"],
            str(root / "facade.json"),
            row["working_dir"],
        )
        session_id = "fixture-session-" + str(row["generation"])
        async with registry.connection() as db:
            await cc_sessions.create(
                db,
                id=session_id,
                session_type="background_task",
                model="sonnet",
                effort="medium",
                status="active",
                started_at=utcnow().isoformat(),
                last_activity_at=utcnow().isoformat(),
                source_tag="peer_api",
            )
        await state.begin(binding, session_id)
        return binding

    return registry, task, state, provider, claim


async def test_repeat(tmp_path):
    registry, task, state, provider, claim = await fixture(tmp_path)
    binding = await claim()
    assert await provider.park(
        binding,
        CCRateLimitError("Private provider diagnosis", raw_text="Private provider diagnosis"),
    )
    async with registry.connection() as db:
        runtime = dict(await (await db.execute("SELECT * FROM peer_task_runtime")).fetchone())
        first = await parks.get_by_id(db, runtime["park_id"])
        assert first["raw_signal"] is None
        assert "Private provider diagnosis" not in first["payload_json"]
        assert set(json.loads(first["payload_json"])) == {
            "source_tag",
            "peer_task_id",
            "epoch",
            "generation",
            "segment_id",
        }
    assert not await provider.resume(first["id"], now=utcnow() + timedelta(hours=2))
    await state.settle(binding, 1.2, clean=True)
    async with registry.connection() as db:
        before = (await (await db.execute("SELECT count(*) FROM direct_session_queue")).fetchone())[
            0
        ]
        rt = SimpleNamespace(
            _db=db,
            _peer_session_lifecycle=None,
            _outreach_pipeline=None,
            record_job_success=lambda *a: None,
            record_job_failure=lambda *a, **k: None,
        )
        await run_resume_tick(rt, now=utcnow() + timedelta(hours=2))
        assert (await (await db.execute("SELECT count(*) FROM direct_session_queue")).fetchone())[
            0
        ] == before
        assert (await parks.get_by_id(db, first["id"]))["status"] == "parked"
        try:
            await _redispatch(db, first)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Legacy owner reconstruction accepted peer")
    assert await provider.resume(first["id"], now=utcnow() + timedelta(hours=2))
    assert not await provider.resume(first["id"], now=utcnow() + timedelta(hours=2))
    binding = await claim()
    assert await provider.park(binding, CCRateLimitError("Second private diagnosis"))
    await state.settle(binding, 1.1, clean=True)
    async with registry.connection() as db:
        second = await parks.get_by_id(db, first["id"])
        assert second["attempts"] == 1 and second["status"] == "parked"
        assert (await (await db.execute("SELECT count(*) FROM cc_rate_limit_parks")).fetchone())[
            0
        ] == 1
        assert (
            await (await db.execute("SELECT sum(admissions) FROM peer_daily_admissions")).fetchone()
        )[0] == 1
    assert await provider.resume(first["id"], now=utcnow() + timedelta(hours=2))
    binding = await claim()
    await state.hold(
        binding,
        "approval",
        capability="research",
        digest=hashlib.sha256(b"new operation").hexdigest(),
    )
    await state.settle(binding, 0.2, clean=True)
    async with registry.connection() as db:
        assert (await (await db.execute("SELECT park_id FROM peer_task_runtime")).fetchone())[
            0
        ] == first["id"]
    assert await state.end(task["id"], "failed", "Peer task stopped")
    async with registry.connection() as db:
        assert (await parks.get_by_id(db, first["id"]))["status"] == "cancelled"
    print(
        "PASS repeated provider holds: same park/attempt lineage, one admission, drain prerequisite, no legacy owner fallback, approval hold preserves park, atomic terminal retirement"
    )


async def test_unknown(tmp_path):
    registry, task, state, provider, claim = await fixture(tmp_path)
    binding = await claim()
    operations = PeerOperationState(registry)
    receipt = await operations.prepare(
        binding, "research", hashlib.sha256(b"effect").hexdigest(), immutable_read=False
    )
    await operations.transition(binding, receipt["id"], "executing")
    await operations.transition(binding, receipt["id"], "unknown")
    assert await provider.park(binding, CCRateLimitError("Private diagnosis"))
    await state.settle(binding, 1, clean=True)
    async with registry.connection() as db:
        identifier = (await (await db.execute("SELECT park_id FROM peer_task_runtime")).fetchone())[
            0
        ]
    assert not await provider.resume(identifier, now=utcnow() + timedelta(hours=2))
    async with registry.connection() as db:
        assert (await (await db.execute("SELECT hold_reason FROM peer_task_runtime")).fetchone())[
            0
        ] == "reconciliation"
        assert (await parks.get_by_id(db, identifier))["status"] == "parked"
    print("PASS unknown consequential outcome blocks provider continuation without park claim")


async def test_rollback(tmp_path):
    registry, task, state, provider, claim = await fixture(tmp_path)
    binding = await claim()
    original = parks.upsert_open_park

    async def interrupted(db, **kwargs):
        await original(db, **kwargs)
        raise asyncio.CancelledError()

    parks.upsert_open_park = interrupted
    try:
        try:
            await provider.park(binding, CCRateLimitError("Private diagnosis"))
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Injected cancellation missing")
    finally:
        parks.upsert_open_park = original
    async with registry.connection() as db:
        assert (await (await db.execute("SELECT count(*) FROM cc_rate_limit_parks")).fetchone())[
            0
        ] == 0
        assert (await (await db.execute("SELECT generation FROM peer_tasks")).fetchone())[
            0
        ] == binding.generation
        assert (await (await db.execute("SELECT hold_reason FROM peer_task_runtime")).fetchone())[
            0
        ] is None
    print("PASS cancellation after real park insert rolls back park and hold/generation together")


@pytest.fixture(autouse=True)
def live_resume(monkeypatch):
    monkeypatch.setattr(config, "effective_mode", lambda: "live")


@pytest.mark.parametrize("park_status", ["parked", "resuming", "needs_user"])
@pytest.mark.parametrize("task_status", ["failed", "canceled", "rejected"])
async def test_all_terminal_outcomes_retire_all_open_park_states(
    tmp_path, park_status, task_status
):
    registry, task, state, provider, claim = await fixture(tmp_path)
    binding = await claim()
    assert await provider.park(binding, CCRateLimitError("Fixture diagnosis"))
    await state.settle(binding, 1, clean=True)
    async with registry.transaction() as db:
        identifier = (await (await db.execute("SELECT park_id FROM peer_task_runtime")).fetchone())[
            0
        ]
        await db.execute(
            "UPDATE cc_rate_limit_parks SET status=? WHERE id=?", (park_status, identifier)
        )
    assert await state.end(task["id"], task_status, "Peer task stopped")
    async with registry.connection() as db:
        assert (await parks.get_by_id(db, identifier))["status"] == "cancelled"
        terminal = await (await db.execute("SELECT state,slot_reserved FROM peer_tasks")).fetchone()
        assert terminal["state"] == task_status and not terminal["slot_reserved"]


@pytest.mark.parametrize("continuation", ["initial", "provider", "approval"])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_pending_cancellation_retires_park_in_the_same_transaction(
    tmp_path, monkeypatch, continuation, interrupt
):
    from dataclasses import replace

    from genesis.autonomy.approval import ApprovalManager
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
    from genesis.peers import coordinator as coordinator_module
    from genesis.peers.approvals import PeerApprovals
    from genesis.peers.coordinator import PeerCoordinator
    from genesis.peers.lifecycle_state import dispatch_digest

    registry, task, state, provider, claim = await fixture(tmp_path)
    identity = await registry.get("fixture")
    if continuation == "initial":
        # The fixture has admitted but never claimed the original request.
        identifier = None
    else:
        binding = await claim()
        assert await provider.park(binding, CCRateLimitError("Fixture diagnosis"))
        await state.settle(binding, 1, clean=True)
        async with registry.connection() as db:
            identifier = (
                await (await db.execute("SELECT park_id FROM peer_task_runtime")).fetchone()
            )[0]
        assert await provider.resume(identifier, now=utcnow() + timedelta(hours=2))
        if continuation == "approval":
            binding = await claim()
            await registry.grant("fixture", "conversation", "ask")
            async with registry.connection() as db:
                row = await (await db.execute("SELECT * FROM peer_tasks")).fetchone()
                digest = dispatch_digest(row)
                generation = await state.hold(
                    binding, "approval", capability="conversation", digest=digest
                )
                held = replace(binding, generation=generation)
                manager = ApprovalManager(db=db)
                gate = AutonomousCliApprovalGate(
                    runtime=SimpleNamespace(_outreach_pipeline=None), approval_manager=manager
                )
                service = PeerApprovals(registry, manager, gate)
                approval_id = await service.request(
                    held, "conversation", digest, "Fixture exact exchange.", notify=False
                )
                await state.associate_approval(held, approval_id)
                await service.deliver(approval_id)
                await state.settle(binding, 1, clean=True)
                assert await gate.resolve_request(
                    approval_id, decision="approved", resolved_by="dashboard"
                )
                assert await state.resume_approval(task["id"])

    async def publish(*args):
        raise AssertionError("Cancellation must not publish")

    controller = PeerCoordinator(
        registry, SimpleNamespace(), None, None, tmp_path / "private", publish
    )
    if interrupt:
        original = coordinator_module.retire_park

        async def interrupted(db, row):
            await original(db, row)
            raise asyncio.CancelledError()

        monkeypatch.setattr(coordinator_module, "retire_park", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await controller.cancel(identity, task["id"])
    else:
        await controller.cancel(identity, task["id"])
    async with registry.connection() as db:
        row = await (
            await db.execute(
                "SELECT t.state,t.slot_reserved,q.status FROM peer_tasks t JOIN direct_session_queue q ON q.id=t.queue_id WHERE t.id=?",
                (task["id"],),
            )
        ).fetchone()
        assert row["state"] == ("submitted" if interrupt else "canceled")
        assert row["status"] == ("pending" if interrupt else "failed")
        assert row["slot_reserved"] == int(interrupt)
        if identifier is not None:
            assert (await parks.get_by_id(db, identifier))["status"] == (
                "resuming" if interrupt else "cancelled"
            )
