"""Tests for focus_next and focus_next_week steering through cognitive_state."""

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import cognitive_state
from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.output_router import OutputRouter
from genesis.reflection.types import DeepReflectionOutput


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "test.db")
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


# ── 2.3: focus_next stored in state_flags ────────────────────────────


class TestFocusNextStoredInCognitiveState:
    @pytest.mark.asyncio
    async def test_focus_next_stored_in_cognitive_state(self, db):
        """Deep reflection with focus_next writes to state_flags section."""
        output = DeepReflectionOutput(
            observations=["obs1"],
            focus_next="Monitor memory consolidation backlog",
            confidence=0.8,
        )
        router = OutputRouter()
        summary = await router.route(output, db)

        assert summary.get("focus_next_stored") is True

        # Verify it landed in state_flags
        row = await cognitive_state.get_current(db, "state_flags")
        assert row is not None
        assert "Deep Reflection Focus Directive" in row["content"]
        assert "Monitor memory consolidation backlog" in row["content"]
        assert row["generated_by"] == "deep_reflection"

    @pytest.mark.asyncio
    async def test_focus_next_empty_no_update(self, db):
        """Empty focus_next does not write to cognitive_state."""
        output = DeepReflectionOutput(
            observations=["obs1"],
            focus_next="",
            confidence=0.7,
        )
        router = OutputRouter()
        summary = await router.route(output, db)

        assert "focus_next_stored" not in summary

        row = await cognitive_state.get_current(db, "state_flags")
        assert row is None

    @pytest.mark.asyncio
    async def test_focus_next_none_no_update(self, db):
        """None/default focus_next does not write to cognitive_state."""
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.7,
        )
        router = OutputRouter()
        summary = await router.route(output, db)

        assert "focus_next_stored" not in summary


# ── 2.4: focus_next_week extracted from strategic output ─────────────


class TestFocusNextWeekFromStrategic:
    @pytest.mark.asyncio
    async def test_focus_next_week_extracted_from_strategic(self, db):
        """Strategic output with focus_next_week writes to pending_actions."""
        from genesis.awareness.types import Depth, TickResult
        from genesis.cc.reflection_bridge import CCReflectionBridge

        strategic_json = json.dumps({
            "observations": ["strategic obs"],
            "focus_next_week": "Prioritize resilience hardening and outreach calibration",
        })
        cc_output = SimpleNamespace(
            text=f"```json\n{strategic_json}\n```",
            is_error=False,
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
            model_used="opus",
            error_message=None,
        )

        tick = TickResult(
            tick_id="t1",
            timestamp=datetime.now(UTC).isoformat(),
            source="scheduled",
            signals=[],
            scores=[],
            classified_depth=None,
            trigger_reason=None,
        )

        CCReflectionBridge(
            session_manager=AsyncMock(),
            invoker=AsyncMock(),
            db=db,
        )

        from genesis.cc.reflection_bridge._output import store_reflection_output
        await store_reflection_output(
            Depth.STRATEGIC, tick, cc_output, db=db,
        )

        row = await cognitive_state.get_current(db, "pending_actions")
        assert row is not None
        assert "Strategic Focus (This Week)" in row["content"]
        assert "resilience hardening" in row["content"]
        assert row["generated_by"] == "strategic_reflection"

    @pytest.mark.asyncio
    async def test_focus_next_week_parse_failure_graceful(self, db):
        """Malformed strategic output does not crash."""
        from genesis.awareness.types import Depth, TickResult
        from genesis.cc.reflection_bridge import CCReflectionBridge

        cc_output = SimpleNamespace(
            text="This is not JSON at all, just free text rambling",
            is_error=False,
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
            model_used="opus",
            error_message=None,
        )

        tick = TickResult(
            tick_id="t2",
            timestamp=datetime.now(UTC).isoformat(),
            source="scheduled",
            signals=[],
            scores=[],
            classified_depth=None,
            trigger_reason=None,
        )

        CCReflectionBridge(
            session_manager=AsyncMock(),
            invoker=AsyncMock(),
            db=db,
        )

        # Should not raise
        from genesis.cc.reflection_bridge._output import store_reflection_output
        await store_reflection_output(
            Depth.STRATEGIC, tick, cc_output, db=db,
        )

        # No focus stored since parse failed
        row = await cognitive_state.get_current(db, "pending_actions")
        assert row is None

    @pytest.mark.asyncio
    async def test_focus_next_week_not_stored_for_deep(self, db):
        """Deep reflection output does not try to extract focus_next_week."""
        from genesis.awareness.types import Depth, TickResult
        from genesis.cc.reflection_bridge import CCReflectionBridge

        deep_json = json.dumps({
            "observations": ["deep obs"],
            "focus_next_week": "This should be ignored for deep depth",
        })
        cc_output = SimpleNamespace(
            text=f"```json\n{deep_json}\n```",
            is_error=False,
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
            model_used="sonnet",
            error_message=None,
        )

        tick = TickResult(
            tick_id="t3",
            timestamp=datetime.now(UTC).isoformat(),
            source="scheduled",
            signals=[],
            scores=[],
            classified_depth=None,
            trigger_reason=None,
        )

        CCReflectionBridge(
            session_manager=AsyncMock(),
            invoker=AsyncMock(),
            db=db,
        )

        from genesis.cc.reflection_bridge._output import store_reflection_output
        await store_reflection_output(
            Depth.DEEP, tick, cc_output, db=db,
        )

        row = await cognitive_state.get_current(db, "pending_actions")
        assert row is None


# ── Render includes both focus directives ────────────────────────────


class TestCognitiveStateRenderIncludesFocus:
    @pytest.mark.asyncio
    async def test_cognitive_state_render_includes_focus(self, db):
        """render() includes both focus_next (state_flags) and focus_next_week (pending_actions)."""
        now = datetime.now(UTC).isoformat()

        # Store focus_next in state_flags
        await cognitive_state.replace_section(
            db,
            section="state_flags",
            id=str(uuid.uuid4()),
            content="## Deep Reflection Focus Directive\nWatch memory backlog",
            generated_by="deep_reflection",
            created_at=now,
        )

        # Store focus_next_week in pending_actions
        await cognitive_state.replace_section(
            db,
            section="pending_actions",
            id=str(uuid.uuid4()),
            content="## Strategic Focus (This Week)\nHarden resilience layer",
            generated_by="strategic_reflection",
            created_at=now,
        )

        rendered = await cognitive_state.render(db)

        assert "Deep Reflection Focus Directive" in rendered
        assert "Watch memory backlog" in rendered
        assert "Strategic Focus (This Week)" in rendered
        assert "Harden resilience layer" in rendered
