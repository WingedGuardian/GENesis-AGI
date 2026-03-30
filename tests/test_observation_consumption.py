"""Tests for observation consumption wiring — retrieved_count tracking,
memory_hits population, prior_context threading, and mark_influenced.
"""

from __future__ import annotations

import pytest

from genesis.db.crud import observations

_COMMON = dict(
    source="sensor",
    type="metric",
    content="cpu at 90%",
    priority="high",
    created_at="2026-01-01T00:00:00",
)


# ── Batch 1: increment_retrieved_batch ───────────────────────────────────────


async def test_increment_retrieved_batch_updates_counts(db):
    await observations.create(db, id="b1-1", **_COMMON)
    await observations.create(db, id="b1-2", **_COMMON)
    await observations.create(db, id="b1-3", **_COMMON)

    count = await observations.increment_retrieved_batch(db, ["b1-1", "b1-2"])
    assert count == 2

    r1 = await observations.get_by_id(db, "b1-1")
    r2 = await observations.get_by_id(db, "b1-2")
    r3 = await observations.get_by_id(db, "b1-3")
    assert r1["retrieved_count"] == 1
    assert r2["retrieved_count"] == 1
    assert r3["retrieved_count"] == 0  # not in batch


async def test_increment_retrieved_batch_empty_list(db):
    count = await observations.increment_retrieved_batch(db, [])
    assert count == 0


async def test_increment_retrieved_batch_nonexistent_ids(db):
    count = await observations.increment_retrieved_batch(db, ["nope1", "nope2"])
    assert count == 0


async def test_increment_retrieved_batch_increments_cumulatively(db):
    await observations.create(db, id="b1-cum", **_COMMON)
    await observations.increment_retrieved_batch(db, ["b1-cum"])
    await observations.increment_retrieved_batch(db, ["b1-cum"])
    row = await observations.get_by_id(db, "b1-cum")
    assert row["retrieved_count"] == 2


# ── Batch 2: ContextAssembler populates memory_hits ─────────────────────────


@pytest.fixture
def identity_dir(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("You are Genesis.")
    user = tmp_path / "USER.md"
    user.write_text("Timezone: EST")
    return tmp_path


def _make_tick(*, depth, tick_id: str = "tick-obs-1"):
    from genesis.awareness.types import DepthScore, SignalReading, TickResult
    return TickResult(
        tick_id=tick_id,
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name="cpu_usage", value=0.3, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
        ],
        scores=[
            DepthScore(
                depth=depth, raw_score=0.3, time_multiplier=1.0,
                final_score=0.3, threshold=0.2, triggered=True,
            ),
        ],
        classified_depth=depth,
        trigger_reason="threshold_exceeded",
    )


async def test_light_context_includes_memory_hits(db, identity_dir):
    from datetime import UTC, datetime

    from genesis.awareness.types import Depth
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    now = datetime.now(UTC).isoformat()
    # Create some observations — must be recent to pass age guard
    await observations.create(
        db, id="mh-1", source="sensor", type="metric",
        content="CPU spike detected", priority="high",
        created_at=now,
    )
    await observations.create(
        db, id="mh-2", source="sensor", type="metric",
        content="Memory leak found", priority="medium",
        created_at=now,
    )

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    # tick-1 maps to anomaly focus (includes memory_hits)
    tick = _make_tick(depth=Depth.LIGHT, tick_id="tick-1")

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    assert ctx.suggested_focus == "anomaly"
    assert ctx.memory_hits is not None
    assert "CPU spike detected" in ctx.memory_hits
    assert "Memory leak found" in ctx.memory_hits


async def test_micro_context_no_memory_hits(db, identity_dir):
    from genesis.awareness.types import Depth
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    await observations.create(db, id="mh-micro", **_COMMON)

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.MICRO)

    ctx = await assembler.assemble(Depth.MICRO, tick, db=db)
    assert ctx.memory_hits is None


async def test_memory_hits_tracks_retrieval(db, identity_dir):
    from datetime import UTC, datetime

    from genesis.awareness.types import Depth
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    now = datetime.now(UTC).isoformat()
    await observations.create(db, id="mh-track", source="sensor", type="metric",
                              content="cpu at 90%", priority="high", created_at=now)

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    # tick-1 maps to anomaly focus (includes memory_hits, so retrieval tracked)
    tick = _make_tick(depth=Depth.LIGHT, tick_id="tick-1")

    await assembler.assemble(Depth.LIGHT, tick, db=db)

    row = await observations.get_by_id(db, "mh-track")
    assert row["retrieved_count"] >= 1


