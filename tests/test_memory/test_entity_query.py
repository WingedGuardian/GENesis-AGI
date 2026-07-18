"""Query→entity resolution lane for hybrid recall — SHADOW probe.

Covers ``genesis.memory.entity_query``:

- ``entity_lane_mode``: off/shadow recognized; live (reserved for PR-2) and
  typos degrade to ``off`` (more conservative than graph_expansion — an
  unshipped lane must not run 8k-row scans on a hot path from a hand-edit).
- ``resolve_query_entities``: alias-normalized, lowercased, n-gram membership
  against the active norm_name set; read-only (no writes / no entity creation).
- ``compute_entity_lane``: connected-entity walk → mention scoring → cap;
  never raises.
- ``maybe_entity_lane_shadow``: emits ``entity_lane_shadow`` eval_events in
  shadow mode, returns None always, filters expired/deprecated candidates out
  of the novelty count (measurement parity with the main pipeline), and is a
  silent no-op when off.
"""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import entities as entities_crud
from genesis.db.crud import memory as memory_crud
from genesis.memory import entity_query, graph_expansion

TARGET = "9d36f039-0000-0000-0000-000000000000"


# ── fixtures ─────────────────────────────────────────────────────────────


async def _seed_spine(db):
    """omi →is_a→ voice-edge-device →constrained_by→ repo-split → TARGET."""
    omi = await entities_crud.create_entity(
        db,
        name="OMI",
        norm_name="omi",
        entity_type="device",
    )
    cat = await entities_crud.create_entity(
        db,
        name="voice-edge-device",
        norm_name="voice-edge-device",
        entity_type="concept",
    )
    rule = await entities_crud.create_entity(
        db,
        name="repo-split",
        norm_name="repo-split",
        entity_type="concept",
    )
    await entities_crud.upsert_link(
        db,
        source_id=omi,
        target_id=cat,
        link_type="is_a",
        provenance="EXTRACTED",
        confidence=0.95,
    )
    await entities_crud.upsert_link(
        db,
        source_id=cat,
        target_id=rule,
        link_type="constrained_by",
        provenance="EXTRACTED",
        confidence=0.95,
    )
    await entities_crud.upsert_mention(
        db,
        memory_id=TARGET,
        entity_id=rule,
        provenance="EXTRACTED",
        confidence=0.95,
        source="seed",
    )
    return omi


async def _seed_memory(db, memory_id, *, invalid_at=None, deprecated=0):
    await memory_crud.create(
        db,
        memory_id=memory_id,
        content=f"content of {memory_id}",
        source_type="memory",
        collection="episodic_memory",
    )
    await memory_crud.create_metadata(
        db,
        memory_id=memory_id,
        created_at="2026-07-01T00:00:00Z",
        collection="episodic_memory",
        origin_class="owner",
    )
    if invalid_at is not None:
        await db.execute(
            "UPDATE memory_metadata SET invalid_at = ? WHERE memory_id = ?",
            (invalid_at, memory_id),
        )
    if deprecated:
        await db.execute(
            "UPDATE memory_metadata SET deprecated = 1 WHERE memory_id = ?",
            (memory_id,),
        )
    await db.commit()


async def _events(db, event_type="entity_lane_shadow"):
    cur = await db.execute(
        "SELECT metrics_json FROM eval_events WHERE event_type = ?",
        (event_type,),
    )
    return [json.loads(r[0]) for r in await cur.fetchall()]


# ── entity_lane_mode ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "section,expected",
    [
        ({"mode": "off"}, "off"),
        ({"mode": "shadow"}, "shadow"),
        ({"mode": "live"}, "off"),  # reserved for PR-2 → conservative off
        ({"mode": "banana"}, "off"),  # typo → off (never surprise-enable)
        ({"mode": False}, "off"),  # YAML-1.1 unquoted `off`
        ({}, "off"),
        (None, "off"),
    ],
)
def test_entity_lane_mode(monkeypatch, section, expected):
    monkeypatch.setattr(
        graph_expansion,
        "load_recall_config",
        lambda: {"enabled": True, "entity_lane": section},
    )
    assert entity_query.entity_lane_mode() == expected


