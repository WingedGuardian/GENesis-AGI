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
        from genesis.db.schema import TABLES

        await conn.execute(TABLES["user_goals"])
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
                recurring       INTEGER DEFAULT 0,
                memory_basis    TEXT DEFAULT '',
                realist_verdict  TEXT,
                realist_reasoning TEXT,
                ego_source       TEXT,
                goal_id          TEXT,
                content_hash     TEXT,
                original_content TEXT,
                content_size     INTEGER,
                expected_outputs TEXT
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
                escalated_to     TEXT,
                kind             TEXT NOT NULL DEFAULT 'follow_up',
                domain           TEXT,
                goal_id          TEXT
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
    async def test_observations_records_read_receipt(
        self, db, mock_health_data, capabilities,
    ):
        """Building the context increments retrieved_count on surfaced obs (B2)."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, "
            " retrieved_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("rr1", "sentinel", "finding", "routine", "needs a read receipt",
             "medium", 0, 0),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        await builder.build()
        cur = await db.execute(
            "SELECT retrieved_count FROM observations WHERE id = 'rr1'"
        )
        assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_observations_unread_sorted_before_read(
        self, db, mock_health_data, capabilities,
    ):
        """Within a priority tier, unread (retrieved_count=0) sorts before read (B2)."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, "
            " retrieved_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("read1", "sentinel", "finding", "routine", "ALREADY_SEEN_ITEM",
             "medium", 0, 5),
        )
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, "
            " retrieved_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("unread1", "sentinel", "finding", "routine", "BRAND_NEW_ITEM",
             "medium", 0, 0),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert result.index("BRAND_NEW_ITEM") < result.index("ALREADY_SEEN_ITEM")

    @pytest.mark.asyncio
    async def test_observations_redirect_triggers_investigation(
        self, db, mock_health_data, capabilities,
    ):
        """A redirect-type observation fires the in-cycle investigation prompt.

        Guards the column index of the redirect_count check (type is row[2]
        once id is prepended to the SELECT).
        """
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("redir1", "realist", "cross_domain_redirect", None,
             "Investigate memory drift across domains", "high", 0),
        )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "require in-cycle investigation" in result

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
    async def test_observations_relevance_suffix_partition(
        self, db, mock_health_data, capabilities,
    ):
        """Perception relevance suffix: ':user' excluded; ':both'/':genesis' kept.

        Locks the producer→consumer contract (writer emits
        '<base>:<relevance>'; this builder filters `NOT LIKE '%:user'`),
        which was previously untested end-to-end.
        """
        rows = [
            ("rel1", "reflection", "micro_reflection", "routine:user",
             "User task quality shifted", "low"),
            ("rel2", "reflection", "micro_reflection", "anomaly:both",
             "CPU and user activity anomaly", "high"),
            ("rel3", "reflection", "micro_reflection", "routine:genesis",
             "Disk usage creeping", "low"),
        ]
        for row in rows:
            await db.execute(
                "INSERT INTO observations "
                "(id, source, type, category, content, priority, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                row,
            )
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "User task quality shifted" not in result
        assert "CPU and user activity anomaly" in result
        assert "Disk usage creeping" in result

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
    async def test_cost_section_removed(self, db, mock_health_data, capabilities):
        """Cost section intentionally removed to prevent budget escalation loop."""
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Cost Status" not in result

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
            "## Active Proposals",
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
        assert "Active Proposals" in result
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
        assert "No active proposals" in result

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
        proposals_section = result.split("## Active Proposals")[1].split("##")[0]
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


class TestErrorVisibilityMarkers:
    """P-9: a query FAILURE must render a distinguishable marker, never the same
    output as the genuine-empty state (a dead instrument must read as dead)."""

    @pytest.mark.asyncio
    async def test_settled_decisions_query_error_renders_marker(
        self, db, mock_health_data, capabilities, monkeypatch,
    ):
        from genesis.db.crud import ego as ego_crud

        async def _boom(*a, **k):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(ego_crud, "list_active_decisions", _boom)
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        out = await builder._settled_decisions_section()
        assert "query error" in out.lower()
        assert out != ""  # not masked as the empty state

    @pytest.mark.asyncio
    async def test_capability_performance_query_error_renders_marker(
        self, db, mock_health_data, capabilities, monkeypatch,
    ):
        from genesis.db.crud import capability_map as cap_crud

        async def _boom(*a, **k):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(cap_crud, "get_all", _boom)
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        out = await builder._capability_performance_section()
        assert "query error" in out.lower()
        assert out != ""


class TestOwnGoalsSection:
    """PR-3b: the genesis ego's own-goal lane rendering — what makes
    own-goal review non-blind."""

    def _builder(self, db):
        return GenesisEgoContextBuilder(db=db, health_data=None, capabilities={})

    async def _insert_goal(self, db, *, gid, title, origin, status="active",
                           updated="2026-01-01T00:00:00+00:00", cadence_days=None):
        await db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, status, priority, origin, cadence_days, "
            " created_at, updated_at) "
            "VALUES (?, ?, 'project', ?, 'medium', ?, ?, ?, ?)",
            (gid, title, status, origin, cadence_days, updated, updated),
        )
        await db.commit()

    async def test_empty_lane_shows_affordance(self, db):
        section = await self._builder(db)._own_goals_section()
        assert "## Your Own Goals" in section
        assert "own_goal_creations" in section

    async def test_lists_own_goals_with_staleness(self, db):
        await self._insert_goal(
            db, gid="og1", title="Retire legacy bridge", origin="genesis_ego",
        )
        section = await self._builder(db)._own_goals_section()
        assert "Retire legacy bridge" in section
        assert "og1" in section  # id present — reviews reference it
        assert "STALE, review due" in section  # updated in 2026-01 → stale

    async def test_fresh_goal_not_stale(self, db):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        await self._insert_goal(
            db, gid="og2", title="Fresh objective", origin="genesis_ego",
            updated=now,
        )
        section = await self._builder(db)._own_goals_section()
        assert "Fresh objective" in section
        assert "STALE" not in section

    async def test_paused_listed_user_goals_absent(self, db):
        await self._insert_goal(
            db, gid="og3", title="Paused own goal", origin="genesis_ego",
            status="paused",
        )
        await self._insert_goal(
            db, gid="ug1", title="User career goal", origin="user",
        )
        section = await self._builder(db)._own_goals_section()
        assert "Paused own goal" in section
        assert "[PAUSED]" in section
        assert "User career goal" not in section
        # a PAUSED goal is never marked review-due
        assert "STALE" not in section

    async def test_build_includes_section_and_contract_keys(
        self, db, mock_health_data, capabilities,
    ):
        builder = GenesisEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        context = await builder.build()
        assert "## Your Own Goals" in context
        assert "own_goal_creations" in context   # output contract
        assert "own_goal_reviews" in context
