"""Tests for ego_source filtering — proposals visible only to their owning ego.

Phase 1a of the ego architecture fix: each ego (genesis / user) must only
see its own proposals in history and board sections.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EGO_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS ego_proposals (
    id               TEXT PRIMARY KEY,
    action_type      TEXT NOT NULL,
    action_category  TEXT NOT NULL DEFAULT '',
    content          TEXT NOT NULL,
    rationale        TEXT NOT NULL DEFAULT '',
    confidence       REAL NOT NULL DEFAULT 0.0,
    urgency          TEXT NOT NULL DEFAULT 'normal',
    alternatives     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    user_response    TEXT,
    cycle_id         TEXT,
    batch_id         TEXT,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    expires_at       TEXT,
    rank             INTEGER,
    execution_plan   TEXT,
    recurring        INTEGER DEFAULT 0,
    memory_basis     TEXT DEFAULT '',
    realist_verdict  TEXT,
    realist_reasoning TEXT,
    ego_source       TEXT,
    goal_id          TEXT,
    content_hash     TEXT,
    original_content TEXT,
    content_size     INTEGER,
    expected_outputs TEXT
)
"""


@pytest.fixture
async def db():
    """In-memory DB with the ego_proposals table."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_EGO_PROPOSALS_DDL)
        await conn.commit()
        yield conn


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _recent_iso(hours_ago: int = 1) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


async def _insert_proposal(
    db: aiosqlite.Connection,
    *,
    id: str,
    ego_source: str | None,
    status: str = "pending",
    action_type: str = "research",
    content: str = "test proposal",
    rank: int | None = None,
    created_at: str | None = None,
    resolved_at: str | None = None,
    realist_verdict: str | None = None,
    realist_reasoning: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO ego_proposals "
        "(id, action_type, content, status, ego_source, rank, created_at, "
        "resolved_at, realist_verdict, realist_reasoning) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
            action_type,
            content,
            status,
            ego_source,
            rank,
            created_at or _recent_iso(),
            resolved_at,
            realist_verdict,
            realist_reasoning,
        ),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# CRUD function tests
# ---------------------------------------------------------------------------


class TestListProposalsEgoSource:
    @pytest.mark.asyncio
    async def test_filter_returns_matching_source(self, db):
        await _insert_proposal(db, id="g1", ego_source="genesis_ego_cycle")
        await _insert_proposal(db, id="u1", ego_source="user_ego_cycle")

        genesis_results = await ego_crud.list_proposals(db, ego_source="genesis_ego_cycle")
        assert len(genesis_results) == 1
        assert genesis_results[0]["id"] == "g1"

        user_results = await ego_crud.list_proposals(db, ego_source="user_ego_cycle")
        assert len(user_results) == 1
        assert user_results[0]["id"] == "u1"

    @pytest.mark.asyncio
    async def test_null_ego_source_visible_to_both(self, db):
        await _insert_proposal(db, id="legacy", ego_source=None)
        await _insert_proposal(db, id="g1", ego_source="genesis_ego_cycle")

        genesis_results = await ego_crud.list_proposals(db, ego_source="genesis_ego_cycle")
        ids = {r["id"] for r in genesis_results}
        assert "legacy" in ids
        assert "g1" in ids

        user_results = await ego_crud.list_proposals(db, ego_source="user_ego_cycle")
        ids = {r["id"] for r in user_results}
        assert "legacy" in ids
        assert "g1" not in ids

    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self, db):
        await _insert_proposal(db, id="g1", ego_source="genesis_ego_cycle")
        await _insert_proposal(db, id="u1", ego_source="user_ego_cycle")
        await _insert_proposal(db, id="legacy", ego_source=None)

        all_results = await ego_crud.list_proposals(db)
        assert len(all_results) == 3

    @pytest.mark.asyncio
    async def test_filter_with_status(self, db):
        await _insert_proposal(db, id="g1", ego_source="genesis_ego_cycle", status="approved")
        await _insert_proposal(db, id="g2", ego_source="genesis_ego_cycle", status="pending")
        await _insert_proposal(db, id="u1", ego_source="user_ego_cycle", status="approved")

        results = await ego_crud.list_proposals(
            db, status="approved", ego_source="genesis_ego_cycle"
        )
        assert len(results) == 1
        assert results[0]["id"] == "g1"


class TestGetPendingQueueEgoSource:
    @pytest.mark.asyncio
    async def test_filter_returns_matching_source(self, db):
        await _insert_proposal(db, id="g1", ego_source="genesis_ego_cycle", rank=1)
        await _insert_proposal(db, id="u1", ego_source="user_ego_cycle", rank=2)

        genesis_results = await ego_crud.get_pending_queue(db, ego_source="genesis_ego_cycle")
        assert len(genesis_results) == 1
        assert genesis_results[0]["id"] == "g1"

    @pytest.mark.asyncio
    async def test_null_ego_source_visible_to_both(self, db):
        await _insert_proposal(db, id="legacy", ego_source=None, rank=1)

        genesis_results = await ego_crud.get_pending_queue(db, ego_source="genesis_ego_cycle")
        assert len(genesis_results) == 1
        assert genesis_results[0]["id"] == "legacy"

        user_results = await ego_crud.get_pending_queue(db, ego_source="user_ego_cycle")
        assert len(user_results) == 1
        assert user_results[0]["id"] == "legacy"

    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self, db):
        await _insert_proposal(db, id="g1", ego_source="genesis_ego_cycle")
        await _insert_proposal(db, id="u1", ego_source="user_ego_cycle")

        all_results = await ego_crud.get_pending_queue(db)
        assert len(all_results) == 2


class TestGetTabledEgoSource:
    @pytest.mark.asyncio
    async def test_filter_returns_matching_source(self, db):
        await _insert_proposal(
            db, id="g1", ego_source="genesis_ego_cycle", status="tabled",
            resolved_at=_now_iso(),
        )
        await _insert_proposal(
            db, id="u1", ego_source="user_ego_cycle", status="tabled",
            resolved_at=_now_iso(),
        )

        genesis_results = await ego_crud.get_tabled(db, ego_source="genesis_ego_cycle")
        assert len(genesis_results) == 1
        assert genesis_results[0]["id"] == "g1"

        user_results = await ego_crud.get_tabled(db, ego_source="user_ego_cycle")
        assert len(user_results) == 1
        assert user_results[0]["id"] == "u1"

    @pytest.mark.asyncio
    async def test_null_ego_source_visible_to_both(self, db):
        await _insert_proposal(
            db, id="legacy", ego_source=None, status="tabled",
            resolved_at=_now_iso(),
        )

        genesis_results = await ego_crud.get_tabled(db, ego_source="genesis_ego_cycle")
        assert len(genesis_results) == 1

        user_results = await ego_crud.get_tabled(db, ego_source="user_ego_cycle")
        assert len(user_results) == 1

    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self, db):
        await _insert_proposal(
            db, id="g1", ego_source="genesis_ego_cycle", status="tabled",
            resolved_at=_now_iso(),
        )
        await _insert_proposal(
            db, id="u1", ego_source="user_ego_cycle", status="tabled",
            resolved_at=_now_iso(),
        )

        all_results = await ego_crud.get_tabled(db)
        assert len(all_results) == 2


# ---------------------------------------------------------------------------
# Context builder integration tests
# ---------------------------------------------------------------------------

# Additional tables needed by the context builders
_EXTRA_TABLES_DDL = [
    """CREATE TABLE awareness_ticks (
        id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT '',
        signals_json TEXT NOT NULL DEFAULT '{}',
        scores_json TEXT NOT NULL DEFAULT '{}',
        signal_data TEXT, classified_depth TEXT,
        trigger_reason TEXT, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE observations (
        id TEXT PRIMARY KEY, person_id TEXT, source TEXT NOT NULL,
        type TEXT NOT NULL, category TEXT, content TEXT NOT NULL,
        priority TEXT NOT NULL, speculative INTEGER NOT NULL DEFAULT 0,
        retrieved_count INTEGER NOT NULL DEFAULT 0,
        influenced_action INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0, resolved_at TEXT,
        resolution_notes TEXT, created_at TEXT NOT NULL,
        expires_at TEXT, content_hash TEXT
    )""",
    """CREATE TABLE cost_events (
        id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
        model TEXT, provider TEXT, engine TEXT, task_id TEXT,
        person_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
        cost_usd REAL NOT NULL DEFAULT 0.0, metadata TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE ego_cycles (
        id TEXT PRIMARY KEY, output_text TEXT NOT NULL,
        proposals_json TEXT NOT NULL DEFAULT '[]',
        focus_summary TEXT NOT NULL DEFAULT '',
        model_used TEXT NOT NULL DEFAULT '',
        cost_usd REAL NOT NULL DEFAULT 0.0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, compacted_into TEXT
    )""",
    """CREATE TABLE follow_ups (
        id TEXT PRIMARY KEY, source TEXT NOT NULL,
        source_session TEXT, content TEXT NOT NULL,
        reason TEXT, strategy TEXT NOT NULL,
        scheduled_at TEXT, status TEXT NOT NULL DEFAULT 'pending',
        linked_task_id TEXT, priority TEXT NOT NULL DEFAULT 'medium',
        created_at TEXT NOT NULL, completed_at TEXT,
        resolution_notes TEXT, blocked_reason TEXT,
        escalated_to TEXT
    )""",
    """CREATE TABLE user_model_cache (
        id TEXT PRIMARY KEY DEFAULT 'current', person_id TEXT,
        model_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        synthesized_at TEXT NOT NULL, synthesized_by TEXT NOT NULL,
        evidence_count INTEGER NOT NULL DEFAULT 0,
        last_change_type TEXT, last_changed_at TEXT
    )""",
    """CREATE TABLE cc_sessions (
        id TEXT PRIMARY KEY, session_type TEXT NOT NULL,
        user_id TEXT, channel TEXT, model TEXT NOT NULL,
        effort TEXT NOT NULL DEFAULT 'medium',
        status TEXT NOT NULL DEFAULT 'active', pid INTEGER,
        started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
        checkpointed_at TEXT, completed_at TEXT,
        source_tag TEXT NOT NULL DEFAULT 'foreground',
        metadata TEXT, cc_session_id TEXT, thread_id TEXT,
        topic TEXT DEFAULT ''
    )""",
    """CREATE TABLE inbox_items (
        id TEXT PRIMARY KEY, file_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        batch_id TEXT, response_path TEXT,
        created_at TEXT NOT NULL, processed_at TEXT,
        error_message TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
        evaluated_content TEXT
    )""",
]


@pytest.fixture
async def full_db():
    """In-memory DB with all tables needed by both context builders."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(_EGO_PROPOSALS_DDL)
        for ddl in _EXTRA_TABLES_DDL:
            await conn.execute(ddl)
        await conn.commit()
        yield conn


def _mock_health():
    hd = AsyncMock()
    hd.snapshot.return_value = {
        "timestamp": _now_iso(),
        "infrastructure": {
            "genesis.db": {"status": "healthy", "latency_ms": 0.5},
        },
        "resilience": "healthy",
        "queues": {},
        "surplus": {},
        "conversation": {
            "status": "idle",
            "last_user_message_age_s": 600.0,
            "recent_user_turns": 1,
            "recent_assistant_turns": 1,
        },
        "cost": {"daily_total": 0.10},
    }
    return hd


class TestGenesisContextSeparation:
    """Genesis context builder should only see genesis_ego_cycle proposals."""

    @pytest.mark.asyncio
    async def test_proposal_history_filters_by_genesis_source(self, full_db):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder

        # Insert genesis proposal (should appear)
        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="pending", content="Genesis ops task",
        )
        # Insert user proposal (should NOT appear)
        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="pending", content="User strategic task",
        )

        builder = GenesisEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
            capabilities={"db": "SQLite"},
        )
        section = await builder._proposal_history_section()

        assert "Genesis ops task" in section
        assert "User strategic task" not in section

    @pytest.mark.asyncio
    async def test_proposal_history_includes_null_source(self, full_db):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder

        await _insert_proposal(
            full_db, id="legacy", ego_source=None,
            status="approved", content="Legacy proposal",
        )

        builder = GenesisEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
            capabilities={"db": "SQLite"},
        )
        section = await builder._proposal_history_section()
        assert "Legacy proposal" in section

    @pytest.mark.asyncio
    async def test_proposal_board_filters_by_genesis_source(self, full_db):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder

        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="pending", content="Genesis board item", rank=1,
        )
        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="pending", content="User board item", rank=2,
        )

        builder = GenesisEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
            capabilities={"db": "SQLite"},
        )
        section = await builder._proposal_board_section()

        assert "Genesis board item" in section
        assert "User board item" not in section

    @pytest.mark.asyncio
    async def test_approved_proposals_filtered(self, full_db):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder

        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="approved", content="Genesis approved",
        )
        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="approved", content="User approved",
        )

        builder = GenesisEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
            capabilities={"db": "SQLite"},
        )
        section = await builder._proposal_board_section()

        assert "Genesis approved" in section
        assert "User approved" not in section


