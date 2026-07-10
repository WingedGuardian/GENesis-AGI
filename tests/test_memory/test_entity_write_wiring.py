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
