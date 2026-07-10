"""E4 entity lane: shadow isolation, live floor, resolution filtering.

Real in-memory entity substrate; other lanes mocked. The invariant
under test: SHADOW mode is behavior-identical to pre-E4 (entity-only
candidates never returned, existing candidates' ``lanes`` untouched —
the arbiter prompt renders lanes), while the shadow report captures
what the lane would have contributed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import entities as entities_crud
from genesis.db.schema._tables import TABLES
from genesis.session_awareness.ranking import rank_candidates

DIM = 8
EMA = [1.0] + [0.0] * (DIM - 1)
TARGET = "9d36f039-0000-0000-0000-000000000000"


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    for table in ("entities", "entity_mentions", "entity_links",
                  "deferred_work_queue"):
        await conn.execute(TABLES[table])
    await conn.commit()
    yield conn
    await conn.close()


async def _seed_spine(db):
    """omi →is_a→ voice-edge-device →constrained_by→ rule → TARGET."""
    omi = await entities_crud.create_entity(
        db, name="OMI", norm_name="omi", entity_type="device",
    )
    cat = await entities_crud.create_entity(
        db, name="voice-edge-device", norm_name="voice-edge-device",
        entity_type="concept",
    )
    rule = await entities_crud.create_entity(
        db, name="repo-split", norm_name="repo-split", entity_type="concept",
    )
    await entities_crud.upsert_link(
        db, source_id=omi, target_id=cat, link_type="is_a",
        provenance="EXTRACTED", confidence=0.95,
    )
    await entities_crud.upsert_link(
        db, source_id=cat, target_id=rule, link_type="constrained_by",
        provenance="EXTRACTED", confidence=0.95,
    )
    await entities_crud.upsert_mention(
        db, memory_id=TARGET, entity_id=rule, provenance="EXTRACTED",
        confidence=0.95, source="seed",
    )
    return omi


def _hit(mid: str, score: float):
    return {
        "id": mid,
        "score": score,
        "payload": {"confidence": 0.8, "memory_class": "fact", "content": "c"},
    }


def _point(mid: str, *, cos=0.3):
    p = MagicMock()
    p.id = mid
    p.vector = [cos] + [0.0] * (DIM - 1)
    p.payload = {
        "confidence": 0.9,
        "memory_class": "decision",
        "content": "the repo-split decision",
        "created_at": "2026-06-14T00:00:00+00:00",
    }
    return p


def _lane_patches(qdrant_hits, retrieve_points):
    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=retrieve_points)
    return qdrant, (
        patch("genesis.qdrant.collections.search", side_effect=[qdrant_hits, []]),
        patch("genesis.memory.drift.drift_recall", new=AsyncMock(return_value=[])),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    )


@pytest.mark.asyncio
async def test_shadow_excludes_entity_only_and_reports(db):
    await _seed_spine(db)
    qdrant, patches = _lane_patches([_hit("other-mem", 0.9)], [_point(TARGET)])
    shadow: list[dict] = []
    with patches[0], patches[1], patches[2], patches[3]:
        picked = await rank_candidates(
            ema=EMA, entity_query="omi noiseword", db=db,
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            entity_lane="shadow", entity_shadow_out=shadow,
        )
    ids = [c["memory_id"] for c in picked]
    assert TARGET not in ids  # shadow: never in the returned set
    assert all("entity" not in c["lanes"] for c in picked)  # lanes untouched
    assert shadow and shadow[0]["memory_id"] == TARGET
    assert shadow[0]["already_candidate"] is False
    assert shadow[0]["entity_path"]["path_score"] > 0


@pytest.mark.asyncio
async def test_live_includes_target_via_entity_lane_with_floor(db):
    await _seed_spine(db)
    # 10 strong operational candidates try to crowd the target out
    wall = [_hit(f"op-{i}", 0.95) for i in range(10)]
    qdrant, patches = _lane_patches(wall, [_point(TARGET, cos=0.2)])
    with patches[0], patches[1], patches[2], patches[3]:
        picked = await rank_candidates(
            ema=EMA, entity_query="omi", db=db,
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            entity_lane="live",
        )
    target = next((c for c in picked if c["memory_id"] == TARGET), None)
    assert target is not None, "entity floor must protect the target"
    assert "entity" in target["lanes"]
    assert target["entity_path"]["path_score"] > 0


@pytest.mark.asyncio
async def test_off_mode_never_touches_entity_tables(db):
    called = AsyncMock()
    qdrant, patches = _lane_patches([_hit("m1", 0.9)], [])
    with patches[0], patches[1], patches[2], patches[3], patch(
        "genesis.db.crud.entities.get_by_norm_name", new=called,
    ):
        await rank_candidates(
            ema=EMA, entity_query="omi", db=db,
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            entity_lane="off",
        )
    called.assert_not_called()


@pytest.mark.asyncio
async def test_unresolvable_keywords_are_noise_filtered(db):
    # No entities at all → lane finds nothing, ranking unaffected
    qdrant, patches = _lane_patches([_hit("m1", 0.9)], [])
    shadow: list[dict] = []
    with patches[0], patches[1], patches[2], patches[3]:
        picked = await rank_candidates(
            ema=EMA, entity_query="confidence honest proceed", db=db,
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            entity_lane="shadow", entity_shadow_out=shadow,
        )
    assert [c["memory_id"] for c in picked] == ["m1"]
    assert shadow == []


@pytest.mark.asyncio
async def test_created_before_drops_future_entity_candidates(db):
    await _seed_spine(db)
    qdrant, patches = _lane_patches([_hit("m1", 0.9)], [_point(TARGET)])
    shadow: list[dict] = []
    with patches[0], patches[1], patches[2], patches[3]:
        await rank_candidates(
            ema=EMA, entity_query="omi", db=db,
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            entity_lane="shadow", entity_shadow_out=shadow,
            created_before="2026-06-01T00:00:00+00:00",  # before memory created
        )
    assert shadow == []  # post-cutoff memory filtered even from shadow


@pytest.mark.asyncio
async def test_multiword_entity_terms_resolve_verbatim(db):
    """Alias-normalized ledger keys ("cc" → "claude code") are multi-word;
    entity_terms must reach resolution unsplit or those entities are
    permanently unreachable (Codex P2, 2026-07-10)."""
    ent = await entities_crud.create_entity(
        db, name="Claude Code", norm_name="claude code", entity_type="product",
    )
    await entities_crud.upsert_mention(
        db, memory_id=TARGET, entity_id=ent, provenance="EXTRACTED",
        confidence=0.95, source="seed",
    )
    qdrant, patches = _lane_patches([_hit("other-mem", 0.9)], [_point(TARGET)])
    shadow: list[dict] = []
    with patches[0], patches[1], patches[2], patches[3]:
        await rank_candidates(
            ema=EMA, entity_query="claude code", db=db,
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            entity_lane="shadow", entity_shadow_out=shadow,
            entity_terms=["claude code"],
        )
    assert shadow and shadow[0]["memory_id"] == TARGET

    # The legacy split path documents WHY the param exists: the same
    # string via entity_query alone shatters and resolves nothing.
    qdrant2, patches2 = _lane_patches([_hit("other-mem", 0.9)], [_point(TARGET)])
    shadow2: list[dict] = []
    with patches2[0], patches2[1], patches2[2], patches2[3]:
        await rank_candidates(
            ema=EMA, entity_query="claude code", db=db,
            qdrant_client=qdrant2, embedding_provider=MagicMock(),
            entity_lane="shadow", entity_shadow_out=shadow2,
        )
    assert shadow2 == []


@pytest.mark.asyncio
async def test_live_drops_entity_candidates_without_episodic_point(db):
    """Mentions cover every write path (KB, FTS5-only) but the lane
    retrieves episodic only — a mention with no episodic point must not
    be floor-forced into the arbiter as an empty score-0 candidate."""
    await _seed_spine(db)
    # retrieve returns NOTHING for the target
    qdrant, patches = _lane_patches([_hit("m1", 0.9)], [])
    with patches[0], patches[1], patches[2], patches[3]:
        picked = await rank_candidates(
            ema=EMA, entity_query="omi", db=db,
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            entity_lane="live",
        )
    assert TARGET not in [c["memory_id"] for c in picked]
    assert [c["memory_id"] for c in picked] == ["m1"]
