"""Tests for ContextAssembler."""

from __future__ import annotations

import pytest

from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult

# tick_id → focus mapping (non-UUID tick_ids use byte encoding):
#   "tick-0" → user_impact, "tick-1" → anomaly, "tick-2" → situation


def _make_tick(*, depth=Depth.MICRO, tick_id: str = "tick-1") -> TickResult:
    """Helper to create a minimal TickResult."""
    return TickResult(
        tick_id=tick_id,
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name="cpu_usage", value=0.3, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
            SignalReading(
                name="memory_usage", value=0.6, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
        ],
        scores=[
            DepthScore(
                depth=Depth.MICRO, raw_score=0.3, time_multiplier=1.0,
                final_score=0.3, threshold=0.2, triggered=True,
            ),
        ],
        classified_depth=depth,
        trigger_reason="threshold_exceeded",
    )


@pytest.fixture
def identity_dir(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("You are Genesis.")
    user = tmp_path / "USER.md"
    user.write_text("Timezone: EST")
    return tmp_path


async def test_micro_context_has_identity_and_signals(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.MICRO)

    ctx = await assembler.assemble(Depth.MICRO, tick, db=db)

    assert ctx.identity == ""  # Micro gets no identity (cheap model overwhelmed by SOUL.md)
    assert "cpu_usage" in ctx.signals_text
    assert "memory_usage" in ctx.signals_text
    assert ctx.depth == "Micro"
    assert isinstance(ctx.tick_number, int)


async def test_micro_context_no_user_profile(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.MICRO)

    ctx = await assembler.assemble(Depth.MICRO, tick, db=db)

    assert ctx.user_profile is None
    assert ctx.cognitive_state is None


async def test_light_context_includes_user_and_cognitive_state(db, identity_dir):
    """user_impact focus includes user_profile; cognitive_state always present."""
    from genesis.db.crud import cognitive_state
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    await cognitive_state.create(
        db, id="cs-1", content="Working on Phase 4.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    # tick-0 → user_impact focus (includes user_profile)
    tick = _make_tick(depth=Depth.LIGHT, tick_id="tick-0")

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.suggested_focus == "user_impact"
    assert ctx.user_profile is not None
    assert "Timezone: EST" in ctx.user_profile
    assert ctx.cognitive_state is not None
    assert "Working on Phase 4" in ctx.cognitive_state


async def test_light_context_empty_cognitive_state_uses_bootstrap(db, identity_dir):
    from unittest.mock import patch

    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.LIGHT)

    # Mock out session patches so production data doesn't leak into the test
    with patch("genesis.db.crud.cognitive_state.load_session_patches", return_value=[]):
        ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.cognitive_state is not None
    assert "No cognitive state yet" in ctx.cognitive_state


async def test_assemble_with_user_model_evolver(db, identity_dir):
    """user_impact focus includes user_model from evolver."""
    from unittest.mock import AsyncMock

    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    evolver = AsyncMock()
    evolver.get_model_summary = AsyncMock(return_value="User prefers Python")

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader, user_model_evolver=evolver)
    # tick-0 → user_impact focus (includes user_model)
    tick = _make_tick(depth=Depth.LIGHT, tick_id="tick-0")

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.suggested_focus == "user_impact"
    assert ctx.user_model == "User prefers Python"
    evolver.get_model_summary.assert_awaited_once()


async def test_assemble_without_evolver(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.LIGHT)

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.user_model is None


async def test_signals_text_formatting(db, identity_dir):
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.MICRO)

    ctx = await assembler.assemble(Depth.MICRO, tick, db=db)

    assert "cpu_usage: 0.3" in ctx.signals_text
    assert "memory_usage: 0.6" in ctx.signals_text


async def test_light_context_sets_suggested_focus(db, identity_dir):
    """Light context should set suggested_focus via tick-based rotation."""
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.LIGHT)

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)
    assert ctx.suggested_focus in {"situation", "user_impact", "anomaly"}


async def test_light_memory_hits_capped_at_7(db, identity_dir):
    """Anomaly focus should have at most 7 observations in context."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)

    now = datetime.now(UTC).isoformat()
    for i in range(25):
        await observations.create(
            db, id=f"cap-{i}", source="test", type="metric",
            content=f"Observation {i}", priority="medium", created_at=now,
        )

    # tick-1 → anomaly focus (includes memory_hits)
    tick = _make_tick(depth=Depth.LIGHT, tick_id="tick-1")
    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.suggested_focus == "anomaly"
    assert ctx.memory_hits is not None
    lines = [line for line in ctx.memory_hits.split("\n") if line.strip()]
    assert len(lines) <= 7, f"Expected ≤7 obs for light, got {len(lines)}"


async def test_light_focus_aware_context_stripping(db, identity_dir):
    """Each focus area gets only the context it needs."""
    from datetime import UTC, datetime

    from genesis.db.crud import cognitive_state, observations
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    await cognitive_state.create(
        db, id="cs-fa", content="Active context.",
        section="active_context", generated_by="test",
        created_at=datetime.now(UTC).isoformat(),
    )
    await observations.create(
        db, id="fa-obs", source="test", type="metric",
        content="Test observation", priority="medium",
        created_at=datetime.now(UTC).isoformat(),
    )

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)

    # situation (tick-2): no user_profile, no memory_hits
    ctx_sit = await assembler.assemble(
        Depth.LIGHT, _make_tick(depth=Depth.LIGHT, tick_id="tick-2"), db=db,
    )
    assert ctx_sit.suggested_focus == "situation"
    assert ctx_sit.user_profile is None
    assert ctx_sit.user_model is None
    assert ctx_sit.memory_hits is None
    assert ctx_sit.cognitive_state is not None

    # user_impact (tick-0): has user_profile, no memory_hits
    ctx_ui = await assembler.assemble(
        Depth.LIGHT, _make_tick(depth=Depth.LIGHT, tick_id="tick-0"), db=db,
    )
    assert ctx_ui.suggested_focus == "user_impact"
    assert ctx_ui.user_profile is not None
    assert ctx_ui.memory_hits is None

    # anomaly (tick-1): has memory_hits, no user_profile
    ctx_anom = await assembler.assemble(
        Depth.LIGHT, _make_tick(depth=Depth.LIGHT, tick_id="tick-1"), db=db,
    )
    assert ctx_anom.suggested_focus == "anomaly"
    assert ctx_anom.user_profile is None
    assert ctx_anom.user_model is None
    assert ctx_anom.memory_hits is not None
