"""Native SQLite writer/migration matrix; all stores are disposable."""

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db import peer_status
from genesis.db.migrations.runner import MigrationRunner
from genesis.db.schema import TABLES, create_all_tables

MIGRATION = "20261023045640_peer_status_clock"
OLD = "2026-01-02T03:04:05.006Z"
DOCUMENTS = [None, "broken", "null", "[]", "{}"] + [
    json.dumps({"native_state": state})
    for state in (None, True, 42, [], {}, "pending", "running", "cancelling", "succeeded", "failed", "cancelled", "unknown")
]


@pytest.fixture
async def clock_db():
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        for name in ("cc_sessions", "approval_requests"):
            await db.execute(TABLES[name])
        await peer_status.install_base(db)
        yield db


async def receipt(db, sid="one", status="active", document='{"native_state":"running"}'):
    await db.execute(
        "INSERT INTO cc_sessions(id,session_type,model,status,started_at,last_activity_at,source_tag,metadata) "
        "VALUES(?,'background_task','sonnet',?,?,?,'agent_api',?)",
        (sid, status, OLD, OLD, document),
    )


async def approval(db, aid="hold", sid="one", status="pending"):
    await db.execute(
        "INSERT INTO approval_requests(id,action_type,action_class,description,context,status) "
        "VALUES(?,'test','reversible','Synthetic hold',?,?)",
        (aid, json.dumps({"agent_session_id": sid}), status),
    )


async def projected(db, sid="one"):
    return await (await db.execute("SELECT a2a_state,a2a_status_at FROM cc_sessions WHERE id=?", (sid,))).fetchone()


@pytest.mark.parametrize("document", DOCUMENTS)
@pytest.mark.parametrize("status", ["active", "completed", "failed", "expired", "checkpointed"])
@pytest.mark.parametrize("pending", [False, True])
async def test_sql_state_matches_independent_contract(clock_db, document, status, pending):
    db = clock_db
    if pending:
        await approval(db)  # Approval-before-receipt is a real writer order.
    await receipt(db, status=status, document=document)
    try:
        meta = json.loads(document or "{}")
    except ValueError:
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    state = meta.get("native_state", "pending")
    if status == "active" and pending:
        expected = "auth_required"
    elif status == "active" and isinstance(state, str) and state in {"succeeded", "failed", "cancelled"}:
        expected = "working"
    else:
        expected = {"pending": "submitted", "succeeded": "completed", "failed": "failed", "cancelled": "canceled"}.get(state, "working") if isinstance(state, str) else "working"
    row = await projected(db)
    assert row[0] == expected and row[1].endswith("Z") and len(row[1]) == 24


@pytest.mark.parametrize("recursive", [0, 1])
async def test_transition_only_clock_and_all_approval_writers(clock_db, recursive):
    db = clock_db
    await db.execute(f"PRAGMA recursive_triggers={recursive}")
    await receipt(db)
    await receipt(db, "two")
    await db.execute("UPDATE cc_sessions SET a2a_status_at=?", (OLD,))
    await db.execute("UPDATE cc_sessions SET metadata=json_set(metadata,'$.heartbeat',1), last_activity_at='2030-01-01' WHERE id='one'")
    assert tuple(await projected(db)) == ("working", OLD)
    await approval(db)
    assert (await projected(db))[0] == "auth_required" and (await projected(db))[1] != OLD
    await db.execute("UPDATE cc_sessions SET a2a_status_at=? WHERE id='one'", (OLD,))
    await approval(db, "second")
    await db.execute("UPDATE approval_requests SET status='approved' WHERE id='hold'")
    assert tuple(await projected(db)) == ("auth_required", OLD)
    await db.execute("UPDATE approval_requests SET context=? WHERE id='second'", (json.dumps({"agent_session_id": "two"}),))
    assert (await projected(db))[0] == "working" and (await projected(db))[1] != OLD
    assert (await projected(db, "two"))[0] == "auth_required"
    await db.execute("UPDATE cc_sessions SET a2a_status_at=? WHERE id='two'", (OLD,))
    await db.execute("DELETE FROM approval_requests WHERE id='second'")
    assert (await projected(db, "two"))[0] == "working" and (await projected(db, "two"))[1] != OLD
    await approval(db, "bulk1")
    await approval(db, "bulk2")
    await db.execute("UPDATE approval_requests SET status='expired' WHERE status='pending'")
    assert (await projected(db))[0] == "working"
    await db.execute("UPDATE cc_sessions SET source_tag='foreground' WHERE id='one'")
    assert tuple(await projected(db)) == (None, None)
    await db.execute("UPDATE cc_sessions SET source_tag='agent_api' WHERE id='one'")
    assert (await projected(db))[0] == "working"


async def test_terminal_staging_last_approval_and_rollback(clock_db):
    db = clock_db
    await receipt(db)
    await approval(db)
    await db.execute("UPDATE cc_sessions SET metadata=json_set(metadata,'$.native_state','succeeded'),a2a_status_at=?", (OLD,))
    await db.execute("UPDATE approval_requests SET status='cancelled'")
    assert (await projected(db))[0] == "working" and (await projected(db))[1] != OLD
    await db.commit()
    before = tuple(await projected(db))
    await db.execute("UPDATE cc_sessions SET status='completed'")
    assert (await projected(db))[0] == "completed"
    await db.rollback()
    assert tuple(await projected(db)) == before