class TestUserContextSeparation:
    """User context builder should only see user_ego_cycle proposals."""

    @pytest.mark.asyncio
    async def test_proposal_history_filters_by_user_source(self, full_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="pending", content="User strategic task",
        )
        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="pending", content="Genesis ops task",
        )

        builder = UserEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
        )
        section = await builder._proposal_history_section()

        assert "User strategic task" in section
        assert "Genesis ops task" not in section

    @pytest.mark.asyncio
    async def test_proposal_history_light_depth_filtered(self, full_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        # 2 user proposals, 1 genesis proposal
        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="pending", content="User task 1",
        )
        await _insert_proposal(
            full_db, id="u2", ego_source="user_ego_cycle",
            status="rejected", content="User task 2",
        )
        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="pending", content="Genesis task",
        )

        builder = UserEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
        )
        section = await builder._proposal_history_section(depth="light")

        # Light depth returns counts like "Active: 1 | Recently tried: 1"
        assert "Active: 1" in section
        assert "Recently tried: 1" in section

    @pytest.mark.asyncio
    async def test_proposal_history_includes_null_source(self, full_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        await _insert_proposal(
            full_db, id="legacy", ego_source=None,
            status="executed", content="Legacy proposal",
        )

        builder = UserEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
        )
        section = await builder._proposal_history_section()
        assert "Legacy proposal" in section

    @pytest.mark.asyncio
    async def test_proposal_board_filters_by_user_source(self, full_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="pending", content="User board item", rank=1,
        )
        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="pending", content="Genesis board item", rank=2,
        )

        builder = UserEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
        )
        section = await builder._proposal_board_section()

        assert "User board item" in section
        assert "Genesis board item" not in section

    @pytest.mark.asyncio
    async def test_approved_proposals_filtered(self, full_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="approved", content="User approved",
        )
        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="approved", content="Genesis approved",
        )

        builder = UserEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
        )
        section = await builder._proposal_board_section()

        assert "User approved" in section
        assert "Genesis approved" not in section

    @pytest.mark.asyncio
    async def test_tabled_proposals_filtered(self, full_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="tabled", content="User tabled item",
            resolved_at=_now_iso(),
        )
        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="tabled", content="Genesis tabled item",
            resolved_at=_now_iso(),
        )

        builder = UserEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
        )
        section = await builder._proposal_board_section()

        assert "User tabled item" in section
        assert "Genesis tabled item" not in section


