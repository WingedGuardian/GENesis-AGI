"""Durable admission races and rollback, using real registry connections."""

import asyncio

import pytest

from genesis.db.crud import direct_session_queue as queue
from genesis.db.schema import TABLES
from genesis.peers.tasks import PeerTasks, TaskRefusal


@pytest.fixture
async def tasks(registry):
    async with registry.connection() as db:
        for name in (
            "direct_session_queue",
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
        ):
            await db.execute(TABLES[name])
        await db.commit()
    await registry.register(
        "muse", same_owner=True, daily_allowance=10, token_name="GENESIS_PEER_ADMISSION_TOKEN"
    )
    await registry.grant("muse", "conversation", "allow")
    return PeerTasks(registry), await registry.get("muse")


def message(message_id="one", text="Please help"):
    return {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": text}]}


async def test_eight_identical_sends_share_one_receipt_quota_and_queue(tasks):
    service, identity = tasks
    rows = await asyncio.gather(*(service.admit(identity, message()) for _ in range(8)))
    assert len({row["id"] for row in rows}) == 1
    async with service.registry.connection() as db:
        for table in ("peer_tasks", "peer_receipts", "direct_session_queue"):
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 1
        assert (
            await (await db.execute("SELECT admissions FROM peer_daily_admissions")).fetchone()
        )[0] == 1
        assert await queue.claim_next(db) is None


async def test_changed_retry_conflicts_without_new_charge(tasks):
    service, identity = tasks
    await service.admit(identity, message())
    with pytest.raises(TaskRefusal) as error:
        await service.admit(identity, message(text="Changed intent"))
    assert (error.value.code, error.value.status) == ("state_conflict", 409)


async def test_queue_failure_rolls_back_all_admission_records(tasks, monkeypatch):
    service, identity = tasks
    real = queue.insert_prepared

    async def fail(*args):
        raise RuntimeError("injected insert failure")

    monkeypatch.setattr(queue, "insert_prepared", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await service.admit(identity, message())
    async with service.registry.connection() as db:
        for table in (
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
            "direct_session_queue",
        ):
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 0
    monkeypatch.setattr(queue, "insert_prepared", real)
    assert (await service.admit(identity, message()))["state"] == "submitted"


async def test_concurrency_limit_and_retry_at_capacity(tasks):
    service, identity = tasks
    first = await service.admit(identity, message("one"))
    await service.admit(identity, message("two"))
    assert (await service.admit(identity, message("one")))["id"] == first["id"]
    with pytest.raises(TaskRefusal) as error:
        await service.admit(identity, message("three"))
    assert error.value.status == 429


async def test_owned_ids_do_not_cross_peer_epoch_or_revocation(tasks):
    service, identity = tasks
    first = await service.admit(identity, message())
    assert (await service.owned(identity, first["id"]))["id"] == first["id"]
    for wrong in ({**identity, "peer_id": "other"}, {**identity, "epoch": "different"}):
        with pytest.raises(TaskRefusal) as error:
            await service.owned(wrong, first["id"])
        assert error.value.status == 404
    await service.registry.revoke("muse")
    with pytest.raises(TaskRefusal) as error:
        await service.owned(identity, first["id"])
    assert error.value.status == 404


@pytest.mark.parametrize("limit", [True, 0, -1, 7201, "3600", None])
async def test_work_limit_exact_integer_bounds(tasks, limit):
    service, identity = tasks
    with pytest.raises(TaskRefusal) as error:
        await service.admit(identity, message(), work_limit_s=limit)
    assert error.value.status == 400


async def test_daily_allowance_charges_new_receipts_only_and_resets_utc_day(tasks, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from genesis.peers import tasks as module

    service, identity = tasks
    async with service.registry.transaction() as db:
        await db.execute("UPDATE peers SET daily_allowance=1 WHERE peer_id='muse'")
    first = await service.admit(identity, message())
    await service.cancel(identity, first["id"])
    assert (await service.admit(identity, message()))["id"] == first["id"]
    with pytest.raises(TaskRefusal) as error:
        await service.admit(identity, message("two"))
    assert error.value.status == 429
    tomorrow = datetime.now(UTC) + timedelta(days=1)

    class Clock:
        @staticmethod
        def now(zone):
            return tomorrow

    monkeypatch.setattr(module, "datetime", Clock)
    assert (await service.admit(identity, message("two")))["id"] != first["id"]


async def test_global_slot_limit_includes_other_peer(tasks):
    service, identity = tasks
    await service.registry.register(
        "other",
        same_owner=False,
        daily_allowance=2,
        token_name="GENESIS_PEER_OTHER_ADMISSION_TOKEN",
    )
    await service.registry.grant("other", "conversation", "allow")
    await service.admit(identity, message())
    await service.admit(await service.registry.get("other"), message())
    with pytest.raises(TaskRefusal) as error:
        await service.admit(identity, message("two"))
    assert error.value.status == 429


async def test_context_requires_ownership_and_grants_are_snapshotted(tasks):
    service, identity = tasks
    with pytest.raises(TaskRefusal) as error:
        await service.admit(identity, {**message(), "contextId": "invented"})
    assert error.value.status == 404
    await service.registry.grant("muse", "research", "ask")
    first = await service.admit(identity, message())
    second = await service.admit(identity, {**message("two"), "contextId": first["context_id"]})
    assert second["context_id"] == first["context_id"]
    import json

    assert json.loads(first["grants_json"])["research"] == "ask"
    await service.registry.grant("muse", "conversation", "deny")
    for operation in (service.owned(identity, first["id"]), service.cancel(identity, first["id"])):
        with pytest.raises(TaskRefusal) as error:
            await operation
        assert error.value.status == 404
    with pytest.raises(TaskRefusal) as error:
        await service.admit(identity, message())
    assert error.value.status == 401


@pytest.mark.parametrize("state", ["completed", "failed", "rejected"])
async def test_terminal_cancel_refuses_and_claimed_cancel_retains_slot(tasks, state):
    service, identity = tasks
    first = await service.admit(identity, message())
    async with service.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_tasks SET state=?,slot_reserved=0 WHERE id=?", (state, first["id"])
        )
    with pytest.raises(TaskRefusal) as error:
        await service.cancel(identity, first["id"])
    assert (error.value.code, error.value.status) == ("state_conflict", 400)
    running = await service.admit(identity, message("running"))
    async with service.registry.transaction() as db:
        await db.execute(
            "UPDATE direct_session_queue SET status='claimed' WHERE id=?", (running["queue_id"],)
        )
    canceled = await service.cancel(identity, running["id"])
    assert canceled["slot_reserved"] == 1 and canceled["cancel_requested"] == 1
    assert canceled["state"] == "submitted" and canceled["generation"] == 1
    assert (await service.cancel(identity, running["id"]))["generation"] == 1


async def test_paging_is_bounded_and_cursor_bound_to_peer_and_epoch(tasks):
    service, identity = tasks
    ids = []
    for index in range(4):
        row = await service.admit(identity, message(str(index)))
        ids.append(row["id"])
        await service.cancel(identity, row["id"])
    first, cursor, total = await service.page(identity, page_size=2)
    last, end, same_total = await service.page(identity, page_size=2, page_token=cursor)
    assert [row["id"] for row in first + last] == ids and not end
    assert total == same_total == 4
    for wrong in ({**identity, "peer_id": "other"}, {**identity, "epoch": "different"}):
        with pytest.raises(TaskRefusal) as error:
            await service.page(wrong, page_token=cursor)
        assert error.value.status == 404
    for size in (True, 0, 101, -1):
        with pytest.raises(TaskRefusal):
            await service.page(identity, page_size=size)
    for token in ("bad!", "A" * 2049):
        with pytest.raises(TaskRefusal):
            await service.page(identity, page_token=token)


async def test_pending_cancel_is_idempotent_and_frees_admission_slot(tasks):
    service, identity = tasks
    first = await service.admit(identity, message())
    canceled = await service.cancel(identity, first["id"])
    assert canceled["state"] == "canceled" and canceled["slot_reserved"] == 0
    assert (await service.cancel(identity, first["id"]))["generation"] == 1
    async with service.registry.connection() as db:
        row = await queue.get_by_id(db, first["queue_id"])
        assert row["status"] == "failed"
    assert (await service.admit(identity, message("second")))["state"] == "submitted"


async def test_task_migration_matches_canonical_schema_without_committing(tmp_path):
    import importlib

    import aiosqlite

    migration = importlib.import_module("genesis.db.migrations.20261008051227_peer_tasks")
    schemas = []
    for fresh in (True, False):
        async with aiosqlite.connect(tmp_path / str(fresh)) as db:
            await db.execute("BEGIN")
            if fresh:
                for name in ("peer_tasks", "peer_receipts", "peer_daily_admissions"):
                    await db.execute(TABLES[name])
            else:
                await migration.up(db)
                await migration.up(db)
            assert db.in_transaction
            schemas.append(
                await (
                    await db.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                ).fetchall()
            )
            await db.rollback()
            assert not await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
    assert [(name, " ".join(sql.split())) for name, sql in schemas[0]] == [
        (name, " ".join(sql.split())) for name, sql in schemas[1]
    ]
