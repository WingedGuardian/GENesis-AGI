"""Tests for genesis.ego.context — EgoContextBuilder."""

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.ego.context import EgoContextBuilder


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        # Create minimal tables needed by the context builder
        await conn.execute("""
            CREATE TABLE awareness_ticks (
                id INTEGER PRIMARY KEY,
                signal_data TEXT,
                classified_depth TEXT,
                created_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE observations (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                type TEXT NOT NULL,
                category TEXT,
                content TEXT NOT NULL,
                priority TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE cost_events (
                id INTEGER PRIMARY KEY,
                cost_usd REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE ego_cycles (
                id TEXT PRIMARY KEY,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE ego_proposals (
                id TEXT PRIMARY KEY,
                action_type TEXT NOT NULL,
                action_category TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                user_response TEXT,
                created_at TEXT NOT NULL
            )
        """)
        yield conn


@pytest.fixture
def mock_health_data():
    """Mock HealthDataService with realistic snapshot."""
    hd = AsyncMock()
    hd.snapshot.return_value = {
        "timestamp": "2026-03-28T18:00:00+00:00",
        "infrastructure": {
            "genesis.db": {"status": "healthy", "latency_ms": 0.5},
            "qdrant": {"status": "healthy", "latency_ms": 12.3},
            "ollama": {"status": "degraded", "latency_ms": 450.0},
        },
        "resilience": "healthy",
        "queues": {
            "deferred_work_queue": {"pending": 3},
            "dead_letter_queue": {"count": 0},
        },
        "surplus": {"queue_depth": 2, "last_dispatch": "2026-03-28T16:00:00"},
        "conversation": {
            "status": "idle",
            "last_user_message_age_s": 1800.0,
            "recent_user_turns": 15,
            "recent_assistant_turns": 12,
        },
        "cost": {"daily_total": 1.25},
        "call_sites": {},
        "cc_sessions": {},
        "awareness": {},
        "outreach_stats": {},
        "services": {},
        "api_keys": {},
        "mcp_servers": {},
        "provider_activity": {},
        "proactive_memory": {},
    }
    return hd


@pytest.fixture
def capabilities():
    return {
        "db": "SQLite database",
        "router": "LLM routing with circuit breakers",
        "memory": "Hybrid memory store",
        "ego": "Autonomous decision-making session",
    }


class TestEgoContextBuilder:
    @pytest.mark.asyncio
    async def test_build_produces_markdown(self, db, mock_health_data, capabilities):
        builder = EgoContextBuilder(
            db=db,
            health_data=mock_health_data,
            capabilities=capabilities,
        )
        result = await builder.build()
        assert isinstance(result, str)
        assert "# EGO_CONTEXT" in result
        assert "Operational Briefing" in result

    @pytest.mark.asyncio
    async def test_capability_section(self, db, mock_health_data, capabilities):
        builder = EgoContextBuilder(
            db=db,
            health_data=mock_health_data,
            capabilities=capabilities,
        )
        result = await builder.build()
        assert "## Capabilities" in result
        assert "**db**" in result
        assert "**router**" in result
        assert "**ego**" in result

    @pytest.mark.asyncio
    async def test_system_health_section(self, db, mock_health_data, capabilities):
        builder = EgoContextBuilder(
            db=db,
            health_data=mock_health_data,
            capabilities=capabilities,
        )
        result = await builder.build()
        assert "## System Health" in result
        assert "genesis.db" in result
        assert "healthy" in result
        assert "Composite state" in result

    @pytest.mark.asyncio
    async def test_user_activity_section(self, db, mock_health_data, capabilities):
        builder = EgoContextBuilder(
            db=db,
            health_data=mock_health_data,
            capabilities=capabilities,
        )
        result = await builder.build()
        assert "User Activity" in result
        assert "30min ago" in result or "30.0min ago" in result

    @pytest.mark.asyncio
    async def test_signals_section_with_data(self, db, mock_health_data, capabilities):
        signal_data = json.dumps({
            "software_error_spike": {"value": 0.0, "source": "observations"},
            "budget_pct_consumed": {"value": 0.25, "source": "cost_events"},
        })
        await db.execute(
            "INSERT INTO awareness_ticks (signal_data, classified_depth, created_at) "
            "VALUES (?, ?, ?)",
            (signal_data, "Micro", "2026-03-28T17:55:00+00:00"),
        )
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Awareness Signals" in result
        assert "software_error_spike" in result
        assert "budget_pct_consumed" in result

    @pytest.mark.asyncio
    async def test_signals_section_no_data(self, db, mock_health_data, capabilities):
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No awareness ticks recorded" in result

    @pytest.mark.asyncio
    async def test_observations_section(self, db, mock_health_data, capabilities):
        await db.execute(
            "INSERT INTO observations (id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs1", "error", "finding", "code_audit", "NullPointerException in router", "high", 0),
        )
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Unresolved Observations" in result
        assert "NullPointerException" in result
        assert "[high]" in result

    @pytest.mark.asyncio
    async def test_observations_section_empty(self, db, mock_health_data, capabilities):
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No unresolved observations" in result

    @pytest.mark.asyncio
    async def test_cost_section(self, db, mock_health_data, capabilities):
        await db.execute(
            "INSERT INTO cost_events (cost_usd, created_at) VALUES (?, datetime('now'))",
            (0.15,),
        )
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Cost Status" in result
        assert "$0.15" in result

    @pytest.mark.asyncio
    async def test_proposal_history_section(self, db, mock_health_data, capabilities):
        await db.execute(
            "INSERT INTO ego_proposals (id, action_type, action_category, content, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            ("p1", "investigate", "system_health", "Check backlog", "approved"),
        )
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Recent Proposals" in result
        assert "investigate" in result
        assert "approved" in result

    @pytest.mark.asyncio
    async def test_output_contract_section(self, db, mock_health_data, capabilities):
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Output Contract" in result
        assert "proposals" in result
        assert "focus_summary" in result
        assert "JSON" in result

    @pytest.mark.asyncio
    async def test_no_health_data(self, db, capabilities):
        builder = EgoContextBuilder(
            db=db, health_data=None, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health data service not available" in result

    @pytest.mark.asyncio
    async def test_health_snapshot_failure(self, db, capabilities):
        hd = AsyncMock()
        hd.snapshot.side_effect = RuntimeError("DB locked")
        builder = EgoContextBuilder(
            db=db, health_data=hd, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health snapshot failed" in result

    @pytest.mark.asyncio
    async def test_no_capabilities(self, db, mock_health_data):
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities={},
        )
        result = await builder.build()
        assert "No capabilities registered" in result

    @pytest.mark.asyncio
    async def test_observation_content_truncation(self, db, mock_health_data, capabilities):
        long_content = "A" * 500
        await db.execute(
            "INSERT INTO observations (id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs2", "error", "finding", None, long_content, "medium", 0),
        )
        builder = EgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "..." in result
        assert "A" * 500 not in result  # truncated