async def test_light_situation_focus_no_memory_hits(db, identity_dir):
    """Situation focus strips memory_hits even when observations exist."""
    from datetime import UTC, datetime

    from genesis.awareness.types import Depth
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    now = datetime.now(UTC).isoformat()
    await observations.create(db, id="sit-obs", source="sensor", type="metric",
                              content="cpu spike", priority="high", created_at=now)

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    # tick-2 maps to situation focus (no memory_hits)
    tick = _make_tick(depth=Depth.LIGHT, tick_id="tick-2")

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)
    assert ctx.suggested_focus == "situation"
    assert ctx.memory_hits is None


# ── Batch 3: prior_context threading ────────────────────────────────────────


async def test_prior_context_threaded_to_prompt_context(db, identity_dir):
    from genesis.awareness.types import Depth
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.LIGHT)

    ctx = await assembler.assemble(
        Depth.LIGHT, tick, db=db,
        prior_context="Previously found: stale cognitive state",
    )

    assert ctx.prior_context == "Previously found: stale cognitive state"


async def test_prior_context_defaults_to_none(db, identity_dir):
    from genesis.awareness.types import Depth
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    tick = _make_tick(depth=Depth.LIGHT)

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)
    assert ctx.prior_context is None


def test_prompt_context_has_prior_context_field():
    from genesis.perception.types import PromptContext
    ctx = PromptContext(
        depth="Light",
        identity="Genesis",
        signals_text="cpu: 0.3",
        tick_number=1,
        prior_context="some prior findings",
    )
    assert ctx.prior_context == "some prior findings"


def test_light_prompt_renders_memory_hits():
    from genesis.perception.prompts import PromptBuilder
    from genesis.perception.types import PromptContext

    builder = PromptBuilder()
    ctx = PromptContext(
        depth="Light",
        identity="You are Genesis.",
        signals_text="cpu: 0.3",
        tick_number=0,
        user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
        memory_hits="- [high] metric: CPU spike",
        prior_context="Previously found: stale state",
    )
    prompt = builder.build("Light", ctx)
    assert "CPU spike" in prompt
    assert "## Recent Observations" in prompt


def test_light_prompt_renders_defaults_when_no_obs():
    from genesis.perception.prompts import PromptBuilder
    from genesis.perception.types import PromptContext

    builder = PromptBuilder()
    ctx = PromptContext(
        depth="Light",
        identity="You are Genesis.",
        signals_text="cpu: 0.3",
        tick_number=0,
        user_profile="Timezone: EST",
        cognitive_state="Working.",
    )
    prompt = builder.build("Light", ctx)
    assert "(no recent observations)" in prompt


# ── Batch 1: memory_core_facts uses actual retrieved_count ──────────────────


async def test_memory_core_facts_retrieved_count_from_db(db):
    """Verify the hardcoded retrieved_count=0 bug is fixed."""
    # Create an observation with type="learning" and increment its count
    await observations.create(
        db, id="mcf-1", source="test", type="learning",
        content="learned something", priority="high",
        created_at="2026-01-01T00:00:00",
    )
    await observations.increment_retrieved(db, "mcf-1")
    await observations.increment_retrieved(db, "mcf-1")

    row = await observations.get_by_id(db, "mcf-1")
    assert row["retrieved_count"] == 2

    # The fix: observations.query returns full rows including retrieved_count
    results = await observations.query(db, type="learning", resolved=False, limit=10)
    assert len(results) >= 1
    obs = next(r for r in results if r["id"] == "mcf-1")
    assert obs["retrieved_count"] == 2


# ── Batch 4: mark_influenced ────────────────────────────────────────────────


async def test_mark_influenced_sets_flag(db):
    await observations.create(db, id="inf-1", **_COMMON)

    result = await observations.mark_influenced(db, "inf-1")
    assert result is True

    row = await observations.get_by_id(db, "inf-1")
    assert row["influenced_action"] == 1


async def test_mark_influenced_idempotent(db):
    await observations.create(db, id="inf-2", **_COMMON)

    await observations.mark_influenced(db, "inf-2")
    await observations.mark_influenced(db, "inf-2")

    row = await observations.get_by_id(db, "inf-2")
    assert row["influenced_action"] == 1


async def test_mark_influenced_nonexistent(db):
    result = await observations.mark_influenced(db, "nope")
    assert result is False


