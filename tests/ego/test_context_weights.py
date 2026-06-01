"""Tests for context weight filtering in ego context builders.

Covers: skip/light/deep/always behavior, _ALWAYS_SECTIONS enforcement,
weight threading through compaction, and backward compatibility.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.ego.user_context import UserEgoContextBuilder

# ---------------------------------------------------------------------------
# Shared DB fixture (minimal schema for context builders)
# ---------------------------------------------------------------------------

_MINIMAL_TABLES = [
    """CREATE TABLE IF NOT EXISTS user_model_cache (
        id TEXT PRIMARY KEY DEFAULT 'current',
        model_json TEXT, version INTEGER DEFAULT 1,
        synthesized_at TEXT, synthesized_by TEXT DEFAULT '',
        evidence_count INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS cc_sessions (
        id TEXT PRIMARY KEY, session_type TEXT DEFAULT '',
        model TEXT DEFAULT '', effort TEXT DEFAULT 'medium',
        status TEXT DEFAULT 'active', started_at TEXT NOT NULL,
        last_activity_at TEXT NOT NULL, source_tag TEXT DEFAULT 'foreground',
        topic TEXT DEFAULT '', user_id TEXT, channel TEXT, pid INTEGER,
        checkpointed_at TEXT, completed_at TEXT, metadata TEXT,
        cc_session_id TEXT, thread_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, type TEXT NOT NULL,
        category TEXT, content TEXT NOT NULL, priority TEXT DEFAULT 'medium',
        speculative INTEGER DEFAULT 0, retrieved_count INTEGER DEFAULT 0,
        influenced_action INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0,
        resolved_at TEXT, resolution_notes TEXT, created_at TEXT NOT NULL,
        expires_at TEXT, content_hash TEXT, person_id TEXT,
        surfaced_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ego_proposals (
        id TEXT PRIMARY KEY, action_type TEXT NOT NULL,
        action_category TEXT DEFAULT '', content TEXT NOT NULL,
        rationale TEXT DEFAULT '', confidence REAL DEFAULT 0.0,
        urgency TEXT DEFAULT 'normal', alternatives TEXT DEFAULT '',
        status TEXT DEFAULT 'pending', user_response TEXT,
        cycle_id TEXT, batch_id TEXT, created_at TEXT NOT NULL,
        resolved_at TEXT, expires_at TEXT, rank INTEGER,
        execution_plan TEXT, recurring INTEGER DEFAULT 0,
        memory_basis TEXT DEFAULT '', realist_verdict TEXT,
        realist_reasoning TEXT, goal_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS inbox_items (
        id TEXT PRIMARY KEY, file_path TEXT DEFAULT '', content_hash TEXT DEFAULT '',
        status TEXT DEFAULT 'pending', created_at TEXT NOT NULL,
        batch_id TEXT, response_path TEXT, processed_at TEXT,
        error_message TEXT, retry_count INTEGER DEFAULT 0, evaluated_content TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS awareness_ticks (
        id TEXT PRIMARY KEY, signals_json TEXT, scores_json TEXT,
        classified_depth TEXT, trigger_reason TEXT, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS follow_ups (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, content TEXT NOT NULL,
        reason TEXT, strategy TEXT NOT NULL, status TEXT DEFAULT 'pending',
        priority TEXT DEFAULT 'medium', created_at TEXT NOT NULL,
        completed_at TEXT, resolution_notes TEXT, blocked_reason TEXT,
        escalated_to TEXT, source_session TEXT, scheduled_at TEXT,
        linked_task_id TEXT, pinned INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS user_goals (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
        priority TEXT DEFAULT 'medium', status TEXT DEFAULT 'active',
        category TEXT DEFAULT '', progress_notes TEXT DEFAULT '[]',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ego_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ego_cycles (
        id TEXT PRIMARY KEY, ego_source TEXT DEFAULT '',
        focus_summary TEXT, proposals_json TEXT DEFAULT '[]',
        model TEXT, effort TEXT, duration_s REAL,
        created_at TEXT NOT NULL
    )""",
]


@pytest.fixture
async def db():
    """Minimal in-memory DB for context builder tests."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for stmt in _MINIMAL_TABLES:
            await conn.execute(stmt)
        await conn.commit()
        yield conn


@pytest.fixture
def user_builder(db):
    return UserEgoContextBuilder(db=db, capabilities={"test_cap": "A test capability"})


# ---------------------------------------------------------------------------
# UserEgoContextBuilder weight tests
# ---------------------------------------------------------------------------


class TestUserContextWeights:
    """Context weight filtering for the user ego context builder."""

    async def test_build_default_all_sections_present(self, user_builder):
        """No weights = all sections present (backward compat)."""
        result = await user_builder.build()
        assert "USER_EGO_CONTEXT" in result
        assert "## User Profile" in result
        assert "## Genesis Capabilities" in result
        assert "## Output Contract" in result

    async def test_build_with_none_weights_same_as_default(self, user_builder):
        """Passing context_weights=None is identical to no argument."""
        default = await user_builder.build()
        explicit_none = await user_builder.build(context_weights=None)
        assert default == explicit_none

    async def test_skip_removes_section(self, user_builder):
        """A section with weight 'skip' should not appear."""
        result = await user_builder.build(context_weights={"capabilities": "skip"})
        assert "## Genesis Capabilities" not in result
        # Other sections still present
        assert "## User Profile" in result

    async def test_skip_multiple_sections(self, user_builder):
        """Multiple sections can be skipped simultaneously."""
        weights = {
            "capabilities": "skip",
            "capability_performance": "skip",
            "recurring_patterns": "skip",
        }
        result = await user_builder.build(context_weights=weights)
        assert "## Genesis Capabilities" not in result
        # Core sections still present
        assert "## User Profile" in result
        assert "## Output Contract" in result

    async def test_light_returns_shorter_output(self, user_builder):
        """Light mode should produce output no longer than deep mode."""
        deep = await user_builder.build()
        light_weights = {
            "goals": "light",
            "world_snapshot": "light",
            "activity_pulse": "light",
            "recent_conversations": "light",
            "backlog_summary": "light",
            "capabilities": "light",
        }
        light = await user_builder.build(context_weights=light_weights)
        assert len(light) <= len(deep)

    async def test_always_sections_never_skipped(self, user_builder):
        """Sections in _ALWAYS_SECTIONS cannot be skipped."""
        weights = {
            "user_model": "skip",
            "intentions": "skip",
            "directives": "skip",
            "output_contract": "skip",
        }
        result = await user_builder.build(context_weights=weights)
        assert "## User Profile" in result
        assert "## Output Contract" in result

    async def test_always_sections_not_lightened(self, user_builder):
        """Sections in _ALWAYS_SECTIONS render full depth even with 'light'."""
        weights = {"output_contract": "light"}
        result = await user_builder.build(context_weights=weights)
        # Output contract should have full JSON schema
        assert "proposals" in result
        assert "focus_summary" in result

    async def test_unknown_weight_key_defaults_to_deep(self, user_builder):
        """Sections not in weight dict render at full depth."""
        weights = {"capabilities": "skip"}
        result = await user_builder.build(context_weights=weights)
        assert "## User Goals" in result

    async def test_capabilities_light_mode(self, user_builder):
        """Light capabilities shows count, not full list."""
        light = await user_builder.build(context_weights={"capabilities": "light"})
        assert "1 capabilities available" in light
        # Should NOT contain the full description
        assert "A test capability" not in light


# ---------------------------------------------------------------------------
# GenesisEgoContextBuilder weight tests
# ---------------------------------------------------------------------------


class TestGenesisContextWeights:
    """Context weight filtering for the genesis ego context builder."""

    @pytest.fixture
    def genesis_builder(self, db):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder
        return GenesisEgoContextBuilder(db=db, capabilities={"test": "A cap"})

    async def test_build_default_all_sections(self, genesis_builder):
        """No weights = all genesis sections present."""
        result = await genesis_builder.build()
        assert "GENESIS_EGO_CONTEXT" in result
        assert "## Output Contract" in result

    async def test_build_with_none_backward_compat(self, genesis_builder):
        """context_weights=None is backward compatible."""
        default = await genesis_builder.build()
        explicit = await genesis_builder.build(context_weights=None)
        assert default == explicit

    async def test_user_ego_keys_ignored(self, genesis_builder):
        """User ego weight keys that don't exist in genesis are ignored."""
        weights = {"goals": "skip", "world_snapshot": "skip"}
        result = await genesis_builder.build(context_weights=weights)
        assert "GENESIS_EGO_CONTEXT" in result


# ---------------------------------------------------------------------------
# Compaction threading
# ---------------------------------------------------------------------------


class TestCompactionWeightThreading:
    """Verify context_weights flow through CompactionEngine to build()."""

    async def test_weights_reach_build(self, db):
        """assemble_context passes context_weights to build()."""
        from genesis.ego.compaction import CompactionEngine

        # Seed ego_state for mode lookup
        await db.execute(
            "INSERT INTO ego_state (key, value) VALUES (?, ?)",
            ("user_ego_mode", "active"),
        )
        await db.commit()

        engine = CompactionEngine(db=db)
        mock_builder = AsyncMock()
        mock_builder.build.return_value = "mock context"

        await engine.assemble_context(
            context_builder=mock_builder,
            context_weights={"goals": "light", "capabilities": "skip"},
        )

        mock_builder.build.assert_called_once_with(
            context_weights={"goals": "light", "capabilities": "skip"},
            focus_id=None,
        )

    async def test_always_sections_enforced_in_compaction(self, db):
        """Compaction upgrades always-section weights before passing to build()."""
        from genesis.ego.compaction import CompactionEngine

        await db.execute(
            "INSERT OR IGNORE INTO ego_state (key, value) VALUES (?, ?)",
            ("user_ego_mode", "active"),
        )
        await db.commit()

        engine = CompactionEngine(db=db)
        mock_builder = AsyncMock()
        mock_builder.build.return_value = "mock context"

        # Try to skip always-sections — compaction should upgrade them
        await engine.assemble_context(
            context_builder=mock_builder,
            context_weights={
                "user_model": "skip",
                "output_contract": "light",
                "goals": "light",
            },
        )

        passed_weights = mock_builder.build.call_args[1]["context_weights"]
        assert passed_weights["user_model"] == "deep"
        assert passed_weights["output_contract"] == "deep"
        assert passed_weights["goals"] == "light"  # non-always stays light

    async def test_none_weights_backward_compat(self, db):
        """assemble_context with no weights passes None to build()."""
        from genesis.ego.compaction import CompactionEngine

        await db.execute(
            "INSERT OR IGNORE INTO ego_state (key, value) VALUES (?, ?)",
            ("user_ego_mode", "active"),
        )
        await db.commit()

        engine = CompactionEngine(db=db)
        mock_builder = AsyncMock()
        mock_builder.build.return_value = "mock context"

        await engine.assemble_context(context_builder=mock_builder)
        mock_builder.build.assert_called_once_with(
            context_weights=None, focus_id=None,
        )


# ---------------------------------------------------------------------------
# Focus weight table validation
# ---------------------------------------------------------------------------


class TestFocusWeightTable:
    """Validate the FOCUS_CONTEXT_WEIGHTS structure."""

    def test_all_focus_types_have_all_sections(self):
        """Every focus type's weight dict covers all 19 sections."""
        from genesis.ego.focus import _ALL_SECTIONS, FOCUS_CONTEXT_WEIGHTS

        for focus_type, weights in FOCUS_CONTEXT_WEIGHTS.items():
            for section in _ALL_SECTIONS:
                assert section in weights, (
                    f"'{focus_type}' missing '{section}'"
                )

    def test_always_sections_are_always(self):
        """_ALWAYS_SECTIONS should be 'always' in every focus type."""
        from genesis.ego.focus import _ALWAYS_SECTIONS, FOCUS_CONTEXT_WEIGHTS

        for focus_type, weights in FOCUS_CONTEXT_WEIGHTS.items():
            for section in _ALWAYS_SECTIONS:
                assert weights[section] == "always", (
                    f"'{focus_type}': {section}={weights[section]}, expected 'always'"
                )

    def test_weight_values_valid(self):
        """All weight values should be one of the valid levels."""
        from genesis.ego.focus import FOCUS_CONTEXT_WEIGHTS

        valid = {"always", "deep", "light", "skip"}
        for focus_type, weights in FOCUS_CONTEXT_WEIGHTS.items():
            for section, value in weights.items():
                assert value in valid, (
                    f"'{focus_type}/{section}' has invalid weight '{value}'"
                )

    def test_revised_weights_applied(self):
        """Verify the 7 weight revisions from PR 3 planning session."""
        from genesis.ego.focus import FOCUS_CONTEXT_WEIGHTS

        # capabilities: skip → light for reactive, goal_review, escalation
        assert FOCUS_CONTEXT_WEIGHTS["reactive"]["capabilities"] == "light"
        assert FOCUS_CONTEXT_WEIGHTS["goal_review"]["capabilities"] == "light"
        assert FOCUS_CONTEXT_WEIGHTS["escalation"]["capabilities"] == "light"
        # capabilities stays skip for dispatch_outcome
        assert FOCUS_CONTEXT_WEIGHTS["dispatch_outcome"]["capabilities"] == "skip"

        # recurring_patterns: skip → light for goal_review
        assert FOCUS_CONTEXT_WEIGHTS["goal_review"]["recurring_patterns"] == "light"

        # follow_ups: light → deep for reactive
        assert FOCUS_CONTEXT_WEIGHTS["reactive"]["follow_ups"] == "deep"

        # proposal_history: skip → light for dispatch_outcome
        assert FOCUS_CONTEXT_WEIGHTS["dispatch_outcome"]["proposal_history"] == "light"

    def test_section_count(self):
        """_ALL_SECTIONS should have exactly 19 entries."""
        from genesis.ego.focus import _ALL_SECTIONS
        assert len(_ALL_SECTIONS) == 19

    def test_goal_deep_dive_deep_for_goal_review(self):
        """goal_deep_dive is 'deep' only during goal_review cycles."""
        from genesis.ego.focus import FOCUS_CONTEXT_WEIGHTS

        assert FOCUS_CONTEXT_WEIGHTS["goal_review"]["goal_deep_dive"] == "deep"

    def test_goal_deep_dive_skip_for_non_goal_review(self):
        """goal_deep_dive is 'skip' for all non-goal_review focus types."""
        from genesis.ego.focus import FOCUS_CONTEXT_WEIGHTS

        for focus_type in (
            "proactive", "daily_briefing", "reactive",
            "dispatch_outcome", "escalation",
        ):
            assert (
                FOCUS_CONTEXT_WEIGHTS[focus_type]["goal_deep_dive"] == "skip"
            ), f"goal_deep_dive should be 'skip' for {focus_type}"
