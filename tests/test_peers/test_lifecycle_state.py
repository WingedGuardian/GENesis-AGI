"""Durable continuation uses actual SQLite and the normal individual resolver."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
from genesis.cc.peer_segment import PeerSegment
from genesis.db.schema import TABLES
from genesis.peers.approvals import PeerApprovals
from genesis.peers.lifecycle_state import PeerLifecycleState, dispatch_digest
from genesis.peers.operation_state import PeerOperationState
from genesis.peers.session import PeerSessionBinding
from genesis.peers.tasks import PeerTasks, TaskRefusal


async def test_escaped_sensitive_receipt_refused_without_committing(lifecycle):
    import secrets

    s = lifecycle
    _, _, binding = await approve(s)
    operations = PeerOperationState(s.registry)
    receipt = await operations.prepare(binding, "conversation", s.digest, immutable_read=True)
    await operations.transition(binding, receipt["id"], "executing")
    result = {"nested": [{"text": 'token: "' + secrets.token_hex(16) + '"'}]}
    with pytest.raises(ValueError, match="Peer operation result refused"):
        await operations.transition(binding, receipt["id"], "completed", result=result)
    async with s.registry.connection() as db:
        row = await (
            await db.execute(
                "SELECT status,result_json FROM peer_operations WHERE id=?", (receipt["id"],)
            )
        ).fetchone()
    assert tuple(row) == ("executing", None)
    await operations.transition(
        binding, receipt["id"], "completed", result={"text": "Public fixture"}
    )


@pytest.fixture
async def lifecycle(registry, tmp_path, request):
    async with registry.connection() as db:
        for name in (
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
        ):
            await db.execute(TABLES[name])
        await db.commit()
    await registry.register(
        "fixture", same_owner=True, daily_allowance=10, token_name="GENESIS_PEER_FIXTURE_TOKEN"
    )
    await registry.grant("fixture", "conversation", getattr(request, "param", "ask"))
    for n in ("1", "2"):
        await registry.grant("fixture", "resource:" + n * 32, "ask")
    identity = await registry.get("fixture")
    task = await PeerTasks(registry).admit(
        identity, {"messageId": "first", "role": "ROLE_USER", "parts": [{"text": "Fixture."}]}
    )
    state = PeerLifecycleState(registry, tmp_path / "work")
    claimed = await state.claim()
    binding = PeerSessionBinding(
        task["id"],
        PeerSegment(
            claimed["segment_id"], claimed["deadline_at"], ("mcp__genesis_peer__task_context",)
        ),
        0,
        str(tmp_path / "facade.json"),
        claimed["working_dir"],
    )
    async with registry.connection() as db:
        manager = ApprovalManager(db=db)
        gate = AutonomousCliApprovalGate(
            runtime=SimpleNamespace(_outreach_pipeline=None), approval_manager=manager
        )
        service = PeerApprovals(registry, manager, gate)
        yield SimpleNamespace(
            registry=registry,
            identity=identity,
            task=task,
            state=state,
            binding=binding,
            manager=manager,
            gate=gate,
            service=service,
            digest=dispatch_digest(claimed),
        )


async def hold(s, capability="conversation", digest=None):
    digest = s.digest if digest is None else digest
    generation = await s.state.hold(s.binding, "approval", capability=capability, digest=digest)
    held = replace(s.binding, generation=generation)
    identifier = await s.service.request(held, capability, digest, "Read the fixture exchange.")
    await s.state.associate_approval(held, identifier)
    await s.service.deliver(identifier)
    return identifier


async def approve(s):
    identifier = await hold(s)
    await s.state.settle(s.binding, 1.2, clean=True)
    assert await s.gate.resolve_request(identifier, decision="approved", resolved_by="dashboard")
    assert await s.state.resume_approval(s.task["id"])
    claimed = await s.state.claim()
    binding = replace(
        s.binding,
        generation=claimed["generation"],
        working_dir=claimed["working_dir"],
        segment=PeerSegment(claimed["segment_id"], claimed["deadline_at"], s.binding.segment.tools),
    )
    return identifier, claimed, binding


async def test_continuation_consumes_once_and_preserves_budget_and_daily_charge(lifecycle):
    s = lifecycle
    identifier, claimed, binding = await approve(s)
    assert claimed["reserved_s"] == 3598 and claimed["generation"] == 2
    assert not await s.state.resume_approval(s.task["id"])
    assert await s.state.consent(binding, "conversation", s.digest)
    assert await s.state.consent(binding, "conversation", s.digest)
    assert not await s.gate.resolve_request(
        identifier, decision="approved", resolved_by="dashboard"
    )
    async with s.registry.connection() as db:
        count = (
            await (await db.execute("SELECT SUM(admissions) FROM peer_daily_admissions")).fetchone()
        )[0]
        assert count == 1


async def test_approved_hold_cannot_resume_before_both_drains(lifecycle):
    s = lifecycle
    identifier = await hold(s)
    assert await s.gate.resolve_request(identifier, decision="approved", resolved_by="dashboard")
    assert not await s.state.resume_approval(s.task["id"])
    await s.state.settle(s.binding, 1, clean=False)
    assert not await s.state.resume_approval(s.task["id"])
    row = await PeerTasks(s.registry).owned(s.identity, s.task["id"])
    assert row["slot_reserved"] == 1


@pytest.mark.parametrize(
    "change", ["digest", "epoch", "grant", "cancel", "expiry", "late", "resolver", "context"]
)
async def test_consent_rechecks_each_boundary_at_operation_use(lifecycle, change):
    s = lifecycle
    identifier, _, binding = await approve(s)
    digest = s.digest
    async with s.registry.transaction() as db:
        if change == "digest":
            digest = hashlib.sha256(b"changed operation").hexdigest()
        elif change == "epoch":
            await db.execute("UPDATE peers SET epoch=? WHERE peer_id='fixture'", ("f" * 32,))
        elif change == "grant":
            await db.execute("UPDATE peer_grants SET decision='deny' WHERE peer_id='fixture'")
        elif change == "cancel":
            await db.execute("UPDATE peer_tasks SET cancel_requested=1 WHERE id=?", (s.task["id"],))
        elif change == "expiry":
            await db.execute(
                "UPDATE peer_tasks SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (s.task["id"],),
            )
        elif change == "late":
            await db.execute(
                "UPDATE approval_requests SET resolved_at=timeout_at WHERE id=?", (identifier,)
            )
        elif change == "context":
            await db.execute("UPDATE approval_requests SET context='{}' WHERE id=?", (identifier,))
        elif change == "resolver":
            await db.execute(
                "UPDATE approval_requests SET resolved_by='dashboard:batch' WHERE id=?",
                (identifier,),
            )
    try:
        allowed = await s.state.consent(binding, "conversation", digest)
    except TaskRefusal:
        allowed = False
    assert allowed is False


async def test_old_segment_cannot_authorize_over_approval_hold(lifecycle):
    s = lifecycle
    await hold(s)
    await s.state.settle(s.binding, 1, clean=True)
    with pytest.raises(TaskRefusal):
        await s.state.consent(s.binding, "conversation", s.digest)
    row = await PeerTasks(s.registry).owned(s.identity, s.task["id"])
    assert row["state"] == "working" and row["generation"] == 1


@pytest.mark.parametrize(
    "elapsed,uncertain,expected", [(1.2, False, 2), (0, True, 3600), (float("nan"), False, 3600)]
)
async def test_settlement_is_once_and_unknown_work_is_never_refunded(
    lifecycle, elapsed, uncertain, expected
):
    s = lifecycle
    await s.state.settle(s.binding, elapsed, clean=True, uncertain=uncertain)
    await s.state.settle(s.binding, elapsed, clean=True, uncertain=uncertain)
    row = await PeerTasks(s.registry).owned(s.identity, s.task["id"])
    assert row["work_elapsed_s"] == expected and row["slot_reserved"] == 0


async def test_terminalization_refuses_unconfirmed_cleanup(lifecycle):
    s = lifecycle
    assert not await s.state.end(s.task["id"], "failed", "Execution stopped")
    await s.state.settle(s.binding, 1, clean=False)
    assert not await s.state.end(s.task["id"], "failed", "Execution stopped")
    await s.state.settle(s.binding, 1, clean=True)
    assert await s.state.end(s.task["id"], "failed", "Execution stopped")
    assert not await s.state.end(s.task["id"], "failed", "Execution stopped")


async def next_binding(s):
    claimed = await s.state.claim()
    return replace(
        s.binding,
        generation=claimed["generation"],
        working_dir=claimed["working_dir"],
        segment=PeerSegment(claimed["segment_id"], claimed["deadline_at"], s.binding.segment.tools),
    )


async def test_unchanged_conversation_survives_two_independent_resource_holds(lifecycle):
    s = lifecycle
    _, _, s.binding = await approve(s)
    first = s.binding
    for n in ("1", "2"):
        capability = "resource:" + n * 32
        digest = hashlib.sha256(n.encode()).hexdigest()
        assert await s.state.consent(s.binding, "conversation", s.digest)
        assert not await s.state.consent(s.binding, capability, digest)
        identifier = await hold(s, capability, digest)
        await s.state.settle(s.binding, 1, clean=True)
        assert await s.gate.resolve_request(
            identifier, decision="approved", resolved_by="dashboard"
        )
        assert await s.state.resume_approval(s.task["id"])
        s.binding = await next_binding(s)
        assert await s.state.consent(s.binding, capability, digest)
        assert await s.state.consent(s.binding, "conversation", s.digest)
    with pytest.raises(TaskRefusal):
        await s.state.consent(first, "conversation", s.digest)
    async with s.registry.connection() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM peer_task_consents")).fetchone())[
            0
        ] == 3
        assert (
            await (await db.execute("SELECT SUM(admissions) FROM peer_daily_admissions")).fetchone()
        )[0] == 1


async def test_consumption_and_queue_insertion_rollback_together(lifecycle, monkeypatch):
    from genesis.peers.lifecycle_state import queue

    s = lifecycle
    identifier = await hold(s)
    await s.state.settle(s.binding, 1, clean=True)
    assert await s.gate.resolve_request(identifier, decision="approved", resolved_by="dashboard")
    original = queue.insert_prepared

    async def fail_after_insert(db, prepared):
        await original(db, prepared)
        raise RuntimeError("Fixture crash")

    with monkeypatch.context() as patch:
        patch.setattr(queue, "insert_prepared", fail_after_insert)
        with pytest.raises(RuntimeError):
            await s.state.resume_approval(s.task["id"])
    async with s.registry.connection() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM direct_session_queue")).fetchone())[
            0
        ] == 1
        assert (
            await (
                await db.execute(
                    "SELECT consumed_at FROM approval_requests WHERE id=?", (identifier,)
                )
            ).fetchone()
        )[0] is None
        assert (await (await db.execute("SELECT COUNT(*) FROM peer_task_consents")).fetchone())[
            0
        ] == 0
    assert await s.state.resume_approval(s.task["id"])
    assert not await s.state.resume_approval(s.task["id"])


@pytest.mark.parametrize("immutable_read", [True, False])
async def test_unknown_operation_retry_requires_exact_read_and_confirmed_drain(
    lifecycle, immutable_read
):
    s = lifecycle
    _, _, s.binding = await approve(s)
    operations = PeerOperationState(s.registry)
    operation = await operations.prepare(
        s.binding, "conversation", s.digest, immutable_read=immutable_read
    )
    await operations.transition(s.binding, operation["id"], "executing")
    await operations.transition(s.binding, operation["id"], "unknown")
    with pytest.raises(TaskRefusal):
        await operations.prepare(s.binding, "conversation", s.digest, immutable_read=immutable_read)
    # A second independent approval creates a new attempt, without altering
    # conversation consent. The drain proof comes from the trusted controller.
    identifier = await hold(s, "resource:" + "1" * 32, hashlib.sha256(b"resource").hexdigest())
    await s.state.settle(s.binding, 1, clean=True)
    assert await s.gate.resolve_request(identifier, decision="approved", resolved_by="dashboard")
    assert await s.state.resume_approval(s.task["id"])
    s.binding = await next_binding(s)
    if not immutable_read:
        with pytest.raises(TaskRefusal):
            await operations.prepare(
                s.binding, "conversation", s.digest, immutable_read=immutable_read
            )
    else:
        retry = await operations.prepare(s.binding, "conversation", s.digest, immutable_read=True)
        assert retry["id"] == operation["id"] and retry["status"] == "prepared"
        await operations.transition(s.binding, retry["id"], "executing")
        await operations.transition(
            s.binding, retry["id"], "completed", result={"text": "Fixture result."}
        )
        completed = await operations.prepare(
            s.binding, "conversation", s.digest, immutable_read=True
        )
        assert completed["status"] == "completed"
        await s.registry.grant("fixture", "conversation", "deny")
        with pytest.raises(TaskRefusal):
            await operations.prepare(s.binding, "conversation", s.digest, immutable_read=True)


async def test_frozen_migration_matches_canonical_lifecycle_schema(tmp_path):
    import importlib

    import aiosqlite

    names = ("peer_segments", "peer_task_runtime", "peer_task_consents", "peer_operations")
    migration = importlib.import_module("genesis.db.migrations.20261008124008_peer_lifecycle")
    async with (
        aiosqlite.connect(tmp_path / "migration.db") as migrated,
        aiosqlite.connect(tmp_path / "canonical.db") as canonical,
    ):
        await migration.up(migrated)
        for name in names:
            await canonical.execute(TABLES[name])
            for pragma in ("table_info", "foreign_key_list", "index_list"):
                assert (
                    await (await migrated.execute(f"PRAGMA {pragma}({name})")).fetchall()
                    == await (await canonical.execute(f"PRAGMA {pragma}({name})")).fetchall()
                )


@pytest.mark.parametrize("lifecycle", ["allow"], indirect=True)
async def test_completion_proof_survives_cleanup_without_new_execution_allowance(lifecycle):
    import time

    from genesis.db.crud import cc_sessions
    from genesis.peers.lifecycle_state import current

    s = lifecycle
    async with s.registry.connection() as db:
        await cc_sessions.create(
            db,
            id="completion-fixture",
            session_type="background_task",
            model="sonnet",
            source_tag="peer_api",
            started_at="2026-10-08T00:00:00+00:00",
            last_activity_at="2026-10-08T00:00:00+00:00",
        )
    await s.state.begin(s.binding, "completion-fixture")
    completed_at = time.time()
    await s.state.record_completion(s.binding, completed_at, 1.0)
    await s.state.settle(s.binding, 3600, clean=True)
    async with s.registry.connection() as db:
        segment = await (
            await db.execute(
                "SELECT * FROM peer_segments WHERE id=?", (s.binding.segment.segment_id,)
            )
        ).fetchone()
        assert segment["completed_at"] == completed_at and segment["execution_elapsed_s"] == 1.0
        assert (await current(db, s.task["id"], execution=False))["work_elapsed_s"] == 3600
        with pytest.raises(TaskRefusal):
            await current(db, s.task["id"])
    with pytest.raises(TaskRefusal):
        await s.state.record_completion(s.binding, completed_at, 1.0)


@pytest.mark.parametrize("change", ["working_dir", "deadline", "segment"])
async def test_authorization_refuses_mismatched_internal_binding(lifecycle, change):
    s = lifecycle
    _, _, binding = await approve(s)
    if change == "working_dir":
        binding = replace(binding, working_dir=binding.working_dir + "-changed")
    elif change == "deadline":
        binding = replace(
            binding, segment=replace(binding.segment, deadline_at=binding.segment.deadline_at + 1)
        )
    else:
        binding = replace(binding, segment=replace(binding.segment, segment_id="f" * 32))
    with pytest.raises(TaskRefusal):
        await s.state.consent(binding, "conversation", s.digest)


async def test_conversation_tools_use_request_consent_and_distinct_operation_receipts(lifecycle):
    import json

    s = lifecycle
    _, _, binding = await approve(s)
    operations = PeerOperationState(s.registry)
    identifiers = set()
    for name in ("task_context", "resources_list"):
        # Match the actual broker's invocation digest, which is distinct from
        # the exact accepted-request digest carried by conversation consent.
        digest = hashlib.sha256(
            json.dumps(
                {"operation": name, "arguments": {}, "resource_digest": None},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert digest != s.digest
        receipt = await operations.prepare(binding, "conversation", digest, immutable_read=True)
        identifiers.add(receipt["id"])
        await operations.transition(binding, receipt["id"], "executing")
        await operations.transition(
            binding, receipt["id"], "completed", result={"text": "Fixture."}
        )
    assert len(identifiers) == 2
    with pytest.raises(TaskRefusal):
        await operations.prepare(
            binding,
            "resource:" + "1" * 32,
            hashlib.sha256(b"unapproved version").hexdigest(),
            immutable_read=True,
        )
