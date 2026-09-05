"""Prompt-time terminal-row repair (touch_terminal_session_row_sync).

Origin (measured 2026-09-04): last_activity_at == started_at on 98% of
4556 rows (nothing on the terminal path ever advanced it, starving the
reaper's idle clock), and filesystem adoption recorded LIVE sessions as
'completed'. The repair runs on every UserPromptSubmit via the heartbeat
path: advance the clock, reopen a non-active terminal row (the user IS
here), clear the terminal timestamp. Channel rows (id != cc_session_id)
and voice rows are never touched.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import cc_sessions as crud
from genesis.db.crud.cc_sessions import touch_terminal_session_row_sync
from genesis.db.schema import create_all_tables


@pytest.fixture
async def file_db(tmp_path):
    """A REAL file-backed DB — the sync helper opens by path, so :memory:
    can't be shared with it."""
    path = tmp_path / "genesis.db"
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn, str(path)
    await conn.close()


@pytest.mark.asyncio
async def test_reopens_adopted_completed_row(file_db):
    conn, path = file_db
    await crud.register_from_filesystem(
        conn,
        id="term-1",
        cc_session_id="term-1",
        started_at="2026-03-07T08:00:00+00:00",
        status="completed",
        completed_at="2026-03-07T08:00:00+00:00",
    )
    touch_terminal_session_row_sync(path, "term-1")
    row = await crud.get_by_id(conn, "term-1")
    assert row["status"] == "active"
    assert row["completed_at"] is None
    assert row["last_activity_at"] > "2026-03-07T08:00:00+00:00"


@pytest.mark.asyncio
async def test_advances_clock_on_active_row(file_db):
    conn, path = file_db
    await crud.register_from_filesystem(
        conn,
        id="term-2",
        cc_session_id="term-2",
        started_at="2026-03-07T08:00:00+00:00",
        status="active",
    )
    touch_terminal_session_row_sync(path, "term-2")
    row = await crud.get_by_id(conn, "term-2")
    assert row["last_activity_at"] > "2026-03-07T08:00:00+00:00"


@pytest.mark.asyncio
async def test_channel_row_untouched(file_db):
    """id != cc_session_id → SessionManager's territory, never repaired."""
    conn, path = file_db
    await crud.create(
        conn,
        id="chan-1",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="completed",
        started_at="2026-03-07T08:00:00+00:00",
        last_activity_at="2026-03-07T08:00:00+00:00",
    )
    await crud.update_cc_session_id(conn, "chan-1", cc_session_id="cc-uuid-x")
    touch_terminal_session_row_sync(path, "cc-uuid-x")
    row = await crud.get_by_id(conn, "chan-1")
    assert row["status"] == "completed"
    assert row["last_activity_at"] == "2026-03-07T08:00:00+00:00"


@pytest.mark.asyncio
async def test_voice_row_untouched(file_db):
    conn, path = file_db
    await crud.register_voice_session(
        conn,
        id="voice-1",
        started_at="2026-03-07T08:00:00+00:00",
    )
    await crud.update_status(conn, "voice-1", status="completed")
    touch_terminal_session_row_sync(path, "voice-1")
    row = await crud.get_by_id(conn, "voice-1")
    assert row["status"] == "completed"


def test_missing_db_is_silent(tmp_path):
    """Best-effort contract: no DB, no error, no delay."""
    touch_terminal_session_row_sync(str(tmp_path / "absent.db"), "x")


def test_failed_row_not_reopened(tmp_path):
    """'failed' is an in-process verdict (conversation/bridge error paths) —
    a later prompt in the same cc uuid doesn't un-fail it; only
    checkpointed/completed/expired (dark or adoption states) reopen."""
    # covered structurally: 'failed' is absent from the repair's status set.
    import inspect

    src = inspect.getsource(touch_terminal_session_row_sync)
    assert "'failed'" not in src.split("status IN")[1].split(")")[0]
