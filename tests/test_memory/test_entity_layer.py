"""Entity layer substrate (E2): crud, registry tiering, seed spine.

The acceptance-critical invariant: OMI →is_a→ voice-edge-device
→constrained_by→ genesis-voice-repo-split, whose mention row anchors the
GENesis-Voice repo-split memory — reachable in ≤2 undirected valid hops.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import entities as entities_crud
from genesis.db.schema._tables import TABLES
from genesis.memory import entity_registry
from genesis.memory.entity_seed import SEED_MENTIONS, apply_seed


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


# Registry resolution never hits the alias YAML in tests.
_NO_ALIASES: dict[str, str] = {}


async def _mk(db, name, entity_type="concept", **kw):
    return await entities_crud.create_entity(
        db, name=name, norm_name=name.lower(), entity_type=entity_type, **kw
    )


class TestCrud:
    @pytest.mark.asyncio
    async def test_create_collision_returns_existing(self, db):
        first = await _mk(db, "Qdrant", "product")
        second = await _mk(db, "Qdrant", "product")
        assert first == second

    @pytest.mark.asyncio
    async def test_mention_upsert_keeps_stronger(self, db):
        eid = await _mk(db, "Qdrant", "product")
        await entities_crud.upsert_mention(
            db, memory_id="m1", entity_id=eid, provenance="EXTRACTED",
            confidence=0.9,
        )
        await entities_crud.upsert_mention(
            db, memory_id="m1", entity_id=eid, provenance="INFERRED",
            confidence=0.4,
        )
        rows = await entities_crud.memories_mentioning(db, [eid])
        assert rows[0]["confidence"] == 0.9
        assert rows[0]["provenance"] == "EXTRACTED"

    @pytest.mark.asyncio
    async def test_link_type_slugified(self, db):
        a = await _mk(db, "a")
        b = await _mk(db, "b")
        await entities_crud.upsert_link(
            db, source_id=a, target_id=b, link_type="Is A!",
            provenance="EXTRACTED",
        )
        rows = await db.execute_fetchall(
            "SELECT link_type FROM entity_links"
        )
        assert rows[0][0] == "is_a"

    @pytest.mark.asyncio
    async def test_connected_entities_two_hops_undirected(self, db):
        omi = await _mk(db, "omi", "device")
        cat = await _mk(db, "voice-edge-device")
        rule = await _mk(db, "repo-split")
        await entities_crud.upsert_link(
            db, source_id=omi, target_id=cat, link_type="is_a",
            provenance="EXTRACTED", confidence=0.95,
        )
        await entities_crud.upsert_link(
            db, source_id=cat, target_id=rule, link_type="constrained_by",
            provenance="EXTRACTED", confidence=0.95,
        )
        reached = await entities_crud.connected_entities(db, [omi])
        assert reached[cat]["depth"] == 1
        assert reached[rule]["depth"] == 2
        assert reached[rule]["via_link_type"] == "constrained_by"
        # sibling via undirected hop through the category
        pe = await _mk(db, "haos voice pe", "device")
        await entities_crud.upsert_link(
            db, source_id=pe, target_id=cat, link_type="is_a",
            provenance="EXTRACTED",
        )
        reached = await entities_crud.connected_entities(db, [omi])
        assert pe in reached  # OMI → category → sibling device

    @pytest.mark.asyncio
    async def test_connected_entities_validity_filter(self, db):
        a = await _mk(db, "a")
        b = await _mk(db, "b")
        await entities_crud.upsert_link(
            db, source_id=a, target_id=b, link_type="related_to",
            provenance="EXTRACTED",
        )
        n = await entities_crud.invalidate_links_for_entity(
            db, entity_id=b, invalid_at="2026-01-01T00:00:00+00:00",
            invalidated_by="test",
        )
        assert n == 1
        reached = await entities_crud.connected_entities(db, [a])
        assert reached == {}
        # as_of BEFORE the invalidation still sees the edge
        reached = await entities_crud.connected_entities(
            db, [a], as_of="2025-12-01T00:00:00+00:00",
        )
        assert b in reached

    @pytest.mark.asyncio
    async def test_ambiguous_provenance_weakens_path(self, db):
        a = await _mk(db, "a")
        b = await _mk(db, "b")
        await entities_crud.upsert_link(
            db, source_id=a, target_id=b, link_type="related_to",
            provenance="AMBIGUOUS", confidence=1.0,
        )
        reached = await entities_crud.connected_entities(db, [a])
        assert reached[b]["path_confidence"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_merge_rewrites_mentions_and_links(self, db):
        loser = await _mk(db, "qdrantdb", "product")
        survivor = await _mk(db, "qdrant", "product")
        other = await _mk(db, "genesis", "product")
        await entities_crud.upsert_mention(
            db, memory_id="m1", entity_id=loser, provenance="EXTRACTED",
        )
        await entities_crud.upsert_link(
            db, source_id=loser, target_id=other, link_type="part_of",
            provenance="EXTRACTED",
        )
        await entities_crud.merge_entity(db, loser_id=loser, survivor_id=survivor)
        rows = await entities_crud.memories_mentioning(db, [survivor])
        assert rows and rows[0]["memory_id"] == "m1"
        reached = await entities_crud.connected_entities(db, [survivor])
        assert other in reached
        # norm lookup on the loser follows the merge to the survivor
        resolved = await entities_crud.get_by_norm_name(
            db, norm_name="qdrantdb", entity_type="product",
        )
        assert resolved["entity_id"] == survivor


class TestRegistry:
    @pytest.mark.asyncio
    async def test_mechanical_exact_identity(self, db):
        eid1, prov = await entity_registry.resolve_entity(
            db, name="src/genesis/memory/store.py", entity_type="code_file",
            aliases=_NO_ALIASES,
        )
        eid2, _ = await entity_registry.resolve_entity(
            db, name="src/genesis/memory/store.py", entity_type="code_file",
            aliases=_NO_ALIASES,
        )
        assert eid1 == eid2
        assert prov == "EXTRACTED"

    @pytest.mark.asyncio
    async def test_named_cross_cluster_reuse(self, db):
        eid1, _ = await entity_registry.resolve_entity(
            db, name="OMI", entity_type="product", aliases=_NO_ALIASES,
        )
        eid2, prov = await entity_registry.resolve_entity(
            db, name="omi", entity_type="device", aliases=_NO_ALIASES,
        )
        assert eid1 == eid2
        assert prov == "EXTRACTED"

    @pytest.mark.asyncio
    async def test_fuzzy_creates_ambiguous_and_enqueues(self, db):
        await entity_registry.resolve_entity(
            db, name="GENesis-Voice", entity_type="repo", aliases=_NO_ALIASES,
        )
        eid, prov = await entity_registry.resolve_entity(
            db, name="GENesis-Voices", entity_type="repo", aliases=_NO_ALIASES,
        )
        assert prov == "AMBIGUOUS"
        rows = await db.execute_fetchall(
            "SELECT work_type, payload_json FROM deferred_work_queue"
        )
        assert rows and rows[0][0] == "entity_adjudication"
        assert eid in rows[0][1]

    @pytest.mark.asyncio
    async def test_distinct_name_is_extracted(self, db):
        _, prov = await entity_registry.resolve_entity(
            db, name="Tailscale", entity_type="product", aliases=_NO_ALIASES,
        )
        assert prov == "EXTRACTED"


class TestSeed:
    @pytest.mark.asyncio
    async def test_seed_idempotent_and_spine_reachable(self, db):
        counts1 = await apply_seed(db)
        counts2 = await apply_seed(db)
        assert counts1 == counts2
        n = (await db.execute_fetchall("SELECT COUNT(*) FROM entities"))[0][0]
        assert n == counts1["entities"]

        omi = await entities_crud.get_by_norm_name(db, norm_name="omi")
        assert omi is not None
        reached = await entities_crud.connected_entities(db, [omi["entity_id"]])
        depths = {}
        for eid, info in reached.items():
            row = await entities_crud.get_entity(db, eid)
            depths[row["norm_name"]] = info["depth"]
        assert depths.get("voice-edge-device") == 1
        assert depths.get("genesis-voice-repo-split") == 2

        mentions = await entities_crud.memories_mentioning(db, list(reached))
        target = SEED_MENTIONS[0][1]
        assert any(m["memory_id"] == target for m in mentions)


class TestCodexRemediationCrud:
    """Regression tests for the 2026-07-10 Codex P2 findings (crud side)."""

    @pytest.mark.asyncio
    async def test_upsert_link_keeps_dated_validity(self, db):
        a = await _mk(db, "A")
        b = await _mk(db, "B")
        # Undated weak claim, then a stronger dated one: date must land.
        await entities_crud.upsert_link(
            db, source_id=a, target_id=b, link_type="is_a",
            provenance="INFERRED", confidence=0.5,
        )
        await entities_crud.upsert_link(
            db, source_id=a, target_id=b, link_type="is_a",
            provenance="EXTRACTED", confidence=0.9, valid_at="2026-06-14",
        )
        rows = await db.execute_fetchall(
            "SELECT valid_at, confidence FROM entity_links",
        )
        assert rows[0][0] == "2026-06-14" and rows[0][1] == 0.9
        # An even stronger UNdated claim must not erase the known date.
        await entities_crud.upsert_link(
            db, source_id=a, target_id=b, link_type="is_a",
            provenance="EXTRACTED", confidence=0.95,
        )
        rows = await db.execute_fetchall(
            "SELECT valid_at, confidence FROM entity_links",
        )
        assert rows[0][0] == "2026-06-14" and rows[0][1] == 0.95

    @pytest.mark.asyncio
    async def test_merge_keeps_stronger_rows(self, db):
        survivor = await _mk(db, "OMI", "device")
        loser = await _mk(db, "omi-dupe", "concept")
        other = await _mk(db, "voice-edge-device")
        # Survivor holds the WEAKER mention and link; loser the stronger.
        await entities_crud.upsert_mention(
            db, memory_id="m1", entity_id=survivor, provenance="INFERRED",
            confidence=0.4,
        )
        await entities_crud.upsert_mention(
            db, memory_id="m1", entity_id=loser, provenance="EXTRACTED",
            confidence=0.9,
        )
        await entities_crud.upsert_link(
            db, source_id=survivor, target_id=other, link_type="is_a",
            provenance="INFERRED", confidence=0.3,
        )
        await entities_crud.upsert_link(
            db, source_id=loser, target_id=other, link_type="is_a",
            provenance="EXTRACTED", confidence=0.9, valid_at="2026-06-14",
        )
        await entities_crud.merge_entity(
            db, loser_id=loser, survivor_id=survivor,
        )
        mention = await db.execute_fetchall(
            "SELECT provenance, confidence FROM entity_mentions "
            "WHERE memory_id = 'm1' AND entity_id = ?",
            (survivor,),
        )
        assert list(mention[0]) == ["EXTRACTED", 0.9]
        link = await db.execute_fetchall(
            "SELECT provenance, confidence, valid_at FROM entity_links "
            "WHERE source_id = ? AND target_id = ?",
            (survivor, other),
        )
        assert list(link[0]) == ["EXTRACTED", 0.9, "2026-06-14"]

    @pytest.mark.asyncio
    async def test_merge_carries_invalidation_state(self, db):
        survivor = await _mk(db, "OMI", "device")
        loser = await _mk(db, "omi-dupe", "concept")
        other = await _mk(db, "voice-edge-device")
        # Survivor: weaker ACTIVE link. Loser: stronger link already
        # CLOSED — the merge must not resurrect it as active.
        await entities_crud.upsert_link(
            db, source_id=survivor, target_id=other, link_type="is_a",
            provenance="INFERRED", confidence=0.3,
        )
        await entities_crud.upsert_link(
            db, source_id=loser, target_id=other, link_type="is_a",
            provenance="EXTRACTED", confidence=0.9,
        )
        await entities_crud.invalidate_links_for_entity(
            db, entity_id=loser, invalid_at="2026-07-01",
            invalidated_by="superseding-memory",
        )
        await entities_crud.merge_entity(
            db, loser_id=loser, survivor_id=survivor,
        )
        link = await db.execute_fetchall(
            "SELECT confidence, invalid_at, invalidated_by "
            "FROM entity_links WHERE source_id = ? AND target_id = ?",
            (survivor, other),
        )
        assert link[0][0] == 0.9
        assert link[0][1] is not None
        assert link[0][2] == "superseding-memory"

    @pytest.mark.asyncio
    async def test_delete_entities_cascade(self, db):
        doomed = await _mk(db, "000000001", "commit")
        kept = await _mk(db, "OMI", "device")
        await entities_crud.upsert_mention(
            db, memory_id="m1", entity_id=doomed, provenance="EXTRACTED",
            confidence=0.9,
        )
        await entities_crud.upsert_mention(
            db, memory_id="m1", entity_id=kept, provenance="EXTRACTED",
            confidence=0.9,
        )
        await entities_crud.upsert_link(
            db, source_id=doomed, target_id=kept, link_type="mentions",
            provenance="EXTRACTED", confidence=0.8,
        )
        counts = await entities_crud.delete_entities_cascade(db, [doomed])
        assert counts == {"entities": 1, "mentions": 1, "links": 1}
        remaining = await db.execute_fetchall("SELECT entity_id FROM entities")
        assert [r[0] for r in remaining] == [kept]
        # Idempotent second pass + empty-input no-op.
        counts = await entities_crud.delete_entities_cascade(db, [doomed])
        assert counts == {"entities": 0, "mentions": 0, "links": 0}
        assert await entities_crud.delete_entities_cascade(db, []) == {
            "entities": 0, "mentions": 0, "links": 0,
        }