# ── resolve_query_entities ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_exact_and_multiword(db):
    await _seed_spine(db)
    # unigram
    w = await entity_query.resolve_query_entities(db, "what about omi lately")
    assert w and all(v == 1.0 for v in w.values())
    # multiword norm_name resolves via bigram
    w2 = await entity_query.resolve_query_entities(db, "the repo-split decision")
    assert w2  # 'repo-split' is one whitespace token here


@pytest.mark.asyncio
async def test_resolve_empty_and_unresolvable(db):
    assert await entity_query.resolve_query_entities(db, "") == {}
    assert await entity_query.resolve_query_entities(db, "   ") == {}
    # no entity matches these stopword-ish terms
    await _seed_spine(db)
    assert await entity_query.resolve_query_entities(db, "the a is of") == {}


@pytest.mark.asyncio
async def test_resolve_is_read_only(db):
    await _seed_spine(db)
    before = (await (await db.execute("SELECT COUNT(*) FROM entities")).fetchone())[0]
    await entity_query.resolve_query_entities(db, "omi voice-edge-device repo-split")
    after = (await (await db.execute("SELECT COUNT(*) FROM entities")).fetchone())[0]
    assert after == before  # never creates entities


# ── compute_entity_lane ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_reaches_target_via_graph(db):
    omi = await _seed_spine(db)
    lane, reached = await entity_query.compute_entity_lane(db, {omi: 1.0})
    assert TARGET in lane  # omi → … → repo-split → TARGET mention
    assert reached >= 2  # voice-edge-device + repo-split reached


@pytest.mark.asyncio
async def test_compute_empty_weights(db):
    assert await entity_query.compute_entity_lane(db, {}) == ([], 0)


# ── maybe_entity_lane_shadow ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shadow_emits_event_and_returns_none(db, monkeypatch):
    await _seed_spine(db)
    await _seed_memory(db, TARGET)
    monkeypatch.setattr(entity_query, "entity_lane_mode", lambda: "shadow")
    ret = await entity_query.maybe_entity_lane_shadow(
        db,
        query="omi",
        ranked_lists=[["some-other-mem"]],
        all_ids={"some-other-mem"},
        limit=10,
        embedding_available=True,
    )
    assert ret is None  # NEVER changes recall output
    events = await _events(db)
    assert len(events) == 1
    m = events[0]
    assert m["entities_resolved"] == 1
    assert TARGET in m["sample_novel_ids"]  # reached + not in all_ids
    assert m["novel_candidates"] >= 1
    assert m["embedding_available"] is True


@pytest.mark.asyncio
async def test_shadow_excludes_expired_from_novel(db, monkeypatch):
    """SHOULD-FIX 1: an entity-reachable but bitemporally-expired memory must
    NOT count as a novel win — the main pipeline drops it at L693."""
    await _seed_spine(db)
    await _seed_memory(db, TARGET, invalid_at="2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(entity_query, "entity_lane_mode", lambda: "shadow")
    await entity_query.maybe_entity_lane_shadow(
        db,
        query="omi",
        ranked_lists=[["x"]],
        all_ids={"x"},
        limit=10,
        embedding_available=False,
    )
    m = (await _events(db))[0]
    assert m["novel_candidates"] == 0  # expired filtered out
    assert m["lane_candidates_prefilter"] >= 1  # it WAS reached, then dropped


@pytest.mark.asyncio
async def test_off_mode_emits_nothing(db, monkeypatch):
    await _seed_spine(db)
    await _seed_memory(db, TARGET)
    monkeypatch.setattr(entity_query, "entity_lane_mode", lambda: "off")
    ret = await entity_query.maybe_entity_lane_shadow(
        db,
        query="omi",
        ranked_lists=[["x"]],
        all_ids={"x"},
        limit=10,
        embedding_available=True,
    )
    assert ret is None
    assert await _events(db) == []


@pytest.mark.asyncio
async def test_shadow_never_raises_on_bad_input(db, monkeypatch):
    monkeypatch.setattr(entity_query, "entity_lane_mode", lambda: "shadow")
    # query None, empty ranked_lists — must not raise
    ret = await entity_query.maybe_entity_lane_shadow(
        db,
        query=None,
        ranked_lists=[],
        all_ids=set(),
        limit=10,
        embedding_available=True,
    )
    assert ret is None
