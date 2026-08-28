"""Tests for genesis.ego.genesis_context — GenesisEgoContextBuilder."""

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.ego.genesis_context import GenesisEgoContextBuilder


@pytest.fixture(autouse=True)
def _force_reconcile_off(monkeypatch):
    """These tests predate the PR-5 reconcile lever and assert the board-visible
    (mode=off) rendering of the proposal sections. The blind-drafting default
    (shadow) is covered separately in test_blind_drafting.py."""
    monkeypatch.setattr("genesis.ego.reconcile_config.effective_mode", lambda: "off")


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


class TestCapabilityPerformanceFocusedDeficiency:
    """The focused weak domain is surfaced even when it's off the top-N table."""

    @pytest.fixture
    async def cap_db(self):
        from genesis.db.schema import TABLES

        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(TABLES["capability_map"])
            await conn.commit()
            yield conn

    @staticmethod
    async def _insert(db, domain, confidence):
        from datetime import UTC, datetime

        await db.execute(
            "INSERT INTO capability_map "
            "(id, domain, confidence, sample_size, trend, evidence_summary, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (domain, domain, confidence, 8, "declining",
             f"{domain}-evidence", datetime.now(UTC).isoformat()),
        )
        await db.commit()

    async def test_weak_focus_row_surfaced_when_off_top15(self, cap_db):
        # 20 strong domains crowd out the weakest one from the top-15 table.
        for i in range(20):
            await self._insert(cap_db, f"strong{i:02d}", 0.90)
        await self._insert(cap_db, "weakling", 0.12)

        builder = GenesisEgoContextBuilder(db=cap_db)
        builder._focus_id = "weakling"
        section = await builder._capability_performance_section()

        # Weakest domain is absent from the confidence-DESC top-15 table...
        assert "| weakling |" not in section
        # ...but its Focused deficiency line names it with its evidence.
        assert "Focused deficiency — weakling" in section
        assert "12%" in section
        assert "weakling-evidence" in section

    async def test_no_focus_id_no_deficiency_line(self, cap_db):
        await self._insert(cap_db, "outreach", 0.30)
        builder = GenesisEgoContextBuilder(db=cap_db)
        builder._focus_id = None
        section = await builder._capability_performance_section()
        assert "Focused deficiency" not in section

    async def test_stale_focus_row_still_surfaced(self, cap_db):
        """The deficiency line must survive the bars get_prompt_rows applies.

        A capability_improvement cycle targets a domain BECAUSE it is weak, and
        a weak domain is exactly the kind that stops being refreshed. If the
        focus row is looked up by scanning get_all's output, the window hides it
        and the advisory silently loses the deficiency it exists to name — so
        the lookup goes through get_by_domain, which is never filtered.
        """
        from datetime import UTC, datetime, timedelta

        # A fresh row anchors the window; the focus row sits far behind it.
        await self._insert(cap_db, "fresh_domain", 0.90)
        await cap_db.execute(
            "INSERT INTO capability_map "
            "(id, domain, confidence, sample_size, trend, evidence_summary, "
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, datetime(?, '-93 days'))",
            ("stale_weakling", "stale_weakling", 0.05, 6, "declining",
             "stale-weakling-evidence", datetime.now(UTC).isoformat()),
        )
        await cap_db.commit()

        builder = GenesisEgoContextBuilder(db=cap_db)
        builder._focus_id = "stale_weakling"
        section = await builder._capability_performance_section()

        # Hidden from the table (it is stale)...
        assert "| stale_weakling |" not in section
        # ...but still named as the focused deficiency.
        assert "Focused deficiency — stale_weakling" in section
        assert "stale-weakling-evidence" in section
        # ...and DATED. This is the one row shown unfiltered, so without a
        # last-vouched stamp it is the row most likely to read as a
        # present-tense claim on months-old evidence. A mutation blanking the
        # stamp previously survived the whole suite.
        _expected = (datetime.now(UTC) - timedelta(days=93)).strftime("%Y-%m-%d")
        assert f"last vouched {_expected}" in section

    async def test_focus_survives_an_entirely_filtered_table(self, cap_db):
        """The S2 case: every row filtered out, focus line must still render.

        This is the severe form of the bug the get_by_domain switch exists to
        prevent. With the empty-check ahead of the focus lookup the section
        returned "no data" and dropped the deficiency line — precisely when the
        cycle was running to address that deficiency.
        """
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        # Only thin rows (below the sample floor) — the prompt read returns [].
        for i in range(3):
            await cap_db.execute(
                "INSERT INTO capability_map "
                "(id, domain, confidence, sample_size, trend, evidence_summary, "
                " updated_at) VALUES (?, ?, ?, 1, 'stable', ?, ?)",
                (f"thin{i}", f"thin{i}", 0.95, f"thin{i}-evidence", now),
            )
        await cap_db.commit()

        builder = GenesisEgoContextBuilder(db=cap_db)
        builder._focus_id = "thin1"
        section = await builder._capability_performance_section()

        assert "Focused deficiency — thin1" in section
        # And the empty state must not claim the table is empty when it is not.
        assert "No performance data yet" not in section

    async def test_filtered_empty_state_does_not_claim_no_data(self, cap_db):
        """A fully-filtered table is not the same as an untracked one."""
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        await cap_db.execute(
            "INSERT INTO capability_map "
            "(id, domain, confidence, sample_size, trend, evidence_summary, "
            " updated_at) VALUES ('t', 't', 0.9, 1, 'stable', 'e', ?)",
            (now,),
        )
        await cap_db.commit()

        builder = GenesisEgoContextBuilder(db=cap_db)
        builder._focus_id = None
        section = await builder._capability_performance_section()

        assert "No qualifying capability rows" in section
        assert "| t |" not in section


