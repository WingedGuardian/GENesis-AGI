"""The host updates only rows registered by its actual SDK child."""

import pytest
from claude_agent_sdk import SystemMessage

from tests.test_live_agent.test_agent import (
    Harness,
    drain,
    owner_environment,  # noqa: F401
    result,
)


@pytest.mark.parametrize("persist_fails", [False, True])
async def test_model_and_checkpoint_follow_observed_id(tmp_path, monkeypatch, persist_fails):
    calls = []

    async def record(sid, model, closed):
        calls.append((sid, model, closed))

    h = Harness(tmp_path, session_recorder=record)
    h.state.write_text('{"session_id": "saved-but-not-initialized"}')
    if persist_fails:
        def fail(*args):
            raise OSError("disk unavailable")
        monkeypatch.setattr("genesis.live_agent.agent.atomic_write_text", fail)
    await h.agent.start()
    await h.feed(SystemMessage("init", {"session_id": "actual", "model": "model-v2"}))
    assert calls == [("actual", "model-v2", False)]
    await h.agent.close()
    assert calls == [("actual", "model-v2", False), ("actual", None, True)]
    await h.agent.close()
    assert len(calls) == 2


async def test_no_checkpoint_of_saved_id_without_init(tmp_path):
    calls = []

    async def record(*args):
        calls.append(args)

    h = Harness(tmp_path, session_recorder=record)
    h.state.write_text('{"session_id":"saved"}')
    await h.agent.start()
    await h.agent.close()
    assert calls == []


async def test_registration_failure_visible_and_retried(tmp_path):
    calls = []

    async def record(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("database unavailable")

    h = Harness(tmp_path, session_recorder=record)
    await h.agent.start()
    init = SystemMessage("init", {"session_id": "actual", "model": "model-v2"})
    await h.feed(init, init)
    assert calls == [("actual", "model-v2", False)] * 2
    assert any(e.kind == "error" and "registration" in e.text for e in drain(h.agent))
    await h.agent.close()
    assert calls[-1] == ("actual", None, True)


async def test_checkpoint_occurs_after_disconnect_and_preserves_cost(tmp_path, monkeypatch):
    import aiosqlite

    from genesis.db.crud.cc_sessions import register_terminal_session_sync
    from genesis.live_agent.registration import update_session_row

    dbpath = tmp_path / "rows.db"
    async with aiosqlite.connect(dbpath) as db:
        await db.execute("""CREATE TABLE cc_sessions (
            id TEXT PRIMARY KEY, cc_session_id TEXT, session_type TEXT, model TEXT,
            source_tag TEXT, status TEXT, cc_pid INTEGER, started_at TEXT,
            last_heartbeat TEXT, completed_at TEXT, checkpointed_at TEXT,
            cost_usd REAL DEFAULT 0, pid INTEGER, last_activity_at TEXT)""")
        await db.commit()
    register_terminal_session_sync(str(dbpath), "actual", pid=99999)
    async with aiosqlite.connect(dbpath) as db:
        await db.execute("UPDATE cc_sessions SET cost_usd=1.25")
        await db.commit()
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: dbpath)
    monkeypatch.setattr("genesis.cc.foreground_reaper._PROC_ROOT", str(tmp_path / "empty-proc"))
    h = Harness(tmp_path, session_recorder=update_session_row)
    await h.agent.start()
    await h.feed(SystemMessage("init", {"session_id": "actual", "model": "model-v2"}))
    original = h.client.disconnect

    async def disconnect():
        async with aiosqlite.connect(dbpath) as db:
            assert (await (await db.execute("SELECT status FROM cc_sessions")).fetchone())[0] == "active"
        await original()

    h.client.disconnect = disconnect
    await h.agent.close()
    async with aiosqlite.connect(dbpath) as db:
        row = await (await db.execute(
            "SELECT status, model, cost_usd, checkpointed_at FROM cc_sessions"
        )).fetchone()
    assert row[:3] == ("checkpointed", "model-v2", 1.25)
    assert row[3]


async def test_result_without_init_still_checkpoints_actual_row(tmp_path):
    calls = []

    async def record(*args):
        calls.append(args)

    h = Harness(tmp_path, session_recorder=record)
    await h.agent.start()
    await h.feed(result(session_id="actual"), None)
    await h.agent.close()
    assert calls[-1] == ("actual", None, True)


async def test_eof_alone_does_not_checkpoint_and_disconnect_failure_leaves_active(tmp_path):
    calls = []

    async def record(*args):
        calls.append(args)

    h = Harness(tmp_path, session_recorder=record)
    await h.agent.start()
    await h.feed(SystemMessage("init", {"session_id": "actual", "model": "m"}), None)
    assert calls == [("actual", "m", False)]

    async def fail():
        raise OSError("child teardown failed")

    h.client.disconnect = fail
    with pytest.raises(OSError, match="teardown"):
        await h.agent.close()
    assert calls == [("actual", "m", False)]
    assert any("checkpoint" in e.text for e in drain(h.agent))
    assert h.agent._host_lock is None


async def test_checkpoint_error_reported_before_releasing_lock(tmp_path):
    attempts = 0
    h = None

    async def record(sid, model, closed):
        nonlocal attempts
        if closed:
            attempts += 1
            assert h.agent._host_lock is not None
            if attempts == 1:
                raise OSError("database unavailable")

    h = Harness(tmp_path, session_recorder=record)
    await h.agent.start()
    await h.feed(SystemMessage("init", {"session_id": "actual", "model": "m"}))
    await h.agent.close()
    assert attempts == 1
    assert h.agent._host_lock is None
    assert any("registration" in e.text for e in drain(h.agent))


@pytest.mark.parametrize("pid", [None, 0, 1, 12345])
async def test_unconfirmed_process_death_keeps_row_active(db, monkeypatch, pid):
    from contextlib import asynccontextmanager

    from genesis.db.crud import cc_sessions
    from genesis.live_agent.registration import update_session_row

    await cc_sessions.create(
        db, id="actual", session_type="foreground", model="m",
        started_at="2026-01-01T00:00:00+00:00", last_activity_at="2026-01-01T00:00:00+00:00",
    )
    await db.execute(
        "UPDATE cc_sessions SET cc_session_id='actual', pid=? WHERE id='actual'", (pid,),
    )
    await db.commit()

    @asynccontextmanager
    async def connect(path, **kwargs):
        assert kwargs == {"existing_only": True}
        yield db

    monkeypatch.setattr("genesis.db.connection.connect_aiosqlite_rw", connect)
    monkeypatch.setattr("genesis.cc.foreground_reaper._pid_dead", lambda pid, row: False)
    with pytest.raises(RuntimeError, match="death unconfirmed"):
        await update_session_row("actual", None, True)
    assert (await (await db.execute("SELECT status FROM cc_sessions WHERE id='actual'")).fetchone())[0] == "active"


async def test_missing_database_is_not_created(tmp_path, monkeypatch):
    import sqlite3

    from genesis.live_agent.registration import update_session_row

    missing = tmp_path / "missing.db"
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: missing)
    with pytest.raises(sqlite3.OperationalError):
        await update_session_row("actual", "m", False)
    assert not missing.exists()
