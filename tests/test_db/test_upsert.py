"""Tests for idempotent upsert semantics across all CRUD modules.

Each test calls upsert twice with the same ID — the second call must succeed
(not raise IntegrityError) and update the row rather than duplicating it.
"""

from __future__ import annotations

import pytest

from genesis.db.crud import (
    autonomy,
    brainstorm,
    budgets,
    capability_gaps,
    cost_events,
    execution_traces,
    memory,
    observations,
    outreach,
    procedural,
    speculative,
    surplus,
    tool_registry,
)


@pytest.mark.asyncio
async def test_observations_upsert(db):
    """Upsert twice with same ID — second call updates, no duplicate."""
    await observations.upsert(
        db, id="obs-1", source="test", type="fact", content="original",
        priority="low", created_at="2026-01-01T00:00:00Z",
    )
    await observations.upsert(
        db, id="obs-1", source="test", type="fact", content="updated",
        priority="high", created_at="2026-01-01T00:00:00Z",
    )
    row = await observations.get_by_id(db, "obs-1")
    assert row is not None
    assert row["content"] == "updated"
    assert row["priority"] == "high"
    # Only one row
    rows = await observations.query(db, source="test")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_outreach_upsert(db):
    await outreach.upsert(
        db, id="out-1", signal_type="alert", topic="test", category="blocker",
        salience_score=0.8, channel="whatsapp", message_content="original",
        created_at="2026-01-01T00:00:00Z",
    )
    await outreach.upsert(
        db, id="out-1", signal_type="alert", topic="test", category="blocker",
        salience_score=0.9, channel="whatsapp", message_content="updated",
        created_at="2026-01-01T00:00:00Z",
    )
    row = await outreach.get_by_id(db, "out-1")
    assert row["message_content"] == "updated"
    assert row["salience_score"] == 0.9


@pytest.mark.asyncio
async def test_execution_traces_upsert(db):
    await execution_traces.upsert(
        db, id="tr-1", user_request="do stuff", plan=["step1"],
        sub_agents=[], created_at="2026-01-01T00:00:00Z",
    )
    await execution_traces.upsert(
        db, id="tr-1", user_request="do stuff v2", plan=["step1", "step2"],
        sub_agents=[], created_at="2026-01-01T00:00:00Z",
    )
    row = await execution_traces.get_by_id(db, "tr-1")
    assert row["user_request"] == "do stuff v2"


@pytest.mark.asyncio
async def test_surplus_upsert(db):
    await surplus.upsert(
        db, id="sur-1", content="original", source_task_type="research",
        generating_model="test", drive_alignment="curiosity",
        created_at="2026-01-01T00:00:00Z", ttl="2026-12-31T00:00:00Z",
    )
    await surplus.upsert(
        db, id="sur-1", content="updated", source_task_type="research",
        generating_model="test", drive_alignment="curiosity",
        created_at="2026-01-01T00:00:00Z", ttl="2026-12-31T00:00:00Z",
        confidence=0.9,
    )
    row = await surplus.get_by_id(db, "sur-1")
    assert row["content"] == "updated"
    assert row["confidence"] == 0.9


@pytest.mark.asyncio
async def test_procedural_upsert(db):
    await procedural.upsert(
        db, id="proc-1", task_type="deploy", principle="be safe",
        steps=["step1"], tools_used=["ssh"], context_tags=["prod"],
        created_at="2026-01-01T00:00:00Z",
    )
    await procedural.upsert(
        db, id="proc-1", task_type="deploy", principle="be very safe",
        steps=["step1", "step2"], tools_used=["ssh"], context_tags=["prod"],
        created_at="2026-01-01T00:00:00Z",
    )
    row = await procedural.get_by_id(db, "proc-1")
    assert row["principle"] == "be very safe"


@pytest.mark.asyncio
async def test_brainstorm_upsert(db):
    await brainstorm.upsert(
        db, id="bs-1", session_type="upgrade_self", model_used="test",
        outputs=["idea1"], created_at="2026-01-01T00:00:00Z",
    )
    await brainstorm.upsert(
        db, id="bs-1", session_type="upgrade_self", model_used="test",
        outputs=["idea1", "idea2"], created_at="2026-01-01T00:00:00Z",
    )
    row = await brainstorm.get_by_id(db, "bs-1")
    assert "idea2" in row["outputs"]