class TestCrossEgoIsolation:
    """End-to-end isolation: each ego's full context excludes the other's proposals."""

    @pytest.mark.asyncio
    async def test_full_isolation(self, full_db):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder
        from genesis.ego.user_context import UserEgoContextBuilder

        # Seed proposals from both egos
        await _insert_proposal(
            full_db, id="g1", ego_source="genesis_ego_cycle",
            status="pending", content="GENESIS_ONLY_MARKER", rank=1,
        )
        await _insert_proposal(
            full_db, id="u1", ego_source="user_ego_cycle",
            status="pending", content="USER_ONLY_MARKER", rank=1,
        )
        await _insert_proposal(
            full_db, id="legacy", ego_source=None,
            status="pending", content="LEGACY_SHARED_MARKER", rank=2,
        )

        genesis_builder = GenesisEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
            capabilities={"db": "SQLite"},
        )
        user_builder = UserEgoContextBuilder(
            db=full_db, health_data=_mock_health(),
        )

        genesis_history = await genesis_builder._proposal_history_section()
        genesis_board = await genesis_builder._proposal_board_section()
        user_history = await user_builder._proposal_history_section()
        user_board = await user_builder._proposal_board_section()

        # Genesis sees its own + legacy, not user's
        assert "GENESIS_ONLY_MARKER" in genesis_history
        assert "GENESIS_ONLY_MARKER" in genesis_board
        assert "LEGACY_SHARED_MARKER" in genesis_history
        assert "LEGACY_SHARED_MARKER" in genesis_board
        assert "USER_ONLY_MARKER" not in genesis_history
        assert "USER_ONLY_MARKER" not in genesis_board

        # User sees its own + legacy, not genesis's
        assert "USER_ONLY_MARKER" in user_history
        assert "USER_ONLY_MARKER" in user_board
        assert "LEGACY_SHARED_MARKER" in user_history
        assert "LEGACY_SHARED_MARKER" in user_board
        assert "GENESIS_ONLY_MARKER" not in user_history
        assert "GENESIS_ONLY_MARKER" not in user_board
