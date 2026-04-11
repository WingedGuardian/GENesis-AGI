"""Tests for the ego compaction engine."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.compaction import (
    _NO_PRIOR_SUMMARY,
    CompactionEngine,
    _build_compaction_prompt,
)
from genesis.ego.types import EgoCycle

# ---------------------------------------------------------------------------
# Mock RoutingResult — lightweight stand-in that avoids importing the full
# routing package (which pulls in litellm, httpx, etc.).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MockRoutingResult:
    success: bool
    call_site_id: str = "8_ego_compaction"
    provider_used: str | None = "gemini-free"
    model_id: str | None = "gemini-3-flash"
    content: str | None = None
    attempts: int = 1
    fallback_used: bool = False
    failed_providers: tuple[str, ...] = ()
    error: str | None = None
    dead_lettered: bool = False
    input_tokens: int = 50
    output_tokens: int = 100
    cost_usd: float = 0.0


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
def mock_router():
    """Router that succeeds with a canned summary."""
    router = AsyncMock()
    router.route_call.return_value = _MockRoutingResult(
        success=True,
        content="- Active: observation backlog\n- Resolved: bridge issue",
    )
    return router


@pytest.fixture
def failing_router():
    """Router that always fails."""
    router = AsyncMock()
    router.route_call.return_value = _MockRoutingResult(
        success=False,
        content=None,
        error="All providers exhausted",
        attempts=4,
        failed_providers=("mistral-large-free", "groq-free", "gemini-free", "openrouter-free"),
    )
    return router


@pytest.fixture
def engine(db, mock_router):
    return CompactionEngine(db=db, router=mock_router, window_size=3)


@pytest.fixture
def failing_engine(db, failing_router):
    return CompactionEngine(db=db, router=failing_router, window_size=3)


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


async def _seed_cycles(engine: CompactionEngine, count: int) -> list[str]:
    """Create *count* cycles with ascending timestamps."""
    ids = []
    for i in range(count):
        cid = f"c{i:03d}"
        cycle = _make_cycle(cid, f"2026-01-{i + 1:02d}T00:00:00Z")
        await engine.store_cycle(cycle)
        ids.append(cid)
    return ids


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

    async def test_get_compacted_summary_empty(self, engine):
        assert await engine.get_compacted_summary() is None

    async def test_get_compacted_summary_after_set(self, engine, db):
        await ego_crud.set_state(
            db, key=CompactionEngine.STATE_KEY_SUMMARY, value="test summary",
        )
        assert await engine.get_compacted_summary() == "test summary"


# ---------------------------------------------------------------------------
# maybe_compact
# ---------------------------------------------------------------------------


class TestMaybeCompact:
    async def test_noop_below_window(self, engine):
        """Fewer than window_size cycles → nothing to compact."""
        await _seed_cycles(engine, 2)
        assert await engine.maybe_compact() is False

    async def test_noop_at_window(self, engine):
        """Exactly window_size cycles → nothing to compact."""
        await _seed_cycles(engine, 3)
        assert await engine.maybe_compact() is False

    async def test_compacts_one_above_window(self, engine, db, mock_router):
        """window_size + 1 → compacts the oldest."""
        await _seed_cycles(engine, 4)
        result = await engine.maybe_compact()
        assert result is True

        # Oldest cycle should be marked compacted.
        row = await ego_crud.get_cycle(db, "c000")
        assert row["compacted_into"] == CompactionEngine.STATE_KEY_SUMMARY

        # Summary should exist in ego_state.
        summary = await engine.get_compacted_summary()
        assert summary is not None

        # Router was called exactly once.
        assert mock_router.route_call.call_count == 1

    async def test_compacts_oldest_first(self, engine, db):
        """With multiple above window, oldest is compacted."""
        await _seed_cycles(engine, 6)
        await engine.maybe_compact()

        compacted = await ego_crud.get_cycle(db, "c000")
        assert compacted["compacted_into"] is not None

        # c001 should still be uncompacted (only one per call).
        still_open = await ego_crud.get_cycle(db, "c001")
        assert still_open["compacted_into"] is None

    async def test_incremental_one_per_call(self, engine, db):
        """Even with many above window, only one compacted per call."""
        await _seed_cycles(engine, 6)
        await engine.maybe_compact()

        uncompacted = await ego_crud.count_uncompacted(db)
        # Started with 6, compacted 1 → 5 uncompacted.
        assert uncompacted == 5

    async def test_summary_updated_in_ego_state(self, engine, db, mock_router):
        """After compaction, ego_state has the LLM's output."""
        mock_router.route_call.return_value = _MockRoutingResult(
            success=True,
            content="## Updated summary with new info",
        )
        await _seed_cycles(engine, 4)
        await engine.maybe_compact()

        summary = await engine.get_compacted_summary()
        assert summary == "## Updated summary with new info"

    async def test_already_compacted_skipped(self, engine, db):
        """Cycles with compacted_into set are ignored."""
        await _seed_cycles(engine, 5)
        # Manually compact the first two.
        await ego_crud.mark_compacted(db, cycle_id="c000", compacted_into="old")
        await ego_crud.mark_compacted(db, cycle_id="c001", compacted_into="old")

        # 3 uncompacted remain (c002, c003, c004) — at window_size=3, nothing to do.
        assert await engine.maybe_compact() is False

    async def test_graceful_degradation_on_failure(self, failing_engine, db):
        """LLM failure → returns False, no data modified."""
        ids = await _seed_cycles(failing_engine, 4)

        result = await failing_engine.maybe_compact()
        assert result is False

        # No cycle should be marked compacted.
        for cid in ids:
            row = await ego_crud.get_cycle(db, cid)
            assert row["compacted_into"] is None

        # No summary should exist.
        summary = await failing_engine.get_compacted_summary()
        assert summary is None

    async def test_db_write_failure_rolls_back(self, db, mock_router):
        """If the DB write fails mid-transaction, neither write persists."""
        engine = CompactionEngine(db=db, router=mock_router, window_size=3)
        await _seed_cycles(engine, 4)

        # Patch db.execute to fail on the UPDATE (second write in the transaction).
        _original_execute = db.execute
        _call_count = 0

        async def _failing_execute(sql, *args, **kwargs):
            nonlocal _call_count
            # The transaction does: BEGIN, INSERT ego_state, UPDATE ego_cycles.
            # Fail on the UPDATE (3rd execute call in the transaction).
            if "UPDATE ego_cycles SET compacted_into" in str(sql):
                raise RuntimeError("simulated DB failure")
            return await _original_execute(sql, *args, **kwargs)

        db.execute = _failing_execute

        result = await engine.maybe_compact()
        assert result is False

        # Restore execute for assertions.
        db.execute = _original_execute

        # Neither write should have persisted.
        summary = await engine.get_compacted_summary()
        assert summary is None, "Summary should not persist after failed transaction"

        row = await ego_crud.get_cycle(db, "c000")
        assert row["compacted_into"] is None, "Cycle should not be marked compacted"

    async def test_route_call_raises_exception(self, db):
        """Defensive: if route_call raises (shouldn't happen), returns None."""
        raising_router = AsyncMock()
        raising_router.route_call.side_effect = RuntimeError("kaboom")
        engine = CompactionEngine(db=db, router=raising_router, window_size=3)
        await _seed_cycles(engine, 4)

        result = await engine.maybe_compact()
        assert result is False

        # No cycle should be marked compacted.
        row = await ego_crud.get_cycle(db, "c000")
        assert row["compacted_into"] is None

    async def test_multiple_compactions_accumulate(self, engine, db, mock_router):
        """Two successive compactions both update the summary."""
        await _seed_cycles(engine, 5)  # 2 above window

        mock_router.route_call.return_value = _MockRoutingResult(
            success=True, content="Summary v1",
        )
        assert await engine.maybe_compact() is True
        assert await engine.get_compacted_summary() == "Summary v1"

        mock_router.route_call.return_value = _MockRoutingResult(
            success=True, content="Summary v2 (includes v1)",
        )
        assert await engine.maybe_compact() is True
        assert await engine.get_compacted_summary() == "Summary v2 (includes v1)"

        assert await ego_crud.count_uncompacted(db) == 3  # window_size


