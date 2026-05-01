"""Tests for genesis.ego.genesis_context — GenesisEgoContextBuilder."""

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.ego.genesis_context import GenesisEgoContextBuilder


@pytest.fixture
async def db():
    """In-memory DB with tables needed by GenesisEgoContextBuilder."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE awareness_ticks (
                id               TEXT PRIMARY KEY,
                source           TEXT NOT NULL,
                signals_json     TEXT NOT NULL,
                scores_json      TEXT NOT NULL,
                signal_data      TEXT,
                classified_depth TEXT,
                trigger_reason   TEXT,
                created_at       TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE observations (
                id               TEXT PRIMARY KEY,
                person_id        TEXT,
                source           TEXT NOT NULL,
                type             TEXT NOT NULL,
                category         TEXT,
                content          TEXT NOT NULL,
                priority         TEXT NOT NULL,
                speculative      INTEGER NOT NULL DEFAULT 0,
                retrieved_count  INTEGER NOT NULL DEFAULT 0,
                influenced_action INTEGER NOT NULL DEFAULT 0,
                resolved         INTEGER NOT NULL DEFAULT 0,
                resolved_at      TEXT,
                resolution_notes TEXT,
                created_at       TEXT NOT NULL,
                expires_at       TEXT,
                content_hash     TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE cost_events (
                id               TEXT PRIMARY KEY,
                event_type       TEXT NOT NULL,
                model            TEXT,
                provider         TEXT,
                engine           TEXT,
                task_id          TEXT,
                person_id        TEXT,
                input_tokens     INTEGER,
                output_tokens    INTEGER,
                cost_usd         REAL NOT NULL DEFAULT 0.0,
                metadata         TEXT,
                created_at       TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE ego_cycles (
                id              TEXT PRIMARY KEY,
                output_text     TEXT NOT NULL,
                proposals_json  TEXT NOT NULL DEFAULT '[]',
                focus_summary   TEXT NOT NULL DEFAULT '',
                model_used      TEXT NOT NULL DEFAULT '',
                cost_usd        REAL NOT NULL DEFAULT 0.0,
                input_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                compacted_into  TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE ego_proposals (
                id              TEXT PRIMARY KEY,
                action_type     TEXT NOT NULL,
                action_category TEXT NOT NULL DEFAULT '',
                content         TEXT NOT NULL,
                rationale       TEXT NOT NULL DEFAULT '',
                confidence      REAL NOT NULL DEFAULT 0.0,
                urgency         TEXT NOT NULL DEFAULT 'normal',
                alternatives    TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'pending',
                user_response   TEXT,
                cycle_id        TEXT,
                batch_id        TEXT,
                created_at      TEXT NOT NULL,
                resolved_at     TEXT,
                expires_at      TEXT,
                rank            INTEGER,
                execution_plan  TEXT,
                recurring       INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE follow_ups (
                id               TEXT PRIMARY KEY,
                source           TEXT NOT NULL,
                source_session   TEXT,
                content          TEXT NOT NULL,
                reason           TEXT,
                strategy         TEXT NOT NULL,
                scheduled_at     TEXT,
                status           TEXT NOT NULL DEFAULT 'pending',
                linked_task_id   TEXT,
                priority         TEXT NOT NULL DEFAULT 'medium',
                created_at       TEXT NOT NULL,
                completed_at     TEXT,
                resolution_notes TEXT,
                blocked_reason   TEXT,
                escalated_to     TEXT
            )
        """)
        yield conn


@pytest.fixture
def mock_health_data():
    """Mock HealthDataService with realistic snapshot."""
    hd = AsyncMock()
    hd.snapshot.return_value = {
        "timestamp": "2026-04-24T10:00:00+00:00",
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
        "surplus": {"queue_depth": 2, "last_dispatch": "2026-04-24T08:00:00"},
    }
    return hd


@pytest.fixture
def capabilities():
    return {
        "db": "SQLite database",
        "router": "LLM routing with circuit breakers",
        "memory": "Hybrid memory store",
    }


