"""Tests for Phase 9 CRUD — approval_requests, task_states, cc_sessions thread_id."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import approval_requests, cc_sessions, task_states
from genesis.db.schema import create_all_tables


@pytest.fixture()
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# approval_requests CRUD
# ---------------------------------------------------------------------------

class TestApprovalRequests:
    async def test_create_and_get(self, db):
        aid = await approval_requests.create(
            db, id="ar-1", action_type="send_email",
            action_class="costly_reversible", description="Send reply",
        )
        assert aid == "ar-1"
        row = await approval_requests.get_by_id(db, "ar-1")
        assert row is not None
        assert row["action_class"] == "costly_reversible"
        assert row["status"] == "pending"

    async def test_list_pending(self, db):
        await approval_requests.create(
            db, id="ar-1", action_type="t", action_class="reversible",
            description="d",
        )
        await approval_requests.create(
            db, id="ar-2", action_type="t", action_class="irreversible",
            description="d",
        )
        pending = await approval_requests.list_pending(db)
        assert len(pending) == 2

    async def test_resolve(self, db):
        await approval_requests.create(
            db, id="ar-1", action_type="t", action_class="reversible",
            description="d",
        )
        ok = await approval_requests.resolve(
            db, "ar-1", status="approved",
            resolved_at="2026-03-14T12:00:00Z", resolved_by="user",
        )
        assert ok is True
        row = await approval_requests.get_by_id(db, "ar-1")
        assert row["status"] == "approved"
        assert row["resolved_by"] == "user"

    async def test_list_expired(self, db):
        await approval_requests.create(
            db, id="ar-1", action_type="t", action_class="reversible",
            description="d", timeout_at="2026-03-14T11:00:00Z",
        )
        await approval_requests.create(
            db, id="ar-2", action_type="t", action_class="reversible",
            description="d", timeout_at="2026-03-14T15:00:00Z",
        )
        expired = await approval_requests.list_expired(db, now="2026-03-14T12:00:00Z")
        assert len(expired) == 1
        assert expired[0]["id"] == "ar-1"

    async def test_expire_timed_out(self, db):
        await approval_requests.create(
            db, id="ar-1", action_type="t", action_class="reversible",
            description="d", timeout_at="2026-03-14T11:00:00Z",
        )
        count = await approval_requests.expire_timed_out(db, now="2026-03-14T12:00:00Z")
        assert count == 1
        row = await approval_requests.get_by_id(db, "ar-1")
        assert row["status"] == "expired"

    async def test_no_timeout_never_expires(self, db):
        """Requests with no timeout are never auto-expired."""
        await approval_requests.create(
            db, id="ar-1", action_type="t", action_class="irreversible",
            description="d", timeout_at=None,
        )
        count = await approval_requests.expire_timed_out(db, now="2099-01-01T00:00:00Z")
        assert count == 0

    async def test_delete(self, db):
        await approval_requests.create(
            db, id="ar-1", action_type="t", action_class="reversible",
            description="d",
        )
        assert await approval_requests.delete(db, "ar-1") is True
        assert await approval_requests.get_by_id(db, "ar-1") is None


# ---------------------------------------------------------------------------
# task_states CRUD
# ---------------------------------------------------------------------------

class TestTaskStates:
    async def test_create_and_get(self, db):
        tid = await task_states.create(
            db, task_id="task-1", description="Research topic X",
        )
        assert tid == "task-1"
        row = await task_states.get_by_id(db, "task-1")
        assert row is not None
        assert row["current_phase"] == "planning"

    async def test_update(self, db):
        await task_states.create(
            db, task_id="task-1", description="d",
        )
        ok = await task_states.update(
            db, "task-1",
            current_phase="verification",
            decisions='{"approach": "A"}',
            updated_at="2026-03-14T12:00:00Z",
        )
        assert ok is True
        row = await task_states.get_by_id(db, "task-1")
        assert row["current_phase"] == "verification"
        assert row["decisions"] == '{"approach": "A"}'

    async def test_get_by_session(self, db):
        await task_states.create(
            db, task_id="task-1", description="d", session_id="sess-1",
        )
        await task_states.create(
            db, task_id="task-2", description="d2", session_id="sess-1",
        )
        await task_states.create(
            db, task_id="task-3", description="d3", session_id="sess-2",
        )
        results = await task_states.get_by_session(db, "sess-1")
        assert len(results) == 2

    async def test_delete(self, db):
        await task_states.create(db, task_id="task-1", description="d")
        assert await task_states.delete(db, "task-1") is True
        assert await task_states.get_by_id(db, "task-1") is None

    async def test_update_no_fields_returns_false(self, db):
        await task_states.create(db, task_id="task-1", description="d")
        result = await task_states.update(db, "task-1")
        assert result is False


# ---------------------------------------------------------------------------
# cc_sessions — thread_id support
# ---------------------------------------------------------------------------

class TestCCSessionsThreadId:
    async def test_create_with_thread_id(self, db):
        await cc_sessions.create(
            db, id="sess-1", session_type="foreground", model="sonnet",
            started_at="2026-03-14T10:00:00Z",
            last_activity_at="2026-03-14T10:00:00Z",
            user_id="user-1", channel="telegram", thread_id="topic-42",
        )
        row = await cc_sessions.get_by_id(db, "sess-1")
        assert row["thread_id"] == "topic-42"

    async def test_get_active_foreground_with_thread(self, db):
        """Sessions with different thread_ids are isolated."""
        await cc_sessions.create(
            db, id="sess-1", session_type="foreground", model="sonnet",
            started_at="2026-03-14T10:00:00Z",
            last_activity_at="2026-03-14T10:00:00Z",
            user_id="user-1", channel="telegram", thread_id="topic-1",
        )
        await cc_sessions.create(
            db, id="sess-2", session_type="foreground", model="opus",
            started_at="2026-03-14T10:00:00Z",
            last_activity_at="2026-03-14T10:00:00Z",
            user_id="user-1", channel="telegram", thread_id="topic-2",
        )

        # Find topic-1 session
        s1 = await cc_sessions.get_active_foreground(
            db, user_id="user-1", channel="telegram", thread_id="topic-1",
        )
        assert s1 is not None
        assert s1["id"] == "sess-1"

        # Find topic-2 session
        s2 = await cc_sessions.get_active_foreground(
            db, user_id="user-1", channel="telegram", thread_id="topic-2",
        )
        assert s2 is not None
        assert s2["id"] == "sess-2"

    async def test_get_active_foreground_null_thread(self, db):
        """thread_id=None matches sessions with NULL thread_id (backward compat)."""
        await cc_sessions.create(
            db, id="sess-1", session_type="foreground", model="sonnet",
            started_at="2026-03-14T10:00:00Z",
            last_activity_at="2026-03-14T10:00:00Z",
            user_id="user-1", channel="terminal",
        )
        row = await cc_sessions.get_active_foreground(
            db, user_id="user-1", channel="terminal", thread_id=None,
        )
        assert row is not None
        assert row["id"] == "sess-1"

    async def test_thread_id_isolates_from_null(self, db):
        """A session with thread_id is NOT found when querying thread_id=None."""
        await cc_sessions.create(
            db, id="sess-1", session_type="foreground", model="sonnet",
            started_at="2026-03-14T10:00:00Z",
            last_activity_at="2026-03-14T10:00:00Z",
            user_id="user-1", channel="telegram", thread_id="topic-1",
        )
        row = await cc_sessions.get_active_foreground(
            db, user_id="user-1", channel="telegram", thread_id=None,
        )
        assert row is None

    async def test_update_rate_limit(self, db):
        await cc_sessions.create(
            db, id="sess-1", session_type="foreground", model="sonnet",
            started_at="2026-03-14T10:00:00Z",
            last_activity_at="2026-03-14T10:00:00Z",
        )
        ok = await cc_sessions.update_rate_limit(
            db, "sess-1",
            rate_limited_at="2026-03-14T12:00:00Z",
            rate_limit_resumes_at="2026-03-14T14:00:00Z",
        )
        assert ok is True
        row = await cc_sessions.get_by_id(db, "sess-1")
        assert row["rate_limited_at"] == "2026-03-14T12:00:00Z"
        assert row["rate_limit_resumes_at"] == "2026-03-14T14:00:00Z"
