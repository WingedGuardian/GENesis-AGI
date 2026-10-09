"""Bounded peer/owner park selection uses one persisted-payload classifier."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc.rate_limit_resume import run_resume_tick
from genesis.db.crud import cc_rate_limit_parks as parks
from genesis.peers.provider_state import peer_park


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("{}", False),
        ('{"source_tag":"peer_api"}', True),
        ('{"peer_task_id":null}', True),
        ("{", False),
        ("[]", False),
        ("null", False),
        (None, False),
        ('{"source_tag":"peer_api","source_tag":"direct_session"}', False),
        ('{"source_tag":"direct_session","source_tag":"peer_api"}', True),
        ('{"source_tag":"peer_api","detail":NaN}', True),
        ("[" * 2000 + "]" * 2000, False),
    ],
    ids=[
        "legacy",
        "source",
        "null_key",
        "malformed",
        "array",
        "null",
        "missing",
        "duplicate_legacy",
        "duplicate_peer",
        "nan",
        "deep",
    ],
)
async def test_python_and_sql_classification_agree(db, payload, expected):
    assert peer_park({"payload_json": payload}) is expected
    await parks.list_due(db, peer_only=False)
    row = await (await db.execute("SELECT genesis_peer_park(?)", (payload,))).fetchone()
    assert bool(row[0]) is expected


async def seed(db, count=4, *, status="parked"):
    identifiers = []
    for i in range(count + 1):
        identifier = await parks.upsert_open_park(
            db,
            kind="direct_session",
            dedup_key=f"fixture-{i}",
            payload={"source_tag": "peer_api"} if i < count else {"prompt": "Fixture owner work"},
            origin_session_id=None,
            limit_kind="session",
            raw_signal=None,
            reset_at=None,
            next_attempt_at="2000-01-01T00:00:00+00:00",
        )
        await db.execute(
            "UPDATE cc_rate_limit_parks SET status=?,created_at=? WHERE id=?",
            (status, f"2000-01-01T00:00:{i:02d}+00:00", identifier),
        )
        identifiers.append(identifier)
    await db.commit()
    return identifiers


@pytest.mark.parametrize("status", ["parked", "needs_user"])
async def test_selection_filters_before_limit_and_preserves_default(db, status):
    identifiers = await seed(db, status=status)

    async def select(**kwargs):
        if status == "parked":
            return await parks.list_due(db, limit=2, **kwargs)
        return await parks.list_by_status(db, status=status, limit=2, **kwargs)

    assert [row["id"] for row in await select()] == identifiers[:2]
    assert not getattr(db, "_genesis_peer_park_registered", False)
    assert [row["id"] for row in await select(peer_only=True)] == identifiers[:2]
    assert [row["id"] for row in await select(peer_only=False)] == identifiers[-1:]


async def test_concurrent_selection_registers_once_with_active_cursor(db, monkeypatch):
    identifiers = await seed(db)
    define = AsyncMock(wraps=db.create_function)
    monkeypatch.setattr(db, "create_function", define)
    rows = await asyncio.gather(*(parks.list_due(db, peer_only=False) for _ in range(32)))
    assert all([row["id"] for row in result] == identifiers[-1:] for result in rows)
    define.assert_awaited_once()
    cursor = await db.execute("SELECT genesis_peer_park(payload_json) FROM cc_rate_limit_parks")
    try:
        await cursor.fetchone()  # Keep a statement using the function active.
        assert len(await parks.list_due(db, peer_only=True)) == 4
        define.assert_awaited_once()
    finally:
        await cursor.close()


async def test_cancelled_registration_retries_before_query(db, monkeypatch):
    identifiers = await seed(db)
    entered, release = asyncio.Event(), asyncio.Event()
    original = db.create_function
    calls = 0

    async def define(*args, **kwargs):
        nonlocal calls
        calls += 1
        await original(*args, **kwargs)
        if calls == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(db, "create_function", define)
    first = asyncio.create_task(parks.list_due(db, peer_only=False))
    await asyncio.wait_for(entered.wait(), 10)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not getattr(db, "_genesis_peer_park_registered", False)
    assert not db._genesis_peer_park_lock.locked()
    release.set()
    assert [row["id"] for row in await parks.list_due(db, peer_only=False)] == identifiers[-1:]
    assert calls == 2


@pytest.mark.parametrize("controller", ["missing", "refused"])
async def test_blocked_peer_parks_do_not_hide_owner_due_work(db, monkeypatch, controller):
    identifiers = await seed(db, count=51)
    runtime = MagicMock()
    runtime._db = db
    runtime.record_job_success = MagicMock()
    runtime.record_job_failure = MagicMock()
    runtime._peer_session_lifecycle = (
        None
        if controller == "missing"
        else MagicMock(resume_provider=AsyncMock(return_value=False))
    )
    dispatch = AsyncMock()
    monkeypatch.setattr("genesis.cc.rate_limit_resume_config.effective_mode", lambda: "live")
    monkeypatch.setattr("genesis.cc.rate_limit_resume._redispatch", dispatch)
    await run_resume_tick(runtime, now=datetime(2026, 10, 9, tzinfo=UTC))
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[1]["id"] == identifiers[-1]
    assert (await parks.get_by_id(db, identifiers[0]))["status"] == "parked"
    runtime.record_job_failure.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    ["{", "[" * 10000 + "]" * 10000, '{"prompt":' + "1" * 5000 + "}"],
    ids=["malformed", "deep", "integer"],
)
async def test_peer_needs_user_backlog_does_not_hide_corrupt_owner_alert(db, monkeypatch, payload):
    identifiers = await seed(db, count=51, status="needs_user")
    await db.execute(
        "UPDATE cc_rate_limit_parks SET payload_json=? WHERE id=?",
        (payload, identifiers[-1]),
    )
    await db.commit()
    runtime = MagicMock()
    runtime._db = db
    runtime._outreach_pipeline.submit = AsyncMock()
    monkeypatch.setattr("genesis.cc.rate_limit_resume_config.effective_mode", lambda: "live")
    await run_resume_tick(runtime, now=datetime(2026, 10, 9, tzinfo=UTC))
    runtime._outreach_pipeline.submit.assert_awaited_once()
    runtime.record_job_failure.assert_not_called()


async def test_corrupt_due_owner_park_accumulates_attempts(db, monkeypatch):
    identifiers = await seed(db, count=0)
    await db.execute(
        "UPDATE cc_rate_limit_parks SET payload_json='{' WHERE id=?", (identifiers[0],)
    )
    await db.commit()
    runtime = MagicMock()
    runtime._db = db
    runtime._outreach_pipeline.submit = AsyncMock()
    monkeypatch.setattr("genesis.cc.rate_limit_resume_config.effective_mode", lambda: "live")
    await run_resume_tick(runtime, now=datetime(2026, 10, 9, tzinfo=UTC))
    row = await parks.get_by_id(db, identifiers[0])
    assert row["status"] == "parked" and row["attempts"] == 1
    runtime.record_job_failure.assert_not_called()
