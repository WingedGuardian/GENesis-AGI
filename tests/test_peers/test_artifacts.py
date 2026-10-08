"""Owned publication uses real lifecycle proofs, private files and SQLite receipts."""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.cc.peer_segment import PeerSegment
from genesis.db.crud import cc_sessions
from genesis.db.schema import INDEXES, TABLES
from genesis.peers import artifacts as module
from genesis.peers.artifacts import PeerArtifacts
from genesis.peers.digests import operation_digest
from genesis.peers.lifecycle_state import PeerLifecycleState
from genesis.peers.operation_state import PeerOperationState
from genesis.peers.protocol import task_view
from genesis.peers.resources import PublishedResources
from genesis.peers.session import PeerSessionBinding
from genesis.peers.tasks import PeerTasks, TaskRefusal


@pytest.fixture
async def publication(registry, tmp_path):
    async with registry.transaction() as db:
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
            "peer_resources",
            "peer_artifacts",
        ):
            await db.execute(TABLES[name])
        for sql in INDEXES:
            if " ON cc_rate_limit_parks" in sql:
                await db.execute(sql)
    await registry.configure("fallback", service_url="https://genesis.example/v1/agent/a2a")
    await registry.register(
        "fixture", same_owner=True, daily_allowance=10, token_name="GENESIS_PEER_FIXTURE_TOKEN"
    )
    await registry.grant("fixture", "conversation", "allow")
    resource = await PublishedResources(registry).publish("Fixture", "Published fixture bytes.")
    capability = "resource:" + resource["resource_id"]
    await registry.grant("fixture", capability, "allow")
    identity = await registry.get("fixture")
    tasks = PeerTasks(registry)
    task = await tasks.admit(
        identity, {"messageId": "result", "role": "ROLE_USER", "parts": [{"text": "Fixture."}]}
    )
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    state = PeerLifecycleState(registry, directory)
    claimed = await state.claim()
    work = Path(claimed["working_dir"])
    work.mkdir(mode=0o700)
    outputs = work / ".peer-results"
    outputs.mkdir(mode=0o700)
    session = str(uuid.uuid4())
    async with registry.connection() as db:
        await cc_sessions.create(
            db,
            id=session,
            session_type="background_task",
            model="sonnet",
            source_tag="peer_api",
            started_at="2026-10-08T00:00:00+00:00",
            last_activity_at="2026-10-08T00:00:00+00:00",
        )
    binding = PeerSessionBinding(
        task["id"],
        PeerSegment(
            claimed["segment_id"], claimed["deadline_at"], ("mcp__genesis_peer__resources_list",)
        ),
        claimed["generation"],
        str(work / "facade.json"),
        str(work),
    )
    await state.begin(binding, session)
    operations = PeerOperationState(registry)
    receipt = await operations.prepare(
        binding, "conversation", operation_digest("resources_list", {}), immutable_read=True
    )
    await operations.transition(binding, receipt["id"], "executing")
    async with registry.connection() as db:
        row = await (await db.execute("SELECT * FROM peer_resources")).fetchone()
    await operations.transition(
        binding,
        receipt["id"],
        "completed",
        result={"resources": [{key: row[key] for key in ("id", "title", "sha256")}]},
    )
    await state.record_completion(binding, time.time(), 1)
    await state.settle(binding, 1, clean=True)
    path = outputs / f"bg-session-{session}.md"
    path.write_text("Safe fixture answer.")
    path.chmod(0o600)
    yield SimpleNamespace(
        registry=registry,
        identity=identity,
        tasks=tasks,
        task=task,
        state=state,
        service=PeerArtifacts(registry, directory),
        binding=binding,
        session=session,
        path=path,
        capability=capability,
        receipt=receipt,
        result=dict(
            success=True,
            cleanup_confirmed=True,
            cancelled=False,
            expired=False,
            artifact_path=str(path),
            tools_summary={"mcp__genesis_peer__resources_list": 1},
        ),
    )


async def publish(s):
    await s.service.publish(s.binding, s.session, s.result)
    rows = await s.service.project(s.identity, [s.task])
    return rows[0]["_peer_result"]["artifact_id"]