class TestGenesisEgoContextBuilder:
    @pytest.mark.asyncio
    async def test_build_produces_markdown(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert isinstance(result, str)
        assert "# GENESIS_EGO_CONTEXT" in result
        assert "Operations Briefing" in result

    # ── System Health ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_system_health_section(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## System Health" in result
        assert "genesis.db" in result
        assert "healthy" in result
        assert "Composite state" in result

    @pytest.mark.asyncio
    async def test_system_health_shows_queues(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Deferred work: 3 pending" in result
        assert "Dead letter: 0 items" in result

    @pytest.mark.asyncio
    async def test_system_health_shows_surplus(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Queue depth: 2" in result

    @pytest.mark.asyncio
    async def test_system_health_no_health_data(self, db, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=None, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health data service not available" in result

    @pytest.mark.asyncio
    async def test_system_health_snapshot_failure(self, db, capabilities):
        hd = AsyncMock()
        hd.snapshot.side_effect = RuntimeError("DB locked")
        builder = GenesisEgoContextBuilder(
            db=db, health_data=hd, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health snapshot failed" in result

    # ── Signals ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_signals_section(self, db, mock_health_data, capabilities):
        signal_data = json.dumps({
            "software_error_spike": {"value": 0.0, "source": "observations"},
            "budget_pct_consumed": {"value": 0.25, "source": "cost_events"},
        })
        await db.execute(
            "INSERT INTO awareness_ticks "
            "(id, source, signals_json, scores_json, signal_data, classified_depth, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t1", "scheduled", signal_data, "{}", "{}", "Micro", "2026-04-24T09:55:00+00:00"),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Awareness Signals" in result
        assert "software_error_spike" in result
        assert "budget_pct_consumed" in result
        assert "Micro" in result
        # Single tick → all trends should be stable (→)
        assert "Trend" in result
        assert "\u2192" in result  # → symbol

    @pytest.mark.asyncio
    async def test_signals_trend_arrows(self, db, mock_health_data, capabilities):
        """Signal trends show up/down/stable arrows based on previous tick."""
        # Previous tick: error_spike=0.0, budget=0.50
        prev_data = json.dumps([
            {"name": "software_error_spike", "value": 0.0, "source": "circuit_breakers"},
            {"name": "budget_pct_consumed", "value": 0.50, "source": "cost_events"},
            {"name": "container_memory_pct", "value": 0.60, "source": "cgroup"},
        ])
        await db.execute(
            "INSERT INTO awareness_ticks "
            "(id, source, signals_json, scores_json, signal_data, classified_depth, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t_prev", "scheduled", prev_data, "{}", "{}", "Micro", "2026-04-24T09:50:00+00:00"),
        )
        # Current tick: error_spike=0.3 (↑), budget=0.25 (↓), memory=0.60 (→)
        curr_data = json.dumps([
            {"name": "software_error_spike", "value": 0.3, "source": "circuit_breakers"},
            {"name": "budget_pct_consumed", "value": 0.25, "source": "cost_events"},
            {"name": "container_memory_pct", "value": 0.60, "source": "cgroup"},
        ])
        await db.execute(
            "INSERT INTO awareness_ticks "
            "(id, source, signals_json, scores_json, signal_data, classified_depth, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t_curr", "scheduled", curr_data, "{}", "{}", "Micro", "2026-04-24T09:55:00+00:00"),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        # Check that all three trend arrows appear
        assert "\u2191" in result  # ↑ for error_spike going up
        assert "\u2193" in result  # ↓ for budget going down
        assert "\u2192" in result  # → for memory staying the same

    @pytest.mark.asyncio
    async def test_signals_section_no_data(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No awareness ticks recorded" in result

    # ── Observations ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_observations_includes_genesis(self, db, mock_health_data, capabilities):
        """Genesis-internal categories (routine, anomaly) should appear."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs1", "sentinel", "finding", "routine", "Health check anomaly", "medium", 0),
        )
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs2", "sentinel", "finding", "anomaly", "CPU spike detected", "high", 0),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Unresolved Observations" in result
        assert "Health check anomaly" in result
        assert "CPU spike detected" in result

    @pytest.mark.asyncio
    async def test_observations_excludes_user_world(
        self, db, mock_health_data, capabilities,
    ):
        """User-world categories (email_recon, inbox) should NOT appear."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs3", "recon", "finding", "email_recon", "New job at Acme", "high", 0),
        )
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs4", "inbox_proc", "finding", "inbox", "User email digest", "medium", 0),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "New job at Acme" not in result
        assert "User email digest" not in result

    @pytest.mark.asyncio
    async def test_observations_excludes_escalations(
        self, db, mock_health_data, capabilities,
    ):
        """Escalation-type observations are excluded from Genesis ego view."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                "esc1", "genesis_ego", "escalation_to_user_ego", "infrastructure",
                "Escalated to user ego", "high", 0,
            ),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Escalated to user ego" not in result

    @pytest.mark.asyncio
    async def test_observations_empty(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No unresolved Genesis-internal observations" in result

    @pytest.mark.asyncio
    async def test_observations_null_category_included(
        self, db, mock_health_data, capabilities,
    ):
        """Observations with NULL category should be included (genesis-internal)."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs5", "error", "finding", None, "Uncategorized system error", "high", 0),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Uncategorized system error" in result

    @pytest.mark.asyncio
    async def test_observation_content_truncation(
        self, db, mock_health_data, capabilities,
    ):
        long_content = "B" * 500
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs6", "error", "finding", None, long_content, "medium", 0),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "..." in result
        assert "B" * 500 not in result

    # ── Cost ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cost_section(self, db, mock_health_data, capabilities):
        await db.execute(
            "INSERT INTO cost_events "
            "(id, event_type, cost_usd, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("c1", "llm_call", 0.15, ),
        )
        await db.execute(
            "INSERT INTO cost_events "
            "(id, event_type, cost_usd, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("c2", "llm_call", 0.25, ),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Cost Status" in result
        assert "$0.40" in result

    @pytest.mark.asyncio
    async def test_cost_section_with_ego_spend(self, db, mock_health_data, capabilities):
        await db.execute(
            "INSERT INTO ego_cycles "
            "(id, output_text, proposals_json, focus_summary, model_used, "
            "cost_usd, input_tokens, output_tokens, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("cyc1", "output", "[]", "focus", "opus-4", 0.08, 1000, 500, 3000),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Ego spend today" in result
        assert "$0.08" in result

    @pytest.mark.asyncio
    async def test_cost_section_empty(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Cost Status" in result
        assert "$0.0000" in result

    # ── Output Contract ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_output_contract_has_escalations(
        self, db, mock_health_data, capabilities,
    ):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Output Contract" in result
        assert "escalations" in result
        assert "suggested_action" in result
        assert "No morning_report" in result

    @pytest.mark.asyncio
    async def test_output_contract_has_proposals(
        self, db, mock_health_data, capabilities,
    ):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "proposals" in result
        assert "focus_summary" in result
        assert "JSON" in result

    # ── Integration ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_all_sections_present(self, db, mock_health_data, capabilities):
        """Verify all expected section headers appear in the output."""
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        expected_sections = [
            "## System Health",
            "## Awareness Signals",
            "## Unresolved Observations",
            "## Maintenance Follow-ups",
            "## Cost Status",
            "## Recent Proposals",
            "## Output Contract",
        ]
        for section in expected_sections:
            assert section in result, f"Missing section: {section}"

    @pytest.mark.asyncio
    async def test_observations_excludes_interest_categories(
        self, db, mock_health_data, capabilities,
    ):
        """interest/interests/finding categories are user-world, excluded here."""
        for cat in ("interest", "interests", "finding"):
            await db.execute(
                "INSERT INTO observations "
                "(id, source, type, category, content, priority, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (f"obs_{cat}", "recon", "finding", cat, f"Content for {cat}", "medium", 0),
            )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Content for interest" not in result
        assert "Content for interests" not in result
        assert "Content for finding" not in result

    # ── Proposal History (3b) ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_proposal_history_section(self, db, mock_health_data, capabilities):
        """Proposal history displays recent proposals in a table."""
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, action_category, content, status, "
            "user_response, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            ("p1", "outreach", "linkedin", "Post update on LinkedIn", "approved", None),
        )
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, action_category, content, status, "
            "user_response, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            ("p2", "investigate", "recon", "Check competitor repos", "rejected", "not now"),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Recent Proposals" in result
        assert "outreach" in result
        assert "Post update on LinkedIn" in result
        assert "approved" in result
        assert "rejected" in result
        assert "not now" in result

    @pytest.mark.asyncio
    async def test_proposal_history_empty(self, db, mock_health_data, capabilities):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No proposals in last 7 days" in result

    @pytest.mark.asyncio
    async def test_proposal_history_truncates_long_content(
        self, db, mock_health_data, capabilities,
    ):
        """Long content in proposal history table is truncated to 80 chars."""
        long_content = "Z" * 200
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, action_category, content, status, "
            "user_response, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            ("p3", "investigate", "recon", long_content, "pending", None),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        proposals_section = result.split("## Recent Proposals")[1].split("##")[0]
        assert "..." in proposals_section
        assert "Z" * 200 not in proposals_section

    @pytest.mark.asyncio
    async def test_proposal_history_pipes_escaped(
        self, db, mock_health_data, capabilities,
    ):
        """Pipe chars in content are escaped to not break markdown table."""
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, action_category, content, status, "
            "user_response, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            ("p4", "investigate", "recon", "repo|branch|status", "pending", None),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "repo/branch/status" in result