async def test_mark_influenced_batch_updates_multiple(db):
    await observations.create(db, id="infb-1", **_COMMON)
    await observations.create(db, id="infb-2", **_COMMON)
    await observations.create(db, id="infb-3", **_COMMON)

    count = await observations.mark_influenced_batch(db, ["infb-1", "infb-2"])
    assert count == 2

    r1 = await observations.get_by_id(db, "infb-1")
    r2 = await observations.get_by_id(db, "infb-2")
    r3 = await observations.get_by_id(db, "infb-3")
    assert r1["influenced_action"] == 1
    assert r2["influenced_action"] == 1
    assert r3["influenced_action"] == 0


async def test_mark_influenced_batch_empty_list(db):
    count = await observations.mark_influenced_batch(db, [])
    assert count == 0


# ── source_in parameter ─────────────────────────────────────────────────────


async def test_source_in_filters_by_multiple_sources(db):
    await observations.create(db, id="si-1", source="reflection", type="x", content="a", priority="low", created_at="2026-01-01T00:00:00")
    await observations.create(db, id="si-2", source="deep_reflection", type="x", content="b", priority="low", created_at="2026-01-01T00:00:01")
    await observations.create(db, id="si-3", source="awareness", type="x", content="c", priority="low", created_at="2026-01-01T00:00:02")

    results = await observations.query(db, source_in=["reflection", "deep_reflection"])
    ids = {r["id"] for r in results}
    assert "si-1" in ids
    assert "si-2" in ids
    assert "si-3" not in ids


async def test_source_and_source_in_mutual_exclusion(db):
    with pytest.raises(ValueError, match="Cannot specify both"):
        await observations.query(db, source="x", source_in=["y"])


# ── resolve_batch ────────────────────────────────────────────────────────────


async def test_resolve_batch_resolves_multiple(db):
    await observations.create(db, id="rb-1", **_COMMON)
    await observations.create(db, id="rb-2", **_COMMON)
    await observations.create(db, id="rb-3", **_COMMON)

    count = await observations.resolve_batch(
        db, ["rb-1", "rb-2"],
        resolved_at="2026-01-01T01:00:00",
        resolution_notes="deduplicated",
    )
    assert count == 2

    r1 = await observations.get_by_id(db, "rb-1")
    assert r1["resolved"] == 1
    assert r1["resolution_notes"] == "deduplicated"
    r3 = await observations.get_by_id(db, "rb-3")
    assert r3["resolved"] == 0


async def test_resolve_batch_empty_list(db):
    count = await observations.resolve_batch(
        db, [], resolved_at="2026-01-01T01:00:00", resolution_notes="n/a",
    )
    assert count == 0


async def test_resolve_batch_skips_already_resolved(db):
    await observations.create(db, id="rb-dup", **_COMMON)
    await observations.resolve(
        db, "rb-dup", resolved_at="2026-01-01T01:00:00", resolution_notes="first",
    )
    count = await observations.resolve_batch(
        db, ["rb-dup"],
        resolved_at="2026-01-01T02:00:00",
        resolution_notes="second",
    )
    assert count == 0  # Already resolved — no update


# ── Diversity query in _build_memory_hits ────────────────────────────────────


async def test_memory_hits_diversity_includes_reflection_sources(db, identity_dir):
    """Even when 50+ awareness observations exist, reflection-source obs appear."""
    from datetime import UTC, datetime

    from genesis.awareness.types import Depth
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    now = datetime.now(UTC).isoformat()
    # Create 25 awareness observations (would saturate old limit=20 query)
    for i in range(25):
        await observations.create(
            db, id=f"aw-{i}", source="awareness", type="signal",
            content=f"awareness signal {i}", priority="low",
            created_at=now,
        )
    # Create 3 reflection observations
    for i in range(3):
        await observations.create(
            db, id=f"ref-{i}", source="deep_reflection", type="learning",
            content=f"reflection learning {i}", priority="medium",
            created_at=now,
        )

    loader = IdentityLoader(identity_dir)
    assembler = ContextAssembler(identity_loader=loader)
    # tick-1 maps to anomaly focus (includes memory_hits)
    tick = _make_tick(depth=Depth.LIGHT, tick_id="tick-1")

    ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)
    assert ctx.suggested_focus == "anomaly"
    assert ctx.memory_hits is not None
    # All 3 reflection observations must be present
    assert "reflection learning 0" in ctx.memory_hits
    assert "reflection learning 1" in ctx.memory_hits
    assert "reflection learning 2" in ctx.memory_hits