async def attach_park(s):
    from genesis.db.crud import cc_rate_limit_parks as parks

    async with s.registry.transaction() as db:
        identifier = await parks.upsert_open_park(
            db,
            kind="direct_session",
            dedup_key="fixture",
            origin_session_id=None,
            limit_kind="session",
            raw_signal=None,
            reset_at=None,
            next_attempt_at="2026-10-08T00:00:00+00:00",
            commit=False,
            payload={
                "source_tag": "peer_api",
                "peer_task_id": s.task["id"],
                "epoch": s.task["epoch"],
                "generation": s.binding.generation,
                "segment_id": s.binding.segment.segment_id,
            },
        )
        await db.execute(
            "UPDATE cc_rate_limit_parks SET status='resuming',attempts=2 WHERE id=?", (identifier,)
        )
        await db.execute(
            "UPDATE peer_task_runtime SET park_id=? WHERE task_id=?", (identifier, s.task["id"])
        )
    return identifier


@pytest.mark.parametrize(
    "content", [b"", ("x" * (17 * 1024 * 1024)).encode(), ("界" * 2000).encode()]
)
async def test_full_bytes_empty_large_unicode_and_idempotent_publication(publication, content):
    s = publication
    s.path.write_bytes(content)
    identifier = await publish(s)
    assert await s.service.fetch(s.identity, s.task["id"], identifier) == content
    await s.service.publish(s.binding, s.session, s.result)
    row = (await s.service.project(s.identity, [s.task]))[0]
    wire = task_view(row)
    summary = content[:4096].decode("utf-8", errors="ignore")
    if summary:
        assert wire.status.message.parts[0].text == summary
    else:
        assert not wire.status.HasField("message")
    assert wire.artifacts[0].parts[0].url.endswith("/artifacts/" + identifier)
    async with s.registry.connection() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM peer_artifacts")).fetchone())[0] == 1
        stored = await (await db.execute("SELECT * FROM peer_tasks")).fetchone()
        assert stored["state"] == "completed" and stored["slot_reserved"] == 0


@pytest.mark.parametrize(
    "change", ["missing", "corrupt", "mode", "symlink", "fifo", "directory_mode"]
)
async def test_unsafe_backing_file_withholds_download_preserves_authorized_snapshot(
    publication, change
):
    s = publication
    identifier = await publish(s)
    if change in {"missing", "symlink", "fifo"}:
        s.path.unlink()
        if change == "symlink":
            target = s.path.parent / "other.md"
            target.write_text("Different fixture.")
            target.chmod(0o600)
            s.path.symlink_to(target)
        elif change == "fifo":
            os.mkfifo(s.path, 0o600)
    elif change == "corrupt":
        s.path.write_text("Changed fixture.")
    elif change == "mode":
        s.path.chmod(0o644)
    else:
        s.path.parent.chmod(0o755)
    with pytest.raises(TaskRefusal) as failure:
        await asyncio.wait_for(s.service.fetch(s.identity, s.task["id"], identifier), 5)
    assert failure.value.code == "result_not_ready"
    assert (await s.service.project(s.identity, [s.task]))[0]["_peer_result"][
        "summary"
    ] == "Safe fixture answer."


@pytest.mark.parametrize("decision", ["ask", "deny"])
async def test_metadata_resource_withdrawal_blocks_preview_and_download(publication, decision):
    s = publication
    identifier = await publish(s)
    await s.registry.grant("fixture", s.capability, decision)
    assert "_peer_result" not in (await s.service.project(s.identity, [s.task]))[0]
    with pytest.raises(TaskRefusal):
        await s.service.fetch(s.identity, s.task["id"], identifier)
    await s.registry.grant("fixture", s.capability, "allow")
    assert await s.service.fetch(s.identity, s.task["id"], identifier) == s.path.read_bytes()


@pytest.mark.parametrize("operation", ["publish", "fetch"])
async def test_withdrawal_during_file_read_rechecks_before_disclosure(
    publication, monkeypatch, operation
):
    s = publication
    identifier = await publish(s) if operation == "fetch" else None
    original = module.asyncio.to_thread

    async def read_then_withdraw(*args):
        content = await original(*args)
        await s.registry.grant("fixture", s.capability, "deny")
        return content

    monkeypatch.setattr(module.asyncio, "to_thread", read_then_withdraw)
    with pytest.raises(TaskRefusal):
        if operation == "publish":
            await s.service.publish(s.binding, s.session, s.result)
        else:
            await s.service.fetch(s.identity, s.task["id"], identifier)
    if operation == "publish":
        async with s.registry.connection() as db:
            assert not await (await db.execute("SELECT 1 FROM peer_artifacts")).fetchone()