# ---------------------------------------------------------------------------
# assemble_context
# ---------------------------------------------------------------------------


class TestAssembleContext:
    async def test_empty_state(self, engine, mock_context_builder):
        """Fresh system: no summary, no cycles, just fresh context."""
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "No compacted history yet" in ctx
        assert "Capabilities" in ctx  # from mock builder
        assert "Recent Cycles" not in ctx

    async def test_with_recent_cycles_only(self, engine, mock_context_builder):
        """Within window: all cycles shown, no summary."""
        await _seed_cycles(engine, 2)
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "No compacted history yet" in ctx
        assert "Recent Cycles (last 2)" in ctx
        assert "output for c000" in ctx
        assert "output for c001" in ctx

    async def test_with_summary_and_recent(self, engine, db, mock_context_builder):
        """Summary + recent cycles + fresh context."""
        await ego_crud.set_state(
            db, key=CompactionEngine.STATE_KEY_SUMMARY, value="Prior knowledge here",
        )
        await _seed_cycles(engine, 2)

        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "Prior knowledge here" in ctx
        assert "Recent Cycles" in ctx
        assert "Capabilities" in ctx

    async def test_recent_cycles_oldest_first(self, engine, mock_context_builder):
        """Cycles appear oldest → newest for reading flow."""
        await _seed_cycles(engine, 3)
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        # c000 should appear before c002
        pos0 = ctx.index("c000")
        pos2 = ctx.index("c002")
        assert pos0 < pos2

    async def test_large_output_truncated(self, engine, mock_context_builder):
        """Very long cycle output gets truncated with marker."""
        big_output = "x" * 5000
        cycle = _make_cycle("big", output_text=big_output)
        await engine.store_cycle(cycle)

        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "[truncated — 5000 chars]" in ctx
        # Full output should NOT be present.
        assert big_output not in ctx

    async def test_context_builder_included(self, engine, mock_context_builder):
        """EgoContextBuilder output appears verbatim."""
        mock_context_builder.build.return_value = "FRESH_CONTEXT_SENTINEL"
        ctx = await engine.assemble_context(context_builder=mock_context_builder)
        assert "FRESH_CONTEXT_SENTINEL" in ctx


