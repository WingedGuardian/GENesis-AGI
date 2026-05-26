"""Tests for system-computed ego focus summary."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.ego.computed_focus import compute_focus_summary


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    # Minimal schema for the tables compute_focus_summary queries
    await conn.execute("""
        CREATE TABLE ego_directives (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            ego_target TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT,
            resolution TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE ego_proposals (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            rank INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT,
            user_response TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE user_goals (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            priority TEXT NOT NULL DEFAULT 'medium',
            category TEXT DEFAULT 'project',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.commit()
    yield conn
    await conn.close()


async def test_empty_db_returns_fallback(db):
    """No directives, proposals, or goals → fallback message."""
    result = await compute_focus_summary(db, "ego_focus_summary")
    assert result == "no active directives or proposals"


async def test_with_directives(db):
    """Active directives appear in computed focus."""
    await db.execute(
        "INSERT INTO ego_directives (id, content, ego_target) "
        "VALUES ('d1', 'Propose Suki CDP application dispatch', 'user_ego')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "1 directive(s)" in result
    assert "Suki" in result


async def test_with_board_proposals(db):
    """Pending proposals appear in computed focus."""
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) "
        "VALUES ('p1', 'Publish 5th Medium article on stateless agents', 'pending')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "1 pending proposal(s)" in result
    assert "Medium" in result


async def test_with_goals(db):
    """User goals appear for user ego but not genesis ego."""
    await db.execute(
        "INSERT INTO user_goals (id, title, status, priority) "
        "VALUES ('g1', 'Suki AI application', 'active', 'high')"
    )
    await db.commit()

    # User ego sees goals
    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "goals:" in result
    assert "Suki" in result

    # Genesis ego does NOT see goals
    result = await compute_focus_summary(db, "genesis_ego_focus_summary")
    assert "goals:" not in result


async def test_approved_proposals(db):
    """Approved proposals awaiting dispatch appear."""
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) "
        "VALUES ('p1', 'Deploy monitoring fix', 'approved')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "1 approved awaiting dispatch" in result


async def test_combined_state(db):
    """Multiple state elements combined with semicolons."""
    await db.execute(
        "INSERT INTO ego_directives (id, content, ego_target) "
        "VALUES ('d1', 'Apply to Suki AI', 'user_ego')"
    )
    await db.execute(
        "INSERT INTO ego_proposals (id, content, status) "
        "VALUES ('p1', 'Research infrastructure costs', 'pending')"
    )
    await db.execute(
        "INSERT INTO user_goals (id, title, status, priority) "
        "VALUES ('g1', 'Panel preparation', 'active', 'high')"
    )
    await db.commit()

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert "directive" in result
    assert "proposal" in result
    assert "goals:" in result
    # Parts joined by semicolons
    assert ";" in result


async def test_db_error_returns_fallback(db):
    """DB errors produce graceful fallback, not exception."""
    await db.close()  # Break the connection

    result = await compute_focus_summary(db, "ego_focus_summary")
    assert result == "general system awareness"