class TestAllRenderersUseTheFilteredRead:
    """All three prompt renderers must read get_prompt_rows, not get_all.

    Pinned because a mutation reverting any single call site left the whole
    suite green: the switch was unconstrained in two of three places. Each
    renderer is also checked for an honest empty state — a table full of
    filtered-out rows is not "no data", and telling the ego it has no track
    record when the table holds thousands of rows is the exact class of false
    self-claim this work exists to remove.
    """

    @pytest.fixture
    async def thin_db(self):
        """A table that is FULL but entirely below the sample floor."""
        from datetime import UTC, datetime

        from genesis.db.schema import TABLES

        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(TABLES["capability_map"])
            now = datetime.now(UTC).isoformat()
            for i in range(50):
                await conn.execute(
                    "INSERT INTO capability_map (id, domain, confidence, "
                    "sample_size, trend, evidence_summary, updated_at) "
                    "VALUES (?, ?, 0.95, 1, 'stable', 'e', ?)",
                    (f"thin{i}", f"thin{i}", now),
                )
            await conn.commit()
            yield conn

    async def test_genesis_context_does_not_claim_no_data(self, thin_db):
        builder = GenesisEgoContextBuilder(db=thin_db)
        builder._focus_id = None
        out = await builder._capability_performance_section()
        assert "No qualifying capability rows" in out
        assert "| thin0 |" not in out

    async def test_user_context_does_not_claim_no_track_record(self, thin_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        out = await UserEgoContextBuilder(db=thin_db)._capability_performance_section()
        assert "No performance data yet" not in out
        assert "No qualifying track record" in out

    async def test_base_context_does_not_tell_the_ego_to_run_aggregation(
        self, thin_db
    ):
        """The old text advised an action that cannot help after the write floor."""
        from genesis.ego.context import EgoContextBuilder

        out = await EgoContextBuilder(db=thin_db)._self_model_section()
        assert "run aggregation to populate" not in out
        assert "No qualifying capability rows" in out


class TestRenderStateMatrix:
    """Every renderer x every result state must render something TRUE.

    Written as a matrix rather than as cases, because the defects this replaces
    were all the same shape: one cell of the surface was fixed and its siblings
    were left asserting the old, now-false thing. Enumerating the cells makes an
    unfixed sibling a test failure instead of a review finding.

    States a prompt read can land in:
      error     - the query raised
      empty     - the map genuinely holds nothing
      filtered  - the map is FULL but every row failed a bar
      rows      - qualifying rows exist
    """

    STATES = ("error", "empty", "filtered", "rows")

    @staticmethod
    async def _make_db(state):
        from datetime import UTC, datetime

        from genesis.db.schema import TABLES

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["capability_map"])
        now = datetime.now(UTC).isoformat()
        if state == "filtered":
            for i in range(40):  # present, but all below the sample floor
                await conn.execute(
                    "INSERT INTO capability_map (id, domain, confidence, "
                    "sample_size, trend, evidence_summary, updated_at) "
                    "VALUES (?, ?, 0.95, 1, 'stable', 'e', ?)",
                    (f"t{i}", f"t{i}", now),
                )
        elif state == "rows":
            for i in range(3):
                await conn.execute(
                    "INSERT INTO capability_map (id, domain, confidence, "
                    "sample_size, trend, evidence_summary, updated_at) "
                    "VALUES (?, ?, 0.8, 25, 'stable', 'e', ?)",
                    (f"r{i}", f"real{i}", now),
                )
        await conn.commit()
        if state == "error":
            await conn.close()  # every query on it now raises
        return conn

    async def _render(self, which, state, focus=None):
        db = await self._make_db(state)
        try:
            if which == "genesis":
                b = GenesisEgoContextBuilder(db=db)
                b._focus_id = focus
                return await b._capability_performance_section()
            if which == "user":
                from genesis.ego.user_context import UserEgoContextBuilder
                return await UserEgoContextBuilder(db=db)._capability_performance_section()
            from genesis.ego.context import EgoContextBuilder
            return await EgoContextBuilder(db=db)._self_model_section()
        finally:
            if state != "error":
                await db.close()

    # Parametrised over the CROSS PRODUCT of the axes, not each axis in turn.
    # Enumerating the state axis with `focus` pinned to None covered every state
    # and still missed the cell that exists only when `entries` is empty AND a
    # focus row is present — a cell reachable only where two conditions hold at
    # once. Separate enumerations do not reach the product.
    @pytest.mark.parametrize("which", ["genesis", "user", "base"])
    @pytest.mark.parametrize("state", STATES)
    @pytest.mark.parametrize("focus", [None, "t0"])
    async def test_cell_renders_a_true_claim(self, which, state, focus):
        out = await self._render(which, state, focus=focus)
        assert out.strip(), f"{which}/{state} rendered nothing"

        if state == "error":
            assert "query error" in out, f"{which}: a failure must not read as data"
        elif state == "empty":
            assert "map is empty" in out, f"{which}: genuinely-empty must say so"
            assert "present;" not in out, (
                f"{which}: must not imply rows were withheld when none exist"
            )
        elif state == "filtered":
            # Must NOT claim emptiness, and must say how many rows are really
            # there — including when a focus row is also being rendered, which
            # is the cell that previously went silent.
            assert "map is empty" not in out, (
                f"{which}: 40 rows present — claiming empty is false"
            )
            assert "40 " in out, (
                f"{which}/focus={focus}: should name the real row count"
            )
        else:
            assert "real0" in out, f"{which}: qualifying rows must render"

    @pytest.mark.parametrize("state", STATES)
    async def test_light_depth_never_states_a_whole_map_figure(self, state):
        """The light branch renders no table, so its numbers must be qualified."""
        from genesis.ego.user_context import UserEgoContextBuilder

        db = await self._make_db(state)
        try:
            out = await UserEgoContextBuilder(db=db)._capability_performance_section(
                depth="light"
            )
        finally:
            if state != "error":
                await db.close()
        assert "domains tracked" not in out, (
            "unqualified whole-map phrasing reintroduced"
        )
        if state == "rows":
            assert "qualifying evidence" in out