@pytest.mark.parametrize(
    "change", ["late", "stale", "cancel", "expiry", "blocked", "unknown", "receipt"]
)
async def test_invalid_proof_or_provenance_cannot_publish(publication, change):
    s = publication
    async with s.registry.transaction() as db:
        if change == "late":
            await db.execute("UPDATE peer_segments SET completed_at=deadline_at+1")
        elif change == "stale":
            await db.execute("UPDATE peer_tasks SET generation=generation+1")
        elif change == "cancel":
            await db.execute("UPDATE peer_tasks SET cancel_requested=1")
        elif change == "expiry":
            await db.execute("UPDATE peer_tasks SET expires_at='2000-01-01T00:00:00+00:00'")
        elif change == "blocked":
            await db.execute("UPDATE peer_segments SET status='blocked'")
        elif change == "unknown":
            await db.execute("UPDATE peer_operations SET immutable_read=0,status='unknown'")
        else:
            await db.execute("UPDATE peer_operations SET operation_digest=?", ("f" * 64,))
    if change == "unknown":
        await s.service.publish(s.binding, s.session, s.result)
    else:
        with pytest.raises(TaskRefusal):
            await s.service.publish(s.binding, s.session, s.result)
    async with s.registry.connection() as db:
        assert not await (await db.execute("SELECT 1 FROM peer_artifacts")).fetchone()
        row = await (await db.execute("SELECT * FROM peer_tasks")).fetchone()
        assert row["state"] == "working"
        if change == "unknown":
            runtime = await (
                await db.execute("SELECT hold_reason FROM peer_task_runtime")
            ).fetchone()
            assert runtime["hold_reason"] == "reconciliation"


async def test_publication_retirement_failure_rolls_back_all_visible_state(
    publication, monkeypatch
):
    s = publication
    identifier = await attach_park(s)
    original = module.retire_park

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(module, "retire_park", fail)
    with pytest.raises(asyncio.CancelledError):
        await s.service.publish(s.binding, s.session, s.result)
    async with s.registry.connection() as db:
        assert not await (await db.execute("SELECT 1 FROM peer_artifacts")).fetchone()
        assert (await (await db.execute("SELECT state FROM peer_tasks")).fetchone())[0] == "working"
        park = await (
            await db.execute(
                "SELECT status,attempts FROM cc_rate_limit_parks WHERE id=?", (identifier,)
            )
        ).fetchone()
        assert tuple(park) == ("resuming", 2)
    assert s.path.read_text() == "Safe fixture answer."  # Retryable private orphan, not disclosed.


async def test_receipt_metadata_cannot_be_reinterpreted_as_read_authority(publication):
    s = publication
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_operations SET result_json=?",
            (json.dumps({"resources": [], "allow": True}),),
        )
    with pytest.raises(TaskRefusal):
        await s.service.publish(s.binding, s.session, s.result)


