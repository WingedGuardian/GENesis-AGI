"""Tests for genesis.ego.user_context — UserEgoContextBuilder."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.schema import TABLES
from genesis.ego.user_context import UserEgoContextBuilder


@pytest.fixture
async def db():
    """In-memory DB with tables needed by UserEgoContextBuilder."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE user_model_cache (
                id              TEXT PRIMARY KEY DEFAULT 'current',
                person_id       TEXT,
                model_json      TEXT NOT NULL,
                version         INTEGER NOT NULL DEFAULT 1,
                synthesized_at  TEXT NOT NULL,
                synthesized_by  TEXT NOT NULL,
                evidence_count  INTEGER NOT NULL DEFAULT 0,
                last_change_type TEXT,
                last_changed_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE cc_sessions (
                id               TEXT PRIMARY KEY,
                session_type     TEXT NOT NULL,
                user_id          TEXT,
                channel          TEXT,
                model            TEXT NOT NULL,
                effort           TEXT NOT NULL DEFAULT 'medium',
                status           TEXT NOT NULL DEFAULT 'active',
                pid              INTEGER,
                started_at       TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                checkpointed_at  TEXT,
                completed_at     TEXT,
                source_tag       TEXT NOT NULL DEFAULT 'foreground',
                metadata         TEXT,
                cc_session_id    TEXT,
                thread_id        TEXT,
                topic            TEXT DEFAULT ''
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
                memory_basis    TEXT DEFAULT ''
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
            CREATE TABLE inbox_items (
                id             TEXT PRIMARY KEY,
                file_path      TEXT NOT NULL,
                content_hash   TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                batch_id       TEXT,
                response_path  TEXT,
                created_at     TEXT NOT NULL,
                processed_at   TEXT,
                error_message  TEXT,
                retry_count    INTEGER NOT NULL DEFAULT 0,
                evaluated_content TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE awareness_ticks (
                id              TEXT PRIMARY KEY,
                signals_json    TEXT,
                scores_json     TEXT,
                classified_depth TEXT,
                trigger_reason  TEXT,
                created_at      TEXT NOT NULL
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
            "ollama": {"status": "healthy", "latency_ms": 80.0},
        },
        "resilience": "healthy",
        "queues": {},
        "surplus": {},
        "conversation": {
            "status": "idle",
            "last_user_message_age_s": 600.0,
            "recent_user_turns": 5,
            "recent_assistant_turns": 4,
        },
        "cost": {"daily_total": 0.50},
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


class TestUserEgoContextBuilder:
    @pytest.mark.asyncio
    async def test_build_produces_markdown(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert isinstance(result, str)
        assert "# USER_EGO_CONTEXT" in result
        assert "What Does the User Need?" in result

    # ── User Model ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_user_model_section(self, db, mock_health_data, capabilities):
        model_data = json.dumps({
            "active_projects": ["Genesis v3", "Career module"],
            "professional_role": "AI engineer",
            "interests": ["autonomy", "AGI"],
        })
        await db.execute(
            "INSERT INTO user_model_cache "
            "(id, model_json, version, synthesized_at, synthesized_by, evidence_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("current", model_data, 3, "2026-04-24T09:00:00", "reflection", 42),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## User Profile" in result
        assert "v3" in result
        assert "42 evidence points" in result
        assert "active_projects" in result
        assert "Genesis v3" in result
        assert "professional_role" in result
        assert "AI engineer" in result

    @pytest.mark.asyncio
    async def test_user_model_empty(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No user model synthesized yet" in result

    @pytest.mark.asyncio
    async def test_user_model_empty_json(self, db, mock_health_data, capabilities):
        """Model row exists but model_json is empty dict."""
        await db.execute(
            "INSERT INTO user_model_cache "
            "(id, model_json, version, synthesized_at, synthesized_by, evidence_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("current", "{}", 1, "2026-04-24T09:00:00", "reflection", 0),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Model data empty" in result

    # ── Recent Conversations ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_recent_conversations_section(self, db, mock_health_data, capabilities):
        await db.execute(
            "INSERT INTO cc_sessions "
            "(id, session_type, model, started_at, last_activity_at, source_tag, topic) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'), ?, ?)",
            ("s1", "foreground", "opus-4", "foreground", "Debug ego dispatch"),
        )
        await db.execute(
            "INSERT INTO cc_sessions "
            "(id, session_type, model, started_at, last_activity_at, source_tag, topic) "
            "VALUES (?, ?, ?, datetime('now', '-1 hour'), datetime('now', '-1 hour'), ?, ?)",
            ("s2", "foreground", "sonnet-4", "foreground", "Review browser stealth"),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## Recent Conversations" in result
        assert "Debug ego dispatch" in result
        assert "Review browser stealth" in result
        assert "2 sessions" in result

    @pytest.mark.asyncio
    async def test_recent_conversations_empty(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No foreground sessions in last 48h" in result

    @pytest.mark.asyncio
    async def test_recent_conversations_excludes_background(
        self, db, mock_health_data, capabilities,
    ):
        """Background sessions should not appear (source_tag != foreground)."""
        await db.execute(
            "INSERT INTO cc_sessions "
            "(id, session_type, model, started_at, last_activity_at, source_tag, topic) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'), ?, ?)",
            ("s3", "background_reflection", "sonnet-4", "background", "Nightly reflection"),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Nightly reflection" not in result

    # ── User-World Observations ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_world_snapshot_with_signals(self, db, mock_health_data, capabilities):
        """World snapshot surfaces user-world observation signals."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs1", "recon", "finding", "email_recon", "New job posting at Acme", "high", 0),
        )
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs2", "inbox", "user_signal", "inbox", "User received project update", "medium", 0),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "User's World" in result
        assert "New job posting at Acme" in result
        assert "User received project update" in result

    @pytest.mark.asyncio
    async def test_world_snapshot_excludes_non_signal_types(
        self, db, mock_health_data, capabilities,
    ):
        """World snapshot only surfaces user_signal/finding/interaction_theme types."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs3", "sentinel", "awareness_tick", "routine", "Health check passed", "low", 0),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health check passed" not in result

    @pytest.mark.asyncio
    async def test_world_snapshot_empty(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No world model data yet" in result or "User's World" in result

    @pytest.mark.asyncio
    async def test_resolved_observations_excluded(self, db, mock_health_data, capabilities):
        """Resolved observations should not appear."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs5", "recon", "finding", "email_recon", "Old resolved item", "high", 1),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Old resolved item" not in result

    # ── Escalations ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_escalations_section(self, db, mock_health_data, capabilities):
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                "esc1", "genesis_ego", "escalation_to_user_ego", "infrastructure",
                "Qdrant backup failed twice — needs user decision", "high", 0,
            ),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Genesis Ego Escalations" in result
        assert "Qdrant backup failed twice" in result
        assert "[high]" in result

    @pytest.mark.asyncio
    async def test_escalations_empty(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No escalations from Genesis ego" in result

    # ── Capabilities ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_capabilities_section(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## Genesis Capabilities" in result
        assert "**db**" in result
        assert "**router**" in result
        assert "**memory**" in result
        assert "**ego**" in result

    @pytest.mark.asyncio
    async def test_capabilities_empty(self, db, mock_health_data):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities={},
        )
        result = await builder.build()
        assert "No capabilities registered" in result

    # ── System Status (removed from user ego) ─────────────────────────

    @pytest.mark.asyncio
    async def test_no_system_status_section(self, db, mock_health_data, capabilities):
        """System status is intentionally excluded from user ego context.

        User ego has no jurisdiction over Genesis health — system issues
        reach it only via genesis ego escalations.
        """
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## System Status" not in result

    # ── Output Contract ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_output_contract_section(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Output Contract" in result
        assert "proposals" in result
        assert "focus_summary" in result
        assert "JSON" in result
        assert "morning_report" in result

    # ── Integration ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_all_sections_present(self, db, mock_health_data, capabilities):
        """Verify all expected section headers appear in the output."""
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        expected_sections = [
            "## User Profile",
            "## User Activity Pulse",
            "## Recent Conversations",
            "## User Goals",
            "## User's World",
            "## Backlogs",
            "## Genesis Ego Escalations",
            "## Genesis Capabilities",
            "## Open Threads",
            "## Active Proposals",
            "## Recurring Patterns (72h)",
            "## Goal Progress (7d)",
            "## Output Contract",
        ]
        for section in expected_sections:
            assert section in result, f"Missing section: {section}"

    @pytest.mark.asyncio
    async def test_user_goals_renders_id(self, db, mock_health_data, capabilities):
        """Goal IDs should appear in the rendered goals section."""
        await db.execute(TABLES["user_goals"])
        await db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, confidence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("goal-abc-123", "Land AI engineering role", "career", "high", "active", 0.8),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "id=goal-abc-123" in result
        assert "Land AI engineering role" in result

    @pytest.mark.asyncio
    async def test_goal_progress_with_executed_proposals(
        self, db, mock_health_data, capabilities,
    ):
        """Goal progress section shows executed proposals grouped by goal."""
        await db.execute(TABLES["user_goals"])
        # Fixture creates ego_proposals inline without goal_id — add the column
        await db.execute("ALTER TABLE ego_proposals ADD COLUMN goal_id TEXT")
        await db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, confidence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("goal-xyz", "Ship Genesis v4", "project", "high", "active", 0.7),
        )
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, content, status, goal_id, "
            " user_response, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            ("prop-1", "investigate", "Research v4 architecture",
             "executed", "goal-xyz", "session:abc12345|completed:Research done"),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## Goal Progress" in result
        assert "Ship Genesis v4" in result
        assert "Research v4 architecture" in result
        assert "id=goal-xyz" in result

    @pytest.mark.asyncio
    async def test_goal_progress_empty(self, db, mock_health_data, capabilities):
        """Goal progress section renders gracefully when no data."""
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## Goal Progress" in result

    @pytest.mark.asyncio
    async def test_user_model_long_value_truncated(
        self, db, mock_health_data, capabilities,
    ):
        """Long model field values are truncated to 300 chars."""
        long_val = "X" * 500
        model_data = json.dumps({"active_projects": long_val})
        await db.execute(
            "INSERT INTO user_model_cache "
            "(id, model_json, version, synthesized_at, synthesized_by, evidence_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("current", model_data, 1, "2026-04-24T09:00:00", "reflection", 1),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "..." in result
        assert "X" * 500 not in result

    @pytest.mark.asyncio
    async def test_observation_priority_ordering(self, db, mock_health_data, capabilities):
        """Higher priority observations should appear before lower."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs_lo", "recon", "finding", "email_recon", "LOW_ITEM", "low", 0),
        )
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs_hi", "recon", "finding", "email_recon", "CRITICAL_ITEM", "critical", 0),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        crit_pos = result.index("CRITICAL_ITEM")
        low_pos = result.index("LOW_ITEM")
        assert crit_pos < low_pos, "Critical items should appear before low-priority items"

    # ── User Activity Pulse (3a) ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_activity_pulse_with_signals(self, db, mock_health_data, capabilities):
        """Non-zero user-facing signals are surfaced as prose."""
        signals = json.dumps([
            {"name": "user_goal_staleness", "value": 0.5, "source": "follow_ups+user_model"},
            {"name": "user_session_pattern", "value": 0.8, "source": "cc_sessions"},
            {"name": "conversations_since_reflection", "value": 3.0, "source": "cc_sessions"},
            {"name": "software_error_spike", "value": 0.1, "source": "circuit_breakers"},
        ])
        await db.execute(
            "INSERT INTO awareness_ticks "
            "(id, signals_json, classified_depth, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("tick1", signals, "Micro", ),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## User Activity Pulse" in result
        # User-facing signals present
        assert "User Goal Staleness" in result
        assert "moderately stale" in result
        assert "User Session Pattern" in result
        assert "significantly below" in result
        assert "Conversations Since Reflection" in result
        # Genesis-internal signal excluded
        assert "Software Error Spike" not in result
        assert "circuit_breakers" not in result

    @pytest.mark.asyncio
    async def test_activity_pulse_all_zero(self, db, mock_health_data, capabilities):
        """All user-facing signals at 0.0 → nominal message."""
        signals = json.dumps([
            {"name": "user_goal_staleness", "value": 0.0, "source": "follow_ups"},
            {"name": "user_session_pattern", "value": 0.0, "source": "cc_sessions"},
        ])
        await db.execute(
            "INSERT INTO awareness_ticks "
            "(id, signals_json, classified_depth, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("tick2", signals, "Micro", ),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "All user activity signals nominal" in result

    @pytest.mark.asyncio
    async def test_activity_pulse_no_ticks(self, db, mock_health_data, capabilities):
        """No awareness ticks recorded → graceful fallback."""
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No awareness ticks recorded" in result

    @pytest.mark.asyncio
    async def test_activity_pulse_dict_format(self, db, mock_health_data, capabilities):
        """signals_json stored as dict (legacy format) still works."""
        signals = json.dumps({
            "user_goal_staleness": {"name": "user_goal_staleness", "value": 0.9, "source": "follow_ups"},
        })
        await db.execute(
            "INSERT INTO awareness_ticks "
            "(id, signals_json, classified_depth, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("tick3", signals, "Micro", ),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "User Goal Staleness" in result
        assert "significantly stale" in result

    # ── Model Freshness Warning (3d) ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_model_freshness_warning_shown(self, db, mock_health_data, capabilities):
        """Stale model + recent activity → warning shown."""
        # Model synthesized 5 days ago
        old_date = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        model_data = json.dumps({"active_projects": ["Genesis v3"]})
        await db.execute(
            "INSERT INTO user_model_cache "
            "(id, model_json, version, synthesized_at, synthesized_by, evidence_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("current", model_data, 3, old_date, "reflection", 42),
        )
        # 5 foreground sessions since synthesis
        for i in range(5):
            await db.execute(
                "INSERT INTO cc_sessions "
                "(id, session_type, model, started_at, last_activity_at, source_tag, topic) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now'), ?, ?)",
                (f"s{i}", "foreground", "opus-4", "foreground", f"Session {i}"),
            )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Model may be stale" in result
        assert "5d old" in result
        assert "5 conversations since" in result

    @pytest.mark.asyncio
    async def test_model_freshness_no_warning_when_fresh(
        self, db, mock_health_data, capabilities,
    ):
        """Recent model → no warning."""
        model_data = json.dumps({"active_projects": ["Genesis v3"]})
        await db.execute(
            "INSERT INTO user_model_cache "
            "(id, model_json, version, synthesized_at, synthesized_by, evidence_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("current", model_data, 3, datetime.now(UTC).isoformat(), "reflection", 42),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Model may be stale" not in result

    @pytest.mark.asyncio
    async def test_model_freshness_no_warning_few_sessions(
        self, db, mock_health_data, capabilities,
    ):
        """Old model but few sessions since → no warning (not enough signal)."""
        old_date = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        model_data = json.dumps({"active_projects": ["Genesis v3"]})
        await db.execute(
            "INSERT INTO user_model_cache "
            "(id, model_json, version, synthesized_at, synthesized_by, evidence_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("current", model_data, 3, old_date, "reflection", 42),
        )
        # Only 1 session since (below threshold of 3)
        await db.execute(
            "INSERT INTO cc_sessions "
            "(id, session_type, model, started_at, last_activity_at, source_tag, topic) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'), ?, ?)",
            ("s1", "foreground", "opus-4", "foreground", "One session"),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Model may be stale" not in result

    # ── Backlog Summary (3c) ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_backlog_with_items(self, db, mock_health_data, capabilities):
        """Backlogs section shows counts and oldest ages."""
        old_date = (datetime.now(UTC) - timedelta(days=6)).isoformat()
        await db.execute(
            "INSERT INTO inbox_items "
            "(id, file_path, content_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("i1", "/inbox/test.md", "abc", "pending", old_date),
        )
        await db.execute(
            "INSERT INTO inbox_items "
            "(id, file_path, content_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("i2", "/inbox/test2.md", "def", "processing", datetime.now(UTC).isoformat()),
        )
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("f1", "recon", "finding", "finding", "Interesting article", "medium", 0, old_date),
        )
        await db.execute(
            "INSERT INTO follow_ups "
            "(id, source, content, strategy, status, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("fu1", "ego", "Review draft", "user_input_needed", "pending", "high", old_date),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## Backlogs" in result
        assert "**Inbox**: 2 pending" in result
        assert "6d ago" in result
        assert "**Recon findings**: 1 pending" in result
        assert "**Awaiting user input**: 1 pending" in result

    @pytest.mark.asyncio
    async def test_backlog_all_clear(self, db, mock_health_data, capabilities):
        """Empty backlogs show 'all clear' message."""
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "All backlogs clear" in result

    @pytest.mark.asyncio
    async def test_backlog_excludes_completed(self, db, mock_health_data, capabilities):
        """Completed inbox items don't count."""
        await db.execute(
            "INSERT INTO inbox_items "
            "(id, file_path, content_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("i3", "/inbox/done.md", "ghi", "completed", datetime.now(UTC).isoformat()),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "All backlogs clear" in result

    # ── Recurring Patterns (72h) ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_recurring_patterns_detected(self, db, mock_health_data, capabilities):
        """3+ observations of same (type, category) appear as pattern."""
        for i in range(4):
            await db.execute(
                "INSERT INTO observations "
                "(id, source, type, category, content, priority, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                (f"pat{i}", "test", "finding", "email_recon",
                 f"Recruiter email #{i}", "medium"),
            )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder._recurring_patterns_section()
        assert "finding/email_recon" in result
        assert "\u00d74" in result

    @pytest.mark.asyncio
    async def test_recurring_patterns_below_threshold(self, db, mock_health_data, capabilities):
        """Fewer than 3 observations do NOT trigger pattern detection."""
        for i in range(2):
            await db.execute(
                "INSERT INTO observations "
                "(id, source, type, category, content, priority, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                (f"few{i}", "test", "task_detected", "user_request",
                 f"Task #{i}", "low"),
            )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder._recurring_patterns_section()
        assert "No recurring patterns detected" in result

    @pytest.mark.asyncio
    async def test_recurring_patterns_excludes_resolved(self, db, mock_health_data, capabilities):
        """Resolved observations are excluded from pattern detection."""
        for i in range(4):
            await db.execute(
                "INSERT INTO observations "
                "(id, source, type, category, content, priority, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'))",
                (f"res{i}", "test", "finding", "email_recon",
                 f"Resolved email #{i}", "medium"),
            )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder._recurring_patterns_section()
        assert "No recurring patterns detected" in result

    # ── Goal Deep Dive ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_goal_deep_dive_empty_without_focus_id(
        self, db, mock_health_data, capabilities,
    ):
        """Deep dive returns empty string when no focus_id is set."""
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        await builder.build()
        result = await builder._goal_deep_dive_section()
        assert result == ""

    @pytest.mark.asyncio
    async def test_goal_deep_dive_renders_with_focus_id(
        self, db, mock_health_data, capabilities,
    ):
        """Deep dive renders full goal info when focus_id is set."""
        await db.execute(TABLES["user_goals"])
        await db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, description, "
            " timeline, confidence, created_at, updated_at, progress_notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("goal-dd1", "Master Kubernetes", "learning", "high", "active",
             "Learn K8s for deployment", "2026-06-30", 0.6,
             "2026-05-01", "2026-05-10",
             json.dumps([
                 {"date": "2026-05-05", "note": "Completed pods tutorial"},
                 {"date": "2026-05-10", "note": "Started services chapter"},
             ])),
        )
        await db.commit()

        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        builder._current_focus_id = "goal-dd1"
        result = await builder._goal_deep_dive_section()

        assert "Goal Deep Dive: Master Kubernetes" in result
        assert "goal-dd1" in result
        assert "learning" in result
        assert "high" in result
        assert "2026-06-30" in result
        assert "Completed pods tutorial" in result
        assert "Started services chapter" in result

    @pytest.mark.asyncio
    async def test_goal_deep_dive_shows_proposals(
        self, db, mock_health_data, capabilities,
    ):
        """Deep dive shows linked proposals."""
        await db.execute(TABLES["user_goals"])
        # Fixture creates ego_proposals inline without goal_id — add column
        import contextlib
        with contextlib.suppress(Exception):
            await db.execute(
                "ALTER TABLE ego_proposals ADD COLUMN goal_id TEXT",
            )
        await db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goal-dd2", "Fix Auth", "project", "critical", "active",
             "2026-05-01", "2026-05-10"),
        )
        await db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, content, status, goal_id, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("prop-1", "investigate", "Audit auth middleware",
             "executed", "goal-dd2", 0.9, "2026-05-12"),
        )
        await db.commit()

        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        builder._current_focus_id = "goal-dd2"
        result = await builder._goal_deep_dive_section()

        assert "Goal-Linked Proposals" in result
        assert "Audit auth middleware" in result
        assert "completed" in result  # NEUTRAL_STATUS maps executed → completed

    @pytest.mark.asyncio
    async def test_goal_deep_dive_skip_depth(
        self, db, mock_health_data, capabilities,
    ):
        """Deep dive returns empty when depth is 'skip'."""
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        builder._current_focus_id = "goal-123"
        result = await builder._goal_deep_dive_section(depth="skip")
        assert result == ""
