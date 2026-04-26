"""Tests for the ego compaction engine (now cycle storage + context assembly)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.compaction import CompactionEngine
from genesis.ego.types import EgoCycle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_cycles"])
        await conn.execute(TABLES["ego_state"])
        yield conn


@pytest.fixture
def engine(db):
    return CompactionEngine(db=db, focus_summary_key="ego_focus_summary")


@pytest.fixture
def mock_context_builder():
    """Mock EgoContextBuilder."""
    builder = AsyncMock()
    builder.build.return_value = "## Capabilities\n- [+] memory: ok\n"
    return builder


def _make_cycle(id: str, created_at: str = "2026-03-28T10:00:00Z", **kw) -> EgoCycle:
    """Helper to build an EgoCycle."""
    return EgoCycle(
        id=id,
        output_text=kw.get("output_text", f"output for {id}"),
        proposals_json=kw.get("proposals_json", "[]"),
        focus_summary=kw.get("focus_summary", f"focus {id}"),
        model_used=kw.get("model_used", "test-model"),
        cost_usd=kw.get("cost_usd", 0.01),
        input_tokens=kw.get("input_tokens", 100),
        output_tokens=kw.get("output_tokens", 50),
        duration_ms=kw.get("duration_ms", 500),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Store + retrieve
# ---------------------------------------------------------------------------


class TestStoreAndRetrieve:
    async def test_store_cycle_persists(self, engine, db):
        cycle = _make_cycle("c1")
        returned = await engine.store_cycle(cycle)
        assert returned == "c1"

        row = await ego_crud.get_cycle(db, "c1")
        assert row is not None
        assert row["output_text"] == "output for c1"

    async def test_store_multiple_cycles(self, engine, db):
        for i in range(3):
            await engine.store_cycle(_make_cycle(f"c{i}"))

        for i in range(3):
            row = await ego_crud.get_cycle(db, f"c{i}")
            assert row is not None


# ---------------------------------------------------------------------------
# assemble_context
# ---------------------------------------------------------------------------


class TestAssembleContext:
    async def test_empty_state(self, engine, mock_context_builder):
        """Fresh system: no previous focus, just context builder output."""
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "Capabilities" in ctx  # from mock builder
        assert "Previous Focus" not in ctx  # no stored focus

    async def test_with_previous_focus(self, engine, db, mock_context_builder):
        """When a previous focus exists, it appears in context."""
        await ego_crud.set_state(
            db, key="ego_focus_summary", value="investigating backlog",
        )
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "Previous Focus" in ctx
        assert "investigating backlog" in ctx
        assert "Capabilities" in ctx  # context builder still included

    async def test_context_builder_included(self, engine, mock_context_builder):
        """EgoContextBuilder output appears verbatim."""
        mock_context_builder.build.return_value = "FRESH_CONTEXT_SENTINEL"
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "FRESH_CONTEXT_SENTINEL" in ctx

    async def test_genesis_ego_focus_key(self, db, mock_context_builder):
        """Genesis ego uses its own focus key."""
        engine = CompactionEngine(
            db=db,
            focus_summary_key="genesis_ego_focus_summary",
        )
        await ego_crud.set_state(
            db, key="genesis_ego_focus_summary", value="system maintenance",
        )
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "system maintenance" in ctx

    async def test_legacy_params_accepted(self, db, mock_context_builder):
        """Router and window_size params accepted for backward compat."""
        engine = CompactionEngine(
            db=db,
            router=object(),  # should be ignored
            window_size=5,
            call_site_id="test",
        )
        # Should not raise
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "Capabilities" in ctx