async def test_http_get_list_duplicate_send_and_owned_download_share_disclosure_gate(
    publication, monkeypatch
):
    import secrets

    from flask import Flask

    from genesis.dashboard.routes.agent_api import ROOT, agent_api_bp

    s = publication
    identifier = await publish(s)
    for name in tuple(os.environ):
        if name.startswith("GENESIS_") and name.endswith("_TOKEN"):
            monkeypatch.delenv(name)
    credential = secrets.token_urlsafe(32)
    monkeypatch.setenv("GENESIS_PEER_FIXTURE_TOKEN", credential)
    headers = {"Authorization": "Bearer " + credential, "A2A-Version": "1.0"}
    app = Flask("owned-results")
    app.config.update(
        GENESIS_EVENT_LOOP=asyncio.get_running_loop(),
        GENESIS_PEER_REGISTRY=s.registry,
        GENESIS_PEER_TASKS=s.tasks,
        GENESIS_PEER_RESULTS=s.service,
    )
    app.register_blueprint(agent_api_bp)
    client = app.test_client()
    download = ROOT + "/tasks/" + s.task["id"] + "/artifacts/" + identifier
    response = await asyncio.to_thread(client.get, download, headers=headers)
    assert response.status_code == 200 and response.data == s.path.read_bytes()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert (await asyncio.to_thread(client.get, download)).status_code == 401
    unknown = download.rsplit("/", 1)[0] + "/" + "f" * 32
    assert (await asyncio.to_thread(client.get, unknown, headers=headers)).status_code == 404
    for withdrawn in (False, True):
        if withdrawn:
            await s.registry.grant("fixture", s.capability, "deny")
        responses = [
            await asyncio.to_thread(client.get, ROOT + "/tasks/" + s.task["id"], headers=headers),
            await asyncio.to_thread(client.get, ROOT + "/tasks", headers=headers),
            await asyncio.to_thread(
                client.post,
                ROOT + "/message:send",
                headers=headers,
                json={
                    "message": json.loads(s.task["message_json"]),
                    "configuration": {"returnImmediately": True},
                },
            ),
        ]
        for response in responses:
            assert response.status_code == 200
            payload = response.get_json()
            wire = payload["tasks"][0] if "tasks" in payload else payload.get("task", payload)
            assert ("message" in wire["status"]) is not withdrawn
            assert ("Safe fixture answer." in response.get_data(as_text=True)) is not withdrawn
        if withdrawn:
            response = await asyncio.to_thread(client.get, download, headers=headers)
            assert response.status_code == 401 and "Safe fixture answer." not in response.get_data(
                as_text=True
            )


async def test_project_refreshes_rows_after_awaited_settings(publication, monkeypatch):
    s = publication
    await publish(s)
    original = s.registry.settings

    async def withdraw_before_batch():
        settings = await original()
        await s.registry.grant("fixture", s.capability, "deny")
        return settings

    monkeypatch.setattr(s.registry, "settings", withdraw_before_batch)
    assert "_peer_result" not in (await s.service.project(s.identity, [s.task, s.task]))[0]
    assert all(
        "_peer_result" not in row for row in await s.service.project(s.identity, [s.task, s.task])
    )


@pytest.mark.parametrize("operation", ["project", "fetch"])
@pytest.mark.parametrize("change", ["deny", "ask", "retire", "cancel", "generation", "epoch"])
async def test_final_disclosure_serializes_against_authority_writers(
    publication, monkeypatch, operation, change
):
    import sqlite3
    from contextlib import asynccontextmanager

    s = publication
    identifier = await publish(s)
    acquired = asyncio.Event()
    release = asyncio.Event()
    original = s.registry.transaction

    @asynccontextmanager
    async def pause_final_gate():
        async with original() as db:
            acquired.set()
            await release.wait()
            yield db

    monkeypatch.setattr(s.registry, "transaction", pause_final_gate)

    async def disclose():
        if operation == "project":
            return await s.service.project(s.identity, [s.task, s.task])
        return await s.service.fetch(s.identity, s.task["id"], identifier)

    async def withdraw(db):
        if change in {"deny", "ask"}:
            await db.execute(
                "UPDATE peer_grants SET decision=? WHERE capability=?", (change, s.capability)
            )
        elif change == "retire":
            await db.execute("UPDATE peer_resources SET active=0")
        elif change == "cancel":
            await db.execute("UPDATE peer_tasks SET cancel_requested=1")
        elif change == "generation":
            await db.execute("UPDATE peer_tasks SET generation=generation+1")
        else:
            await db.execute("UPDATE peers SET epoch=?", ("f" * 32,))

    response = asyncio.create_task(disclose())
    try:
        await asyncio.wait_for(acquired.wait(), 5)
        async with s.registry.connection() as writer:
            # Zero timeout makes the reservation check deterministic.
            await writer.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await writer.execute("BEGIN IMMEDIATE")
        release.set()
        result = await asyncio.wait_for(response, 5)
        if operation == "project":
            assert all("_peer_result" in row for row in result)
        else:
            assert result == b"Safe fixture answer."
        monkeypatch.setattr(s.registry, "transaction", original)
        async with original() as writer:
            await withdraw(writer)
        if operation == "project" and change != "epoch":
            assert all("_peer_result" not in row for row in await disclose())
        else:
            with pytest.raises(TaskRefusal):
                await disclose()
    finally:
        release.set()
        await asyncio.gather(response, return_exceptions=True)


