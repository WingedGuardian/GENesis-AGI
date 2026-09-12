"""WS-M PR-1/PR-2: ephemeral-ideation routing split + purge_discarded GC.

PR-1 kept ideation output (self_unblock / brainstorm / audits) OUT of the
immortal knowledge_units KB. PR-2 splits it by semantics:
  • IDEAS (brainstorm) → surplus_insights staging (ideas-review lifecycle).
  • SELF-OBSERVATIONS (audits / gap-cluster / unblock / prompt-review) → the
    observation lane (TTL + resolve + dashboard surfacing), priority="low".
anticipatory_research / code_audit stay KB-bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import genesis.db.crud.observations as obs_crud
import genesis.db.crud.surplus as surplus_crud
from genesis.surplus.intake import IntakeSource, run_intake
from genesis.surplus.types import (
    EPHEMERAL_IDEATION_TASK_TYPES,
    IDEA_TASK_TYPES,
    SELF_OBSERVATION_TASK_TYPES,
    TaskType,
    is_ephemeral_ideation,
    is_idea_ideation,
    is_self_observation_ideation,
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


class TestIdeationSplit:
    """PR-2: the ephemeral set splits into two DISJOINT subsets whose union is
    the whole set (so the Step-3a intercept guard still catches all 9)."""

    def test_subsets_are_disjoint(self):
        assert frozenset() == IDEA_TASK_TYPES & SELF_OBSERVATION_TASK_TYPES

    def test_union_equals_ephemeral(self):
        assert IDEA_TASK_TYPES | SELF_OBSERVATION_TASK_TYPES == (EPHEMERAL_IDEATION_TASK_TYPES)

    def test_idea_classification(self):
        for tt in (
            TaskType.BRAINSTORM_SELF,
            TaskType.BRAINSTORM_USER,
            TaskType.META_BRAINSTORM,
        ):
            assert is_idea_ideation(str(tt))
            assert not is_self_observation_ideation(str(tt))

    def test_self_observation_classification(self):
        for tt in (
            TaskType.SELF_UNBLOCK,
            TaskType.GAP_CLUSTERING,
            TaskType.WING_AUDIT,
            TaskType.MEMORY_AUDIT,
            TaskType.PROCEDURE_AUDIT,
            TaskType.PROMPT_EFFECTIVENESS_REVIEW,
        ):
            assert is_self_observation_ideation(str(tt))
            assert not is_idea_ideation(str(tt))

    def test_unknown_string_neither(self):
        assert not is_idea_ideation("nope")
        assert not is_self_observation_ideation("")


@pytest.mark.asyncio
async def test_brainstorm_routes_to_staging_not_kb(db):
    """An idea (brainstorm) is staged; the KB ingest is not called."""
    with patch(
        "genesis.memory.knowledge_ingest.ingest_knowledge_unit",
        new=AsyncMock(),
    ) as ingest:
        stats = await run_intake(
            content="Dynamic Awareness Depth Tuning\n\nA feedback loop for tick depth.",
            source=IntakeSource.ANTICIPATORY_RESEARCH,  # collapsed source (real flow)
            source_task_type="brainstorm_self",  # TRUE task type — drives the gate
            generating_model="test-model",
            drive_alignment="competence",
            db=db,
        )
    assert stats.routed_staging >= 1
    assert stats.routed_observation == 0
    assert stats.routed_knowledge == 0
    ingest.assert_not_awaited()
    rows = await surplus_crud.list_pending(db, limit=50)
    staged = [r for r in rows if "Awareness Depth" in r["content"]]
    assert staged, "brainstorm finding should be staged in surplus_insights"
    assert all(r["promotion_status"] == "pending" for r in staged)


@pytest.mark.asyncio
async def test_self_observation_routes_to_observation_not_kb(db):
    """A self-observation (self_unblock) is written to the observation lane —
    NOT staged, NOT KB-ingested (PR-2 split)."""
    with patch(
        "genesis.memory.knowledge_ingest.ingest_knowledge_unit",
        new=AsyncMock(),
    ) as ingest:
        stats = await run_intake(
            content="Self Unblock\n\nThe system is stuck in a priority collision.",
            source=IntakeSource.ANTICIPATORY_RESEARCH,
            source_task_type="self_unblock",  # a self-observation type
            generating_model="test-model",
            db=db,
        )
    assert stats.routed_observation >= 1
    assert stats.routed_staging == 0
    assert stats.routed_knowledge == 0
    ingest.assert_not_awaited()
    # Not staged...
    rows = await surplus_crud.list_pending(db, limit=50)
    assert not any("priority collision" in r["content"] for r in rows)
    # ...written as an observation at priority='low', surfacing type.
    obs = await obs_crud.query(db, type="self_unblock", limit=50)
    assert obs, "self_unblock should be written to the observation lane"
    assert all(o["priority"] == "low" for o in obs)


@pytest.mark.asyncio
async def test_self_observation_dedup_idempotent(db):
    """Re-emitting an identical self-observation does not create a duplicate row."""
    kwargs = dict(
        content="Rapid Genesis Version Volatility\n\nVersion churned 3x in an hour.",
        source=IntakeSource.ANTICIPATORY_RESEARCH,
        source_task_type="gap_clustering",
        generating_model="test-model",
        db=db,
    )
    await run_intake(**kwargs)
    await run_intake(**kwargs)
    obs = await obs_crud.query(db, type="gap_clustering", limit=50)
    assert len(obs) == 1, "identical self-observation should dedup on content_hash"


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
