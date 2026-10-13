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
        ego_source=kw.get("ego_source", ""),
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
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
        assert "Capabilities" in ctx  # from mock builder
        assert "Current System State" not in ctx  # no stored focus

    async def test_with_previous_focus(self, engine, db, mock_context_builder):
        """When a computed focus exists, it appears in context."""
        await ego_crud.set_state(
            db, key="ego_focus_summary", value="investigating backlog",
        )
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
        assert "Current System State" in ctx
        assert "investigating backlog" in ctx
        assert "Capabilities" in ctx  # context builder still included

    async def test_context_builder_included(self, engine, mock_context_builder):
        """EgoContextBuilder output appears verbatim."""
        mock_context_builder.build.return_value = "FRESH_CONTEXT_SENTINEL"
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
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
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
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
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
        assert "Capabilities" in ctx


class TestModeInjection:
    async def test_default_mode_is_active(self, engine, mock_context_builder):
        """Without a stored mode, context includes 'ACTIVE'."""
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
        assert "Operating Mode: ACTIVE" in ctx
        assert "system-level control parameter" in ctx

    async def test_stored_mode_injected(self, engine, db, mock_context_builder):
        """Stored mode appears in context."""
        from genesis.db.crud import ego as ego_crud
        await ego_crud.set_mode(db, "focused:beta-launch", ego_key="ego_mode")
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
        assert "FOCUSED:BETA-LAUNCH" in ctx

    async def test_mode_before_focus(self, engine, db, mock_context_builder):
        """Mode section appears before Current System State."""
        from genesis.db.crud import ego as ego_crud
        await ego_crud.set_state(db, key="ego_focus_summary", value="test focus")
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
        mode_pos = ctx.index("Operating Mode")
        focus_pos = ctx.index("Current System State")
        assert mode_pos < focus_pos

    async def test_genesis_ego_mode_key(self, db, mock_context_builder):
        """Genesis ego uses genesis_ego_mode key."""
        from genesis.db.crud import ego as ego_crud
        from genesis.ego.compaction import CompactionEngine
        engine = CompactionEngine(
            db=db,
            focus_summary_key="genesis_ego_focus_summary",
        )
        await ego_crud.set_mode(db, "urgent", ego_key="genesis_ego_mode")
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source="user_ego_cycle",
        )
        assert "URGENT" in ctx


# ---------------------------------------------------------------------------
# "Your Previous Assessment" is per-ego (issue #2695)
# ---------------------------------------------------------------------------

USER = "user_ego_cycle"
GENESIS = "genesis_ego_cycle"
_PREV = "## Your Previous Assessment"


def _prev_assessment(ctx: str) -> str | None:
    """The body of the previous-assessment section, or None when absent."""
    if _PREV not in ctx:
        return None
    return ctx.split(_PREV, 1)[1].split("\n## ", 1)[0].strip()


class TestPreviousAssessmentPerEgo:
    async def test_interleaved_egos_each_see_their_own(
        self, engine, mock_context_builder,
    ):
        """Interleaved cycles: each ego gets its OWN latest focus summary."""
        for i, (src, label) in enumerate(
            [(USER, "user-1"), (GENESIS, "genesis-1"),
             (USER, "user-2"), (GENESIS, "genesis-2")],
        ):
            await engine.store_cycle(_make_cycle(
                f"c{i}", f"2026-09-0{i + 1}T10:00:00Z",
                focus_summary=label, ego_source=src,
            ))

        # Newest row overall is the genesis ego's; the user ego must not see it.
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source=USER,
        )
        assert _prev_assessment(ctx) == "user-2"

        # Now the user ego is newest; the genesis ego must not see it.
        await engine.store_cycle(_make_cycle(
            "c9", "2026-09-09T10:00:00Z", focus_summary="user-3", ego_source=USER,
        ))
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source=GENESIS,
        )
        assert _prev_assessment(ctx) == "genesis-2"

    async def test_ego_with_no_cycles_gets_no_section(
        self, engine, mock_context_builder,
    ):
        """An ego with no cycles of its own is not shown the other ego's."""
        await engine.store_cycle(_make_cycle(
            "c1", focus_summary="genesis-only", ego_source=GENESIS,
        ))
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source=USER,
        )
        assert _prev_assessment(ctx) is None
        assert "genesis-only" not in ctx

    async def test_single_ego_history(self, engine, mock_context_builder):
        """One ego only: its newest cycle is its previous assessment."""
        for i in range(3):
            await engine.store_cycle(_make_cycle(
                f"c{i}", f"2026-09-0{i + 1}T10:00:00Z",
                focus_summary=f"user-{i}", ego_source=USER,
            ))
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source=USER,
        )
        assert _prev_assessment(ctx) == "user-2"

    async def test_legacy_untagged_rows_excluded_from_scoped_read(
        self, engine, mock_context_builder,
    ):
        """A newer row with empty ego_source is unattributable, so skipped."""
        await engine.store_cycle(_make_cycle(
            "c1", "2026-09-01T10:00:00Z", focus_summary="user-own", ego_source=USER,
        ))
        await engine.store_cycle(_make_cycle(
            "c2", "2026-09-02T10:00:00Z", focus_summary="legacy-untagged",
        ))
        ctx = await engine.assemble_context(
            context_builder=mock_context_builder, ego_source=USER,
        )
        assert _prev_assessment(ctx) == "user-own"

    async def test_ego_source_is_required(self, engine, mock_context_builder):
        """No unscoped mode: a caller that forgets the tag fails loudly."""
        with pytest.raises(TypeError):
            await engine.assemble_context(context_builder=mock_context_builder)