class TestCountFailureDoesNotAbortTheCycle:
    """The count on the failure branch must not itself be able to fail loudly.

    All three renderers wrap the main read in try/except and degrade to one
    italic line. The row count added alongside it runs on that same degraded
    branch — and nothing between a context section and `assemble_context`
    catches per-section exceptions, so an unguarded second query turns a
    graceful degradation into an aborted ego cycle.

    A whole-DB failure fixture cannot reach this: it makes the FIRST read raise,
    so the count is never called. The failure has to be per-call.
    """

    class _FailSecondRead:
        """Wraps a connection; the Nth execute onward raises."""

        def __init__(self, conn, fail_from=2):
            self._conn = conn
            self._calls = 0
            self._fail_from = fail_from

        async def execute(self, *a, **k):
            self._calls += 1
            if self._calls >= self._fail_from:
                raise aiosqlite.OperationalError("database is locked")
            return await self._conn.execute(*a, **k)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @pytest.fixture
    async def thin_db(self):
        from datetime import UTC, datetime

        from genesis.db.schema import TABLES

        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(TABLES["capability_map"])
            now = datetime.now(UTC).isoformat()
            for i in range(5):
                await conn.execute(
                    "INSERT INTO capability_map (id, domain, confidence, "
                    "sample_size, trend, evidence_summary, updated_at) "
                    "VALUES (?, ?, 0.95, 1, 'stable', 'e', ?)",
                    (f"thin{i}", f"thin{i}", now),
                )
            await conn.commit()
            yield conn

    async def test_genesis_section_survives_a_failing_count(self, thin_db):
        wrapped = self._FailSecondRead(thin_db)
        b = GenesisEgoContextBuilder(db=wrapped)
        b._focus_id = None
        out = await b._capability_performance_section()  # must not raise
        assert "count unavailable" in out

    async def test_user_section_survives_a_failing_count(self, thin_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        wrapped = self._FailSecondRead(thin_db)
        out = await UserEgoContextBuilder(db=wrapped)._capability_performance_section()
        assert "count unavailable" in out

    async def test_base_section_survives_a_failing_count(self, thin_db):
        from genesis.ego.context import EgoContextBuilder

        wrapped = self._FailSecondRead(thin_db)
        out = await EgoContextBuilder(db=wrapped)._self_model_section()
        assert "count unavailable" in out


class TestLightDepthDatesItsEvidence:
    """The light branch renders no table, so its sentence is the whole claim.

    The window is anchored on the freshest row precisely so a dead refresh job
    keeps every row rather than blanking the map — which means during such an
    outage every row is months old. An unqualified "current" is therefore false
    exactly when the guarantee is doing its job.
    """

    @pytest.fixture
    async def aged_db(self):
        from datetime import UTC, datetime, timedelta

        from genesis.db.schema import TABLES

        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute(TABLES["capability_map"])
            stamp = (datetime.now(UTC) - timedelta(days=200)).isoformat()
            for i in range(3):
                await conn.execute(
                    "INSERT INTO capability_map (id, domain, confidence, "
                    "sample_size, trend, evidence_summary, updated_at) "
                    "VALUES (?, ?, 0.9, 40, 'stable', 'e', ?)",
                    (f"d{i}", f"dom{i}", stamp),
                )
            await conn.commit()
            yield conn, stamp[:10]

    async def test_does_not_call_a_months_old_map_current(self, aged_db):
        from genesis.ego.user_context import UserEgoContextBuilder

        db, stamp = aged_db
        out = await UserEgoContextBuilder(db=db)._capability_performance_section(
            depth="light"
        )
        # The rows ARE kept — that is the dead-refresh guarantee working.
        assert "3 domains" in out
        # ...but they must not be described as current.
        assert "current, qualifying evidence" not in out
        assert f"newest evidence {stamp}" in out, (
            "the light line asserts freshness it cannot support"
        )