# ---------------------------------------------------------------------------
# Compaction prompt construction
# ---------------------------------------------------------------------------


class TestCompactionPrompt:
    def test_first_compaction_no_prior_summary(self):
        messages = _build_compaction_prompt(
            existing_summary=None,
            cycle_output="decided to investigate backlog",
            cycle_focus="observation management",
            cycle_created_at="2026-03-28T10:00:00Z",
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert _NO_PRIOR_SUMMARY in messages[1]["content"]

    def test_subsequent_compaction_includes_summary(self):
        messages = _build_compaction_prompt(
            existing_summary="Prior summary content here",
            cycle_output="new decisions",
            cycle_focus="system health",
            cycle_created_at="2026-03-28T12:00:00Z",
        )
        assert "Prior summary content here" in messages[1]["content"]
        assert _NO_PRIOR_SUMMARY not in messages[1]["content"]

    def test_cycle_metadata_in_prompt(self):
        messages = _build_compaction_prompt(
            existing_summary=None,
            cycle_output="output text",
            cycle_focus="backlog triage",
            cycle_created_at="2026-03-28T08:00:00Z",
        )
        user_msg = messages[1]["content"]
        assert "2026-03-28T08:00:00Z" in user_msg
        assert "backlog triage" in user_msg

    def test_system_prompt_has_structure_instructions(self):
        messages = _build_compaction_prompt(
            existing_summary=None,
            cycle_output="x",
            cycle_focus="y",
            cycle_created_at="z",
        )
        sys_msg = messages[0]["content"]
        assert "Active Threads" in sys_msg
        assert "Completed Resolutions" in sys_msg
        assert "PRESERVE" in sys_msg
        assert "DISCARD" in sys_msg
