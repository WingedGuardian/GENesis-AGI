"""WS-M PR-1: ephemeral-ideation staging gate + purge_discarded GC.

Ideation task output (self_unblock / brainstorm / audits) must be STAGED in
surplus_insights (ideas-review lifecycle, TTL decay) instead of immortalized as
knowledge_units. anticipatory_research / code_audit stay KB-bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import genesis.db.crud.surplus as surplus_crud
from genesis.surplus.intake import IntakeSource, run_intake
from genesis.surplus.types import (
    EPHEMERAL_IDEATION_TASK_TYPES,
    TaskType,
    is_ephemeral_ideation,
)


class TestIdeationSet:
    def test_core_ideation_in_set(self):
        for tt in (
            TaskType.SELF_UNBLOCK,
            TaskType.BRAINSTORM_SELF,
            TaskType.BRAINSTORM_USER,
            TaskType.META_BRAINSTORM,
            TaskType.MEMORY_AUDIT,
            TaskType.PROCEDURE_AUDIT,
            TaskType.GAP_CLUSTERING,
            TaskType.WING_AUDIT,
            TaskType.PROMPT_EFFECTIVENESS_REVIEW,
        ):
            assert tt in EPHEMERAL_IDEATION_TASK_TYPES
            assert is_ephemeral_ideation(str(tt))

    def test_kb_bound_types_excluded(self):
        assert TaskType.ANTICIPATORY_RESEARCH not in EPHEMERAL_IDEATION_TASK_TYPES
        assert TaskType.CODE_AUDIT not in EPHEMERAL_IDEATION_TASK_TYPES
        assert not is_ephemeral_ideation("anticipatory_research")
        assert not is_ephemeral_ideation("code_audit")

    def test_unknown_string_is_not_ideation(self):
        assert not is_ephemeral_ideation("")
        assert not is_ephemeral_ideation("not_a_real_task_type")


@pytest.mark.asyncio
async def test_self_unblock_routes_to_staging_not_kb(db):
    """A self_unblock finding is staged; the knowledge-base ingest is not called."""
    with patch(
        "genesis.memory.knowledge_ingest.ingest_knowledge_unit",
        new=AsyncMock(),
    ) as ingest:
        stats = await run_intake(
            content="Self Unblock\n\nThe system is stuck in a priority collision.",
            source=IntakeSource.ANTICIPATORY_RESEARCH,  # collapsed source (real flow)
            source_task_type="self_unblock",  # TRUE task type — drives the gate
            generating_model="test-model",
            drive_alignment="competence",
            db=db,
        )
    assert stats.routed_staging >= 1
    assert stats.routed_knowledge == 0
    ingest.assert_not_awaited()
    rows = await surplus_crud.list_pending(db, limit=50)
    staged = [r for r in rows if "priority collision" in r["content"]]
    assert staged, "self_unblock finding should be staged in surplus_insights"
    assert all(r["promotion_status"] == "pending" for r in staged)


@pytest.mark.asyncio
async def test_anticipatory_research_still_routes_to_kb(db):
    """Genuine research is NOT staged — it goes to the knowledge base."""
    with patch(
        "genesis.memory.knowledge_ingest.ingest_knowledge_unit",
        new=AsyncMock(),
    ) as ingest:
        stats = await run_intake(
            content="Top AI Agent Runtime Security Tools 2026\n\nDetailed comparison.",
            source=IntakeSource.ANTICIPATORY_RESEARCH,
            source_task_type="anticipatory_research",
            generating_model="test-model",
            db=db,
            store=AsyncMock(),
        )
    assert stats.routed_staging == 0
    assert stats.routed_knowledge >= 1
    ingest.assert_awaited()
    rows = await surplus_crud.list_pending(db, limit=50)
    assert not any("Top AI Agent" in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_invalid_drive_alignment_defaults_to_competence(db):
    """An empty/invalid drive_alignment coerces to 'competence' (CHECK-safe)."""
    stats = await run_intake(
        content="Brainstorm\n\nSome idea about goal triage.",
        source=IntakeSource.ANTICIPATORY_RESEARCH,
        source_task_type="brainstorm_self",
        generating_model="test-model",
        drive_alignment="",  # invalid → must coerce, not raise on the CHECK
        db=db,
    )
    assert stats.routed_staging >= 1
    rows = await surplus_crud.list_pending(db, limit=50)
    staged = [r for r in rows if "goal triage" in r["content"]]
    assert staged and all(r["drive_alignment"] == "competence" for r in staged)


@pytest.mark.asyncio
async def test_purge_discarded_deletes_only_old_discarded(db):
    now = datetime.now(UTC)
    old = (now - timedelta(days=40)).isoformat()
    recent = (now - timedelta(days=5)).isoformat()
    future_ttl = (now + timedelta(days=7)).isoformat()
    await surplus_crud.create(
        db,
        id="old-disc",
        content="x",
        source_task_type="self_unblock",
        generating_model="m",
        drive_alignment="competence",
        created_at=old,
        ttl=old,
        confidence=0.6,
    )
    await surplus_crud.create(
        db,
        id="new-disc",
        content="y",
        source_task_type="self_unblock",
        generating_model="m",
        drive_alignment="competence",
        created_at=recent,
        ttl=recent,
        confidence=0.6,
    )
    await surplus_crud.create(
        db,
        id="old-pending",
        content="z",
        source_task_type="self_unblock",
        generating_model="m",
        drive_alignment="competence",
        created_at=old,
        ttl=future_ttl,
        confidence=0.6,
    )
    await surplus_crud.discard(db, "old-disc")
    await surplus_crud.discard(db, "new-disc")

    deleted = await surplus_crud.purge_discarded(db, older_than_days=30)

    assert deleted == 1
    assert await surplus_crud.get_by_id(db, "old-disc") is None  # old + discarded
    assert await surplus_crud.get_by_id(db, "new-disc") is not None  # recent
    assert await surplus_crud.get_by_id(db, "old-pending") is not None  # not discarded