@pytest.mark.parametrize("operation", ["publish", "project", "fetch"])
async def test_expiry_during_awaited_proof_withholds_disclosure(
    publication, monkeypatch, operation
):
    from datetime import datetime, timedelta

    s = publication
    identifier = await publish(s) if operation != "publish" else None
    original = module.result_authorized
    calls = 0

    async def prove_then_advance_clock(db, task):
        nonlocal calls
        await original(db, task)
        calls += 1
        # Project two rows: the first preview must be rechecked after later
        # row awaits, not just at its own original proof point.
        if calls == 2:
            expired = datetime.fromisoformat(task["expires_at"]) + timedelta(seconds=1)
            monkeypatch.setattr(module, "utcnow", lambda: expired)

    monkeypatch.setattr(module, "result_authorized", prove_then_advance_clock)
    if operation == "project":
        rows = await s.service.project(s.identity, [s.task, s.task])
        assert len(rows) == 2 and all("_peer_result" not in row for row in rows)
    else:
        with pytest.raises(TaskRefusal):
            if operation == "fetch":
                await s.service.fetch(s.identity, s.task["id"], identifier)
            else:
                await s.service.publish(s.binding, s.session, s.result)
        if operation == "publish":
            async with s.registry.connection() as db:
                assert not await (await db.execute("SELECT 1 FROM peer_artifacts")).fetchone()
                assert (await (await db.execute("SELECT state FROM peer_tasks")).fetchone())[
                    0
                ] == "working"


async def test_expiry_during_park_retirement_rolls_back_publication(publication, monkeypatch):
    from datetime import datetime, timedelta

    s = publication
    identifier = await attach_park(s)
    original = module.retire_park

    async def retire_then_advance_clock(db, task, **kwargs):
        await original(db, task, **kwargs)
        expired = datetime.fromisoformat(task["expires_at"]) + timedelta(seconds=1)
        monkeypatch.setattr(module, "utcnow", lambda: expired)

    monkeypatch.setattr(module, "retire_park", retire_then_advance_clock)
    with pytest.raises(TaskRefusal):
        await s.service.publish(s.binding, s.session, s.result)
    async with s.registry.connection() as db:
        assert not await (await db.execute("SELECT 1 FROM peer_artifacts")).fetchone()
        assert (await (await db.execute("SELECT state FROM peer_tasks")).fetchone())[0] == "working"
        park = await (
            await db.execute(
                "SELECT status,attempts FROM cc_rate_limit_parks WHERE id=?", (identifier,)
            )
        ).fetchone()
        assert tuple(park) == ("resuming", 2)


async def test_successful_publication_retires_stable_provider_park(publication):
    s = publication
    identifier = await attach_park(s)
    await publish(s)
    async with s.registry.connection() as db:
        park = await (
            await db.execute(
                "SELECT status,attempts FROM cc_rate_limit_parks WHERE id=?", (identifier,)
            )
        ).fetchone()
        assert tuple(park) == ("resumed", 2)
        assert (await (await db.execute("SELECT state FROM peer_tasks")).fetchone())[
            0
        ] == "completed"


async def test_frozen_artifact_migration_matches_canonical_schema(tmp_path):
    import importlib

    import aiosqlite

    migration = importlib.import_module("genesis.db.migrations.20261008170000_peer_artifacts")
    async with (
        aiosqlite.connect(tmp_path / "migrated.db") as migrated,
        aiosqlite.connect(tmp_path / "canonical.db") as canonical,
    ):
        await migration.up(migrated)
        await canonical.execute(TABLES["peer_artifacts"])
        for pragma in ("table_info", "foreign_key_list", "index_list"):
            assert (
                await (await migrated.execute(f"PRAGMA {pragma}(peer_artifacts)")).fetchall()
            ) == (await (await canonical.execute(f"PRAGMA {pragma}(peer_artifacts)")).fetchall())