async def legacy(db):
    # Genuine old schema, not the new canonical DDL with mocked ALTER errors.
    ddl = TABLES["cc_sessions"].replace(
        "topic_updated_at TEXT,\n            a2a_state        TEXT,\n            a2a_status_at    TEXT", "topic_updated_at TEXT"
    )
    assert "a2a_state" not in ddl
    await db.execute(ddl)
    await db.execute(TABLES["approval_requests"])
    await db.execute(
        "INSERT INTO cc_sessions(id,session_type,model,status,source_tag,metadata,started_at,last_activity_at) "
        "VALUES('legacy','background_task','sonnet','completed','agent_api',?,?,'')",
        ('{"native_state":"succeeded"}', "2026-01-02T04:04:05.006+01:00"),
    )
    await db.commit()


@pytest.mark.parametrize("stamp,expected", [
    ("2026-01-31T23:59:59.999999Z", "2026-02-01T00:00:00.000Z"),
    ("2026-12-31T23:59:59.999999Z", "2027-01-01T00:00:00.000Z"),
    ("2026-12-31T23:59:59.999999+00:00", "2027-01-01T00:00:00.000Z"),
])
@pytest.mark.parametrize("envelope", ["base", "runner"])
async def test_legacy_microsecond_rollover_is_normalized(stamp, expected, envelope, monkeypatch):
    async with aiosqlite.connect(":memory:") as db:
        await legacy(db)
        await db.execute("UPDATE cc_sessions SET started_at=?", (stamp,))
        if envelope == "base":
            await peer_status.install_base(db)
        else:
            results = await (await runner(db, monkeypatch)).run_pending()
            assert results[0].success
        assert tuple(await projected(db, "legacy")) == ("completed", expected)
        await peer_status.install_base(db)
        assert tuple(await projected(db, "legacy")) == ("completed", expected)


async def runner(db, monkeypatch):
    r = MigrationRunner(db)
    path = Path(importlib.import_module("genesis.db.migrations." + MIGRATION).__file__)
    monkeypatch.setattr(r, "get_available", AsyncMock(return_value=[("20261023045640", MIGRATION, path)]))
    return r


@pytest.mark.parametrize("approval_first", [False, True])
async def test_numbered_before_base_is_completed_by_later_base(monkeypatch, approval_first):
    async with aiosqlite.connect(":memory:") as db:
        if approval_first:
            await db.execute(TABLES["approval_requests"])
            await db.commit()
        r = await runner(db, monkeypatch)
        results = await r.run_pending()
        assert len(results) == 1 and results[0].success
        assert await r.run_pending() == []
        assert not await (await db.execute("PRAGMA table_info(cc_sessions)")).fetchall()
        await create_all_tables(db)
        await receipt(db)
        assert (await projected(db))[0] == "working"
        await approval(db)
        assert (await projected(db))[0] == "auth_required"
        before = tuple(await projected(db))
        await create_all_tables(db)
        assert tuple(await projected(db)) == before
        triggers = await (await db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'peer_status_%'")).fetchall()
        assert len(triggers) == 5


@pytest.mark.parametrize("order", ["base_first", "runner_first"])
async def test_real_runner_and_base_upgrade_orders_are_idempotent(tmp_path, monkeypatch, order):
    async with aiosqlite.connect(tmp_path / "upgrade.db") as db:
        await legacy(db)
        r = await runner(db, monkeypatch)
        if order == "base_first":
            await peer_status.install_base(db)
        result = await r.run_pending()
        assert len(result) == 1 and result[0].success
        await peer_status.install_base(db)
        before = tuple(await projected(db, "legacy"))
        assert before == ("completed", OLD)
        await peer_status.install_base(db)
        assert await r.run_pending() == []
        assert tuple(await projected(db, "legacy")) == before
        await create_all_tables(db)
        assert tuple(await projected(db, "legacy")) == before


@pytest.mark.parametrize("envelope", ["base", "runner"])
async def test_install_failure_rolls_back_ddl_and_preserves_envelope(tmp_path, monkeypatch, envelope):
    async with aiosqlite.connect(tmp_path / "rollback.db") as db:
        await legacy(db)
        await db.execute("CREATE TABLE prior(x)")
        await db.commit()
        if envelope == "base":
            await db.execute("INSERT INTO prior VALUES(1)")
        execute = db.execute

        async def broken(sql, *args, **kwargs):
            if sql.startswith("CREATE TRIGGER"):
                raise RuntimeError("synthetic install failure")
            return await execute(sql, *args, **kwargs)

        monkeypatch.setattr(db, "execute", broken)
        if envelope == "base":
            with pytest.raises(RuntimeError, match="synthetic"):
                await peer_status.install_base(db)
            assert (await (await execute("SELECT count(*) FROM prior")).fetchone())[0] == 1
            assert db.in_transaction
        else:
            results = await (await runner(db, monkeypatch)).run_pending()
            assert len(results) == 1 and not results[0].success
            assert (await (await execute("SELECT count(*) FROM schema_migrations")).fetchone())[0] == 0
        columns = {r[1] for r in await (await execute("PRAGMA table_info(cc_sessions)")).fetchall()}
        assert "a2a_state" not in columns and "a2a_status_at" not in columns
