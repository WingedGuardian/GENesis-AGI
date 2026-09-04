"""E3 write wiring: mechanical anchors, record_extraction, store seam."""

from __future__ import annotations

import asyncio
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


@pytest_asyncio.fixture
async def file_db():
    """File-backed DB at the conftest-redirected ``genesis_db_path()``.

    ``record_anchors`` opens its OWN ``get_raw_db(genesis_db_path())`` connection,
    so the test must share the SAME on-disk file (WAL-aware) to observe its
    committed writes — a ``:memory:`` connection would be a different, private
    database and the mentions would never appear here. The autouse
    ``_isolate_genesis_db_path`` conftest fixture already redirects
    ``genesis_db_path()`` to ``tmp_path/isolated-genesis.db``.
    """
    from genesis.env import genesis_db_path

    conn = await aiosqlite.connect(str(genesis_db_path()))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
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
    async def test_record_anchors_writes_mentions(self, file_db):
        # record_anchors owns its own get_raw_db(genesis_db_path()) connection; the
        # writes must appear on the shared on-disk file (file_db reads the SAME file).
        n = await record_anchors(
            "mem-1", "touched src/genesis/memory/store.py in PR #977",
        )
        assert n == 2
        rows = await file_db.execute_fetchall(
            "SELECT entity_id FROM entity_mentions WHERE memory_id = 'mem-1'"
        )
        assert len(rows) == 2
        entity = await entities_crud.get_by_norm_name(
            file_db, norm_name="src/genesis/memory/store.py",
        )
        assert entity["entity_type"] == "code_file"
        assert entity["source"] == "mechanical"

    @pytest.mark.asyncio
    async def test_record_anchors_batch_is_atomic_on_failure(self, file_db):
        """A mid-batch write failure rolls the WHOLE owned-conn batch back.

        The owned ``BEGIN IMMEDIATE`` … ``COMMIT`` envelope means an exception on a
        later anchor discards the earlier anchors' writes too — nothing partial is
        left behind (verify-RED: with a per-op commit the first anchor's entity
        would survive). record_anchors is best-effort, so the error propagates to
        store()'s suppress; here we assert both the raise and the empty tables.
        """
        # Two anchors; blow up on the SECOND upsert_mention so the first anchor's
        # entity+mention are already written (uncommitted) when the batch aborts.
        calls = {"n": 0}
        real_upsert = entities_crud.upsert_mention

        async def _boom_on_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected mid-batch failure")
            return await real_upsert(*args, **kwargs)

        content = "touched src/genesis/memory/store.py and genesis.memory.retrieval"
        with patch(
            "genesis.db.crud.entities.upsert_mention", side_effect=_boom_on_second
        ), pytest.raises(RuntimeError, match="injected mid-batch failure"):
            await record_anchors("mem-2", content)

        # Whole batch rolled back: neither the mentions NOR the first anchor's entity
        # survive on the shared file.
        mentions = await file_db.execute_fetchall(
            "SELECT 1 FROM entity_mentions WHERE memory_id = 'mem-2'"
        )
        assert mentions == []
        entity = await entities_crud.get_by_norm_name(
            file_db, norm_name="src/genesis/memory/store.py",
        )
        assert entity is None

    @pytest.mark.asyncio
    async def test_record_anchors_isolated_from_concurrent_writer(self, file_db):
        """Acceptance bar: a concurrent writer on a SEPARATE connection cannot see
        (or force-commit) record_anchors' in-flight batch — the owned connection
        isolates it.

        This is the concurrency gap the fix closes: on the OLD shared
        ``SerializedConnection`` a peer coroutine's ``commit()`` could durably commit
        the half-written batch (the lock releases between ops). Here we pause the
        batch after its first mention, prove a peer connection sees NOTHING (the
        writes live on record_anchors' own uncommitted txn), let it finish, and prove
        both anchors then appear.
        """
        first_write = asyncio.Event()
        release = asyncio.Event()
        real_upsert = entities_crud.upsert_mention
        calls = {"n": 0}

        async def _pause_after_first(*args, **kwargs):
            result = await real_upsert(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                first_write.set()
                await release.wait()
            return result

        content = "touched src/genesis/memory/store.py and genesis.memory.retrieval"
        with patch(
            "genesis.db.crud.entities.upsert_mention", side_effect=_pause_after_first
        ):
            task = asyncio.create_task(record_anchors("mem-iso", content))
            await asyncio.wait_for(first_write.wait(), timeout=5)

            # A concurrent peer commits on a DIFFERENT connection — it must not make
            # record_anchors' first-anchor write (uncommitted on its OWN conn) visible.
            await file_db.commit()
            mid = await file_db.execute_fetchall(
                "SELECT 1 FROM entity_mentions WHERE memory_id = 'mem-iso'"
            )
            assert mid == []  # isolated: the owned batch is invisible until it commits

            release.set()
            assert await asyncio.wait_for(task, timeout=5) == 2

        # Once record_anchors commits its owned batch, both anchors are visible.
        final = await file_db.execute_fetchall(
            "SELECT 1 FROM entity_mentions WHERE memory_id = 'mem-iso'"
        )
        assert len(final) == 2


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


class TestAnchorWritesGoWhereTheCallerSaid:
    """``db_path`` names WHICH database, since the connection is no longer the
    caller's to pass (Codex P2, PR #1653).

    ``scripts/entity_backfill.py`` supports ``--db`` to run against a copy or a
    restored backup. Dropping the old positional connection without replacing
    the target would have sent every anchor write to the live default while the
    run reported success against the file the operator named — silent, and
    against the wrong database.
    """

    @pytest.mark.asyncio
    async def test_an_explicit_db_path_is_honoured(self, tmp_path):
        from genesis.env import genesis_db_path

        target = tmp_path / "operator-chosen.db"
        conn = await aiosqlite.connect(str(target))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        for table in ("entities", "entity_mentions", "entity_links",
                      "deferred_work_queue"):
            await conn.execute(TABLES[table])
        await conn.commit()
        try:
            n = await record_anchors(
                "mem-target", "touched src/genesis/memory/store.py in PR #977",
                db_path=str(target),
            )
            assert n == 2
            rows = await conn.execute_fetchall(
                "SELECT entity_id FROM entity_mentions WHERE memory_id = 'mem-target'"
            )
            assert len(rows) == 2, "the write did not land in the named database"
        finally:
            await conn.close()

        # ...and NOWHERE ELSE. The control that makes this test mean something:
        # without it, a call that wrote to BOTH databases would pass.
        default = await aiosqlite.connect(str(genesis_db_path()))
        try:
            for table in ("entities", "entity_mentions", "entity_links",
                          "deferred_work_queue"):
                await default.execute(TABLES[table])
            await default.commit()
            leaked = await default.execute_fetchall(
                "SELECT 1 FROM entity_mentions WHERE memory_id = 'mem-target'"
            )
            assert not leaked, "anchors leaked into the default database"
        finally:
            await default.close()

    @pytest.mark.asyncio
    async def test_no_db_path_still_uses_the_resolved_default(self, file_db):
        """CONTROL. Every other caller passes no target and must keep landing in
        ``genesis_db_path()`` — the parameter is additive."""
        n = await record_anchors("mem-default", "see src/genesis/memory/store.py")
        assert n == 1
        rows = await file_db.execute_fetchall(
            "SELECT entity_id FROM entity_mentions WHERE memory_id = 'mem-default'"
        )
        assert len(rows) == 1

    def test_the_backfill_script_calls_it_the_new_way(self):
        """The caller the finding is actually about. It passes three positionals
        against a two-positional signature, so the run aborts with TypeError at
        the FIRST memory carrying an anchor — read from the script's source
        because importing it pulls argparse/aiosqlite plumbing this test does
        not need."""
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[2] / "scripts" / "entity_backfill.py"
        ).read_text()
        calls = [
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "record_anchors"
        ]
        assert len(calls) == 1, "the backfill's call site moved — retarget this test"
        call = calls[0]
        assert len(call.args) == 2, (
            f"{len(call.args)} positional args; the signature takes memory_id and "
            "content only, so anything else raises TypeError mid-backfill"
        )
        assert "db_path" in {kw.arg for kw in call.keywords}, (
            "--db is not honoured: writes would go to the default database while "
            "the run reports against the file the operator named"
        )