@pytest.mark.asyncio
async def test_speculative_upsert(db):
    await speculative.upsert(
        db, id="spec-1", claim="original claim",
        hypothesis_expiry="2026-12-31T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
    )
    await speculative.upsert(
        db, id="spec-1", claim="updated claim",
        hypothesis_expiry="2027-06-30T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
    )
    row = await speculative.get_by_id(db, "spec-1")
    assert row["claim"] == "updated claim"
    assert "2027" in row["hypothesis_expiry"]


@pytest.mark.asyncio
async def test_autonomy_upsert(db):
    await autonomy.upsert(
        db, id="aut-1", category="outreach",
        updated_at="2026-01-01T00:00:00Z", current_level=1,
    )
    await autonomy.upsert(
        db, id="aut-1", category="outreach",
        updated_at="2026-01-02T00:00:00Z", current_level=2,
    )
    row = await autonomy.get_by_id(db, "aut-1")
    assert row["current_level"] == 2
    assert "01-02" in row["updated_at"]


@pytest.mark.asyncio
async def test_capability_gaps_upsert(db):
    """Upsert on conflict increments frequency and updates last_seen."""
    await capability_gaps.upsert(
        db, id="gap-1", description="missing tool", gap_type="capability_gap",
        first_seen="2026-01-01T00:00:00Z", last_seen="2026-01-01T00:00:00Z",
    )
    await capability_gaps.upsert(
        db, id="gap-1", description="missing tool v2", gap_type="capability_gap",
        first_seen="2026-01-01T00:00:00Z", last_seen="2026-01-15T00:00:00Z",
    )
    row = await capability_gaps.get_by_id(db, "gap-1")
    assert row["description"] == "missing tool v2"
    assert row["frequency"] == 2  # incremented on conflict
    assert "01-15" in row["last_seen"]


@pytest.mark.asyncio
async def test_tool_registry_upsert(db):
    await tool_registry.upsert(
        db, id="tool-1", name="ssh", category="system", description="original",
        tool_type="builtin", created_at="2026-01-01T00:00:00Z",
    )
    await tool_registry.upsert(
        db, id="tool-1", name="ssh", category="system", description="updated",
        tool_type="builtin", created_at="2026-01-01T00:00:00Z",
    )
    row = await tool_registry.get_by_id(db, "tool-1")
    assert row["description"] == "updated"


@pytest.mark.asyncio
async def test_memory_fts_upsert(db):
    """FTS5 upsert uses delete-then-insert pattern."""
    await memory.upsert(
        db, memory_id="mem-1", content="original fact",
    )
    await memory.upsert(
        db, memory_id="mem-1", content="updated fact",
    )
    results = await memory.search(db, query="fact")
    assert len(results) == 1
    assert results[0]["content"] == "updated fact"


@pytest.mark.asyncio
async def test_cost_events_upsert(db):
    await cost_events.upsert(
        db, id="ce-1", event_type="llm_call", cost_usd=0.01,
        model="gpt-4", created_at="2026-01-01T00:00:00Z",
    )
    await cost_events.upsert(
        db, id="ce-1", event_type="llm_call", cost_usd=0.02,
        model="gpt-4", created_at="2026-01-01T00:00:00Z",
    )
    row = await cost_events.get_by_id(db, "ce-1")
    assert row["cost_usd"] == 0.02
    rows = await cost_events.query(db, event_type="llm_call")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_budgets_upsert(db):
    await budgets.upsert(
        db, id="bud-1", budget_type="daily", limit_usd=5.0,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
    )
    await budgets.upsert(
        db, id="bud-1", budget_type="daily", limit_usd=10.0,
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-02T00:00:00Z",
    )
    row = await budgets.get_by_id(db, "bud-1")
    assert row["limit_usd"] == 10.0
    assert "01-02" in row["updated_at"]
