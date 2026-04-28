"""Tests for genesis.ego.user_context — UserEgoContextBuilder."""

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

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
                recurring       INTEGER DEFAULT 0
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
    async def test_user_world_observations(self, db, mock_health_data, capabilities):
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
            ("obs2", "inbox", "finding", "inbox", "User received project update", "medium", 0),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "User-World Signals" in result
        assert "New job posting at Acme" in result
        assert "User received project update" in result
        assert "2 signals" in result

    @pytest.mark.asyncio
    async def test_genesis_internal_observations_excluded(
        self, db, mock_health_data, capabilities,
    ):
        """Observations with internal categories (routine, anomaly) should NOT appear."""
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs3", "sentinel", "finding", "routine", "Health check passed", "low", 0),
        )
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, category, content, priority, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("obs4", "sentinel", "finding", "anomaly", "CPU spike detected", "medium", 0),
        )
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health check passed" not in result
        assert "CPU spike detected" not in result

    @pytest.mark.asyncio
    async def test_user_world_observations_empty(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "No user-world observations in last 7 days" in result

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

    # ── System Status ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_system_status_healthy(self, db, mock_health_data, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=mock_health_data, capabilities=capabilities,
        )
        result = await builder.build()
        assert "## System Status" in result
        assert "all systems nominal" in result

    @pytest.mark.asyncio
    async def test_system_status_degraded(self, db, capabilities):
        hd = AsyncMock()
        hd.snapshot.return_value = {
            "infrastructure": {
                "genesis.db": {"status": "healthy", "latency_ms": 0.5},
                "qdrant": {"status": "degraded", "latency_ms": 5000.0},
                "ollama": {"status": "down", "latency_ms": None},
            },
            "resilience": "degraded",
        }
        builder = UserEgoContextBuilder(
            db=db, health_data=hd, capabilities=capabilities,
        )
        result = await builder.build()
        assert "qdrant: degraded" in result
        assert "ollama: down" in result

    @pytest.mark.asyncio
    async def test_system_status_no_health_data(self, db, capabilities):
        builder = UserEgoContextBuilder(
            db=db, health_data=None, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health data not available" in result

    @pytest.mark.asyncio
    async def test_system_status_snapshot_failure(self, db, capabilities):
        hd = AsyncMock()
        hd.snapshot.side_effect = RuntimeError("DB locked")
        builder = UserEgoContextBuilder(
            db=db, health_data=hd, capabilities=capabilities,
        )
        result = await builder.build()
        assert "Health snapshot failed" in result

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
            "## Recent Conversations",
            "## User-World Signals",
            "## Genesis Ego Escalations",
            "## Genesis Capabilities",
            "## System Status",
            "## Open Threads",
            "## Output Contract",
        ]
        for section in expected_sections:
            assert section in result, f"Missing section: {section}"

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
