"""Tests for inbox_digest MCP tool and supporting CRUD functions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import follow_ups as fu_crud
from genesis.db.crud import inbox_items as inbox_crud


@pytest.fixture
async def db():
    """In-memory DB with follow_ups and inbox_items tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("""CREATE TABLE follow_ups (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_session TEXT,
        content TEXT NOT NULL,
        reason TEXT,
        strategy TEXT,
        scheduled_at TEXT,
        status TEXT DEFAULT 'pending',
        linked_task_id TEXT,
        priority TEXT DEFAULT 'medium',
        created_at TEXT NOT NULL,
        completed_at TEXT,
        resolution_notes TEXT,
        blocked_reason TEXT,
        escalated_to TEXT,
        verified_at TEXT,
        verification_notes TEXT,
        pinned INTEGER DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'follow_up',
        domain TEXT,
        goal_id TEXT,
        dedup_key TEXT
    )""")
    await conn.execute("""CREATE TABLE inbox_items (
        id TEXT PRIMARY KEY,
        file_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        batch_id TEXT,
        response_path TEXT,
        created_at TEXT NOT NULL,
        processed_at TEXT,
        error_message TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        evaluated_content TEXT,
        drop_id TEXT,
        batch_items TEXT
    )""")
    await conn.commit()
    yield conn
    await conn.close()


def _now_iso(offset_days: int = 0) -> str:
    dt = datetime.now(UTC) + timedelta(days=offset_days)
    return dt.isoformat()


class TestGetBySource:
    async def test_returns_matching_source(self, db):
        await fu_crud.create(
            db, content="inbox item", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )
        await fu_crud.create(
            db, content="user item", source="foreground_session",
            reason="test", strategy="user_input_needed",
        )

        results = await fu_crud.get_by_source(db, "inbox_evaluation")
        assert len(results) == 1
        assert results[0]["source"] == "inbox_evaluation"

    async def test_filters_by_status(self, db):
        fid = await fu_crud.create(
            db, content="completed", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )
        await fu_crud.update_status(db, fid, "completed", resolution_notes="done")
        await fu_crud.create(
            db, content="pending", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )

        pending = await fu_crud.get_by_source(db, "inbox_evaluation", status="pending")
        assert len(pending) == 1
        assert pending[0]["content"] == "pending"

    async def test_empty_for_unknown_source(self, db):
        results = await fu_crud.get_by_source(db, "nonexistent")
        assert results == []


class TestGetRecentlyResolved:
    async def test_returns_completed_in_window(self, db):
        fid = await fu_crud.create(
            db, content="resolved item", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )
        await fu_crud.update_status(db, fid, "completed", resolution_notes="ego resolved")

        results = await fu_crud.get_recently_resolved(
            db, source="inbox_evaluation", days=7,
        )
        assert len(results) == 1
        assert results[0]["content"] == "resolved item"

    async def test_excludes_pending(self, db):
        await fu_crud.create(
            db, content="still pending", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )

        results = await fu_crud.get_recently_resolved(
            db, source="inbox_evaluation", days=7,
        )
        assert results == []

    async def test_days_zero_guard(self, db):
        """days=0 should be clamped to 1, not return empty."""
        fid = await fu_crud.create(
            db, content="just resolved", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )
        await fu_crud.update_status(db, fid, "completed")

        results = await fu_crud.get_recently_resolved(
            db, source="inbox_evaluation", days=0,
        )
        # Should find it (days clamped to 1, item was just completed)
        assert len(results) == 1


class TestGetRecentCompleted:
    async def test_returns_completed_with_response(self, db):
        now = _now_iso()
        await db.execute(
            """INSERT INTO inbox_items
               (id, file_path, content_hash, status, response_path, created_at, processed_at)
               VALUES (?, ?, ?, 'completed', ?, ?, ?)""",
            (str(uuid.uuid4()), "/inbox/Genesis.md", "abc123",
             "/inbox/Genesis-1.genesis.md", now, now),
        )
        await db.commit()

        results = await inbox_crud.get_recent_completed(db, days=7)
        assert len(results) == 1
        assert results[0]["response_path"] == "/inbox/Genesis-1.genesis.md"

    async def test_excludes_no_response(self, db):
        now = _now_iso()
        await db.execute(
            """INSERT INTO inbox_items
               (id, file_path, content_hash, status, created_at, processed_at)
               VALUES (?, ?, ?, 'completed', ?, ?)""",
            (str(uuid.uuid4()), "/inbox/Genesis.md", "abc123", now, now),
        )
        await db.commit()

        results = await inbox_crud.get_recent_completed(db, days=7)
        assert results == []

    async def test_excludes_old_items(self, db):
        old = _now_iso(offset_days=-30)
        await db.execute(
            """INSERT INTO inbox_items
               (id, file_path, content_hash, status, response_path, created_at, processed_at)
               VALUES (?, ?, ?, 'completed', ?, ?, ?)""",
            (str(uuid.uuid4()), "/inbox/Genesis.md", "abc123",
             "/inbox/Genesis-1.genesis.md", old, old),
        )
        await db.commit()

        results = await inbox_crud.get_recent_completed(db, days=7)
        assert results == []


class TestGetRecentSourceMode:
    async def test_mine_returns_foreground_only(self, db):
        await fu_crud.create(
            db, content="user item", source="foreground_session",
            reason="test", strategy="user_input_needed",
        )
        await fu_crud.create(
            db, content="system item", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )

        mine = await fu_crud.get_recent(db, source_mode="mine")
        assert len(mine) == 1
        assert mine[0]["source"] == "foreground_session"

    async def test_system_excludes_foreground(self, db):
        await fu_crud.create(
            db, content="user item", source="foreground_session",
            reason="test", strategy="user_input_needed",
        )
        await fu_crud.create(
            db, content="system item", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )

        system = await fu_crud.get_recent(db, source_mode="system")
        assert len(system) == 1
        assert system[0]["source"] == "inbox_evaluation"

    async def test_all_returns_everything(self, db):
        await fu_crud.create(
            db, content="user", source="foreground_session",
            reason="test", strategy="user_input_needed",
        )
        await fu_crud.create(
            db, content="system", source="inbox_evaluation",
            reason="test", strategy="ego_judgment",
        )

        all_items = await fu_crud.get_recent(db, source_mode="all")
        assert len(all_items) == 2


class TestFormatDigest:
    def test_format_with_data(self):
        from genesis.mcp.health.inbox_digest import _format_digest

        pending = [
            {"priority": "high", "content": "[ADOPT] Tool X: Do thing", "strategy": "user_input_needed"},
            {"priority": "low", "content": "[WATCH] Tool Y: Bookmark", "strategy": "ego_judgment"},
        ]
        evals = [
            {"created_at": "2026-06-06", "file_path": "/inbox/Genesis.md",
             "response_path": "/inbox/Genesis-53.genesis.md"},
        ]

        output = _format_digest(pending, [], evals, 7)
        assert "## Inbox Digest" in output
        assert "Pending Action (2 items)" in output
        assert "[ADOPT] Tool X" in output
        assert "Evaluations (1 completed)" in output
        assert "Genesis.md" in output

    def test_format_empty(self):
        from genesis.mcp.health.inbox_digest import _format_digest

        output = _format_digest([], [], [], 7)
        assert "No inbox activity" in output
