"""E3 write wiring: mechanical anchors, record_extraction, store seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import entities as entities_crud
from genesis.db.schema._tables import TABLES
from genesis.memory.entity_anchors import extract_anchors, record_anchors
from genesis.memory.entity_registry import record_extraction


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


@dataclass
class FakeExtraction:
    content: str = "x"
    extraction_type: str = "decision"
    confidence: float = 0.8
    entities: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    temporal: str | None = None


class TestAnchorExtraction:
    def test_paths_symbols_prs_shas(self):
        text = (
            "Fixed src/genesis/memory/store.py and genesis.memory.retrieval "
            "in PR #977; squash 0eb21377 landed. See also #58."
        )
        anchors = dict(extract_anchors(text))
        assert anchors["src/genesis/memory/store.py"] == "code_file"
        assert anchors["genesis.memory.retrieval"] == "code_symbol"
        assert anchors["pr#977"] == "pr"
        assert anchors["pr#58"] == "pr"
        assert anchors["0eb21377"] == "commit"

    def test_hex_needs_digit_and_no_word_false_positives(self):
        # all-letter hex-alphabet words must not match as SHAs
        anchors = extract_anchors("a decade of cafebabe efforts, no facade")
        assert not [a for a in anchors if a[1] == "commit"]
        # markdown heading '# 1' style must not match as PR
        assert not extract_anchors("# heading\n## another")

    def test_dedupe_and_cap(self):
        text = " ".join(f"src/genesis/m{i}.py" for i in range(30))
        text += " src/genesis/m0.py"
        anchors = extract_anchors(text)
        assert len(anchors) == 16  # capped, deduped

    @pytest.mark.asyncio
    async def test_record_anchors_writes_mentions(self, db):
        n = await record_anchors(
            db, "mem-1", "touched src/genesis/memory/store.py in PR #977",
        )
        assert n == 2
        rows = await db.execute_fetchall(
            "SELECT entity_id FROM entity_mentions WHERE memory_id = 'mem-1'"
        )
        assert len(rows) == 2
        entity = await entities_crud.get_by_norm_name(
            db, norm_name="src/genesis/memory/store.py",
        )
        assert entity["entity_type"] == "code_file"
        assert entity["source"] == "mechanical"


class TestRecordExtraction:
    @pytest.mark.asyncio
    async def test_entities_become_mentions(self, db):
        extraction = FakeExtraction(entities=["Qdrant", "Tailscale"])
        counts = await record_extraction(db, "mem-1", extraction, aliases={})
        assert counts == {"mentions": 2, "links": 0, "ambiguous": 0}
        rows = await db.execute_fetchall(
            "SELECT provenance, confidence, source FROM entity_mentions"
        )
        assert all(r[0] == "EXTRACTED" and r[1] == 0.8 for r in rows)
        assert all(r[2] == "llm_extraction" for r in rows)

    @pytest.mark.asyncio
    async def test_relationships_become_links_with_temporal(self, db):
        extraction = FakeExtraction(
            entities=["OMI"],
            relationships=[
                {"from": "OMI", "to": "voice-edge-device", "type": "is_a"},
            ],
            temporal="2026-06-14T00:00:00Z",
        )
        counts = await record_extraction(db, "mem-1", extraction, aliases={})
        assert counts["links"] == 1
        rows = await db.execute_fetchall(
            "SELECT link_type, valid_at, evidence_memory_id FROM entity_links"
        )
        assert rows[0][0] == "is_a"
        assert rows[0][1] == "2026-06-14T00:00:00+00:00"  # canonicalized
        assert rows[0][2] == "mem-1"

    @pytest.mark.asyncio
    async def test_relationship_ambiguous_flag_and_confidence(self, db):
        extraction = FakeExtraction(
            relationships=[
                {"from": "A", "to": "B", "type": "related_to",
                 "ambiguous": True, "confidence": 0.55},
            ],
        )
        await record_extraction(db, "mem-1", extraction, aliases={})
        rows = await db.execute_fetchall(
            "SELECT provenance, confidence FROM entity_links"
        )
        assert rows[0][0] == "AMBIGUOUS"
        assert rows[0][1] == 0.55

    @pytest.mark.asyncio
    async def test_reuses_seeded_typed_entity(self, db):
        seeded = await entities_crud.create_entity(
            db, name="OMI", norm_name="omi", entity_type="device",
            source="seed",
        )
        extraction = FakeExtraction(entities=["omi"])
        await record_extraction(db, "mem-1", extraction, aliases={})
        rows = await db.execute_fetchall("SELECT entity_id FROM entity_mentions")
        assert rows[0][0] == seeded  # concept-cluster cross-type reuse
        n = (await db.execute_fetchall("SELECT COUNT(*) FROM entities"))[0][0]
        assert n == 1  # no duplicate concept-typed OMI

    @pytest.mark.asyncio
    async def test_parser_preserves_relationship_provenance_fields(self, db):
        """END-TO-END through the real parser (review finding: the
        earlier test hand-built dicts and masked a parser field-drop)."""
        from genesis.memory.extraction import parse_extraction_response

        raw = (
            '{"extractions": [{"content": "OMI is a voice device", '
            '"type": "entity", "confidence": 0.8, "entities": ["OMI"], '
            '"relationships": [{"from": "OMI", "to": "voice-edge-device", '
            '"type": "is_a", "confidence": 0.55, "ambiguous": true}], '
            '"temporal": null}]}'
        )
        extractions = parse_extraction_response(raw)
        assert extractions, "parser returned nothing"
        await record_extraction(db, "mem-1", extractions[0], aliases={})
        rows = await db.execute_fetchall(
            "SELECT provenance, confidence FROM entity_links"
        )
        assert rows[0][0] == "AMBIGUOUS"
        assert rows[0][1] == 0.55

    @pytest.mark.asyncio
    async def test_merge_no_self_loop_on_preexisting_pair_link(self, db):
        """Review finding: merging entities that already link to each
        other must not mint a self-loop edge."""
        loser = await entities_crud.create_entity(
            db, name="QdrantDB", norm_name="qdrantdb", entity_type="product",
        )
        survivor = await entities_crud.create_entity(
            db, name="Qdrant", norm_name="qdrant", entity_type="product",
        )
        await entities_crud.upsert_link(
            db, source_id=loser, target_id=survivor, link_type="supersedes",
            provenance="EXTRACTED",
        )
        await entities_crud.merge_entity(db, loser_id=loser, survivor_id=survivor)
        rows = await db.execute_fetchall(
            "SELECT source_id, target_id FROM entity_links"
        )
        assert all(r[0] != r[1] for r in rows), f"self-loop minted: {list(rows)}"

    @pytest.mark.asyncio
    async def test_self_link_and_blank_names_skipped(self, db):
        extraction = FakeExtraction(
            relationships=[
                {"from": "Genesis", "to": "genesis", "type": "related_to"},
                {"from": "", "to": "X", "type": "related_to"},
            ],
        )
        counts = await record_extraction(db, "mem-1", extraction, aliases={})
        assert counts["links"] == 0


class TestStoreSeamFailOpen:
    @pytest.mark.asyncio
    async def test_store_survives_anchor_failure(self):
        from genesis.memory.store import MemoryStore

        ep = MagicMock()
        ep.embed = AsyncMock(return_value=[0.1] * 1024)
        ep.enrich = MagicMock(return_value="episodic: x")
        store = MemoryStore(
            embedding_provider=ep, qdrant_client=MagicMock(),
            db=AsyncMock(), linker=None,
        )
        with patch("genesis.memory.store.upsert_point"), \
             patch("genesis.memory.store.memory_crud") as mock_mem, \
             patch(
                 "genesis.memory.entity_anchors.record_anchors",
                 AsyncMock(side_effect=RuntimeError("boom")),
             ):
            mock_mem.upsert = AsyncMock(return_value="id")
            mock_mem.create_metadata = AsyncMock(return_value=None)
            memory_id = await store.store(
                content="anchor src/genesis/memory/store.py present",
                memory_type="episodic",
                source="test",
            )
        assert memory_id  # anchor failure never breaks the store


class TestCodexRemediationE3:
    """Regression tests for the 2026-07-10 Codex P2 findings (E3 side)."""

    def test_digit_only_ids_are_not_commit_anchors(self):
        # Plain numeric IDs (tickets/builds/counters) polluted the live
        # table with 559 fake commit entities pre-fix.
        for text in ("build 1234567890 done", "counter 000000001 rolled"):
            anchors = extract_anchors(text)
            assert not [a for a in anchors if a[1] == "commit"], text
        # Real SHAs (digit + hex letter) still match.
        anchors = dict(extract_anchors("squash d343a626 landed"))
        assert anchors.get("d343a626") == "commit"

    @pytest.mark.asyncio
    async def test_mechanical_resolution_ignores_aliases(self, db):
        # An alias like "cc" → "claude code" must never rewrite literal
        # identifiers (paths/symbols/PR#s/SHAs).
        from genesis.memory.entity_registry import resolve_entity

        eid, provenance = await resolve_entity(
            db, name="src/genesis/cc/direct_session.py",
            entity_type="code_file",
            aliases={"cc": "claude code"},
        )
        assert provenance == "EXTRACTED"
        rows = await db.execute_fetchall(
            "SELECT norm_name FROM entities WHERE entity_id = ?", (eid,),
        )
        assert rows[0][0] == "src/genesis/cc/direct_session.py"

    @pytest.mark.asyncio
    async def test_relationship_confidence_clamped(self, db):
        extraction = FakeExtraction(
            entities=["OMI"],
            relationships=[
                {"from": "OMI", "to": "left", "type": "is_a",
                 "confidence": 1.7},
                {"from": "OMI", "to": "right", "type": "part_of",
                 "confidence": -0.4},
            ],
        )
        await record_extraction(db, "mem-1", extraction, aliases={})
        rows = await db.execute_fetchall(
            "SELECT link_type, confidence FROM entity_links ORDER BY link_type",
        )
        assert [(r[0], r[1]) for r in rows] == [("is_a", 1.0), ("part_of", 0.0)]
