"""Tests for memory-mcp server — verify all tools are registered with correct signatures."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.mcp.memory_mcp import mcp


async def _get_tools():
    return await mcp.get_tools()


async def test_all_tools_registered():
    tools = await _get_tools()
    expected = [
        "memory_recall", "memory_store", "memory_extract", "memory_proactive",
        "memory_core_facts", "memory_stats",
        "observation_write", "observation_query", "observation_resolve",
        "evolution_propose",
        "conversation_history",
        "knowledge_recall", "knowledge_ingest", "knowledge_status",
        "reference_store", "reference_lookup", "reference_delete",
        "reference_export",
    ]
    for name in expected:
        assert name in tools, f"Missing tool: {name}"


async def test_memory_recall_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["memory_recall"].fn(query="test")


async def test_observation_write_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["observation_write"].fn(content="test", source="test", type="test")


async def test_knowledge_recall_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["knowledge_recall"].fn(query="test")


async def test_knowledge_ingest_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["knowledge_ingest"].fn(content="test", project="p", domain="d")


async def test_evolution_propose_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["evolution_propose"].fn(
            proposal_type="test", current_content="a",
            proposed_change="b", rationale="c",
        )


async def test_procedure_store_registered():
    tools = await _get_tools()
    assert "procedure_store" in tools


async def test_procedure_recall_registered():
    tools = await _get_tools()
    assert "procedure_recall" in tools


async def test_procedure_store_recall_roundtrip():
    """End-to-end regression test for the procedure_store→procedure_recall bug.

    Pre-fix: procedure_store wrote speculative=1, success_count=0,
    confidence=0.0 → find_relevant filtered it out → procedure_recall
    returned []. This test stores a procedure and verifies it comes back.
    """
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store = MagicMock()
            mod._db = real_db
            mod._retriever = MagicMock()

            tools = await _get_tools()
            pid = await tools["procedure_store"].fn(
                task_type="discourse-forum-registration",
                principle="Browser is required; the raw API returns fake success.",
                steps=["navigate to /signup", "fill form", "click submit", "verify"],
                tools_used=["browser_navigate", "browser_fill", "browser_click"],
                context_tags=["discourse", "forum", "registration", "browser"],
            )
            assert isinstance(pid, str) and len(pid) == 36

            # Verify the row landed with explicit-teach defaults.
            cursor = await real_db.execute(
                "SELECT speculative, success_count, confidence, activation_tier "
                "FROM procedural_memory WHERE id = ?",
                (pid,),
            )
            row = await cursor.fetchone()
            assert row[0] == 0  # speculative
            assert row[1] == 1  # success_count
            assert abs(row[2] - 2 / 3) < 1e-9  # Laplace
            assert row[3] == "L3"  # activation_tier

            # Now recall the procedure — must be visible.
            results = await tools["procedure_recall"].fn(
                task_description="register on discourse forum",
                context_tags=["discourse", "forum", "registration"],
            )
            assert len(results) >= 1
            assert any(
                r.get("task_type") == "discourse-forum-registration"
                for r in results
            )
        finally:
            mod._store = old_store
            mod._db = old_db
            mod._retriever = old_retriever


# ─── End-to-end knowledge_ingest test ────────────────────────────────────────


async def test_knowledge_ingest_stores_with_correct_qdrant_id():
    """Verify knowledge_ingest writes qdrant_id that matches actual Qdrant point.

    Uses a real in-memory SQLite DB so the upsert + FTS5 paths run for real;
    mocks only MemoryStore/retriever/qdrant.
    """
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = None
        await create_all_tables(real_db)
        await real_db.commit()

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="qdrant-uuid-123")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "test-embed-model"

        mock_retriever = AsyncMock()
        mock_qdrant = MagicMock()

        old_store, old_db, old_retriever, old_qdrant = (
            mod._store, mod._db, mod._retriever, mod._qdrant,
        )
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = mock_retriever
            mod._qdrant = mock_qdrant

            tools = await _get_tools()
            result = await tools["knowledge_ingest"].fn(
                content="VPC subnets are subdivisions of a VPC CIDR range",
                project="cloud-eng",
                domain="aws-vpc",
                authority="course",
            )

            assert result  # returns unit_id string

            # Verify MemoryStore.store was called with knowledge routing
            mock_store.store.assert_called_once()
            call_kwargs = mock_store.store.call_args
            assert call_kwargs[1]["memory_type"] == "knowledge"
            assert call_kwargs[1]["auto_link"] is False
            assert call_kwargs[1]["collection"] == "knowledge_base"

            # Verify the row actually landed with the qdrant_id from MemoryStore
            row = await mod.knowledge.get(real_db, result)
            assert row is not None
            assert row["qdrant_id"] == "qdrant-uuid-123"
            assert row["embedding_model"] == "test-embed-model"
            assert row["project_type"] == "cloud-eng"
            assert row["domain"] == "aws-vpc"

            # Re-ingest the same content → upsert path: same unit_id, no dup
            mock_store.store = AsyncMock(return_value="qdrant-uuid-123")
            result2 = await tools["knowledge_ingest"].fn(
                content="VPC subnets are subdivisions of a VPC CIDR range",
                project="cloud-eng",
                domain="aws-vpc",
                authority="course",
            )
            assert result2 == result  # stable id on conflict

            # Change the body (different content, same concept prefix) →
            # upsert path + Qdrant replacement
            mock_store.store = AsyncMock(return_value="qdrant-uuid-456")
            result3 = await tools["knowledge_ingest"].fn(
                content="VPC subnets are subdivisions of a VPC CIDR range "
                        "and are bound to a single availability zone",
                project="cloud-eng",
                domain="aws-vpc",
                authority="course",
                concept="VPC Subnets",  # explicit concept — same logical entry
            )
            # First call used derived concept (content[:200]), this uses an
            # explicit override — so it creates a NEW row with a different
            # concept, not an upsert. That's correct behavior.
            assert result3 != result
        finally:
            mod._store = old_store
            mod._db = old_db
            mod._retriever = old_retriever
            mod._qdrant = old_qdrant


async def test_knowledge_ingest_memory_class_override():
    """Verify memory_class parameter threads through to MemoryStore.store."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="qdrant-xyz")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = AsyncMock()
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            await tools["knowledge_ingest"].fn(
                content="login at https://example.com/login",
                project="reference",
                domain="reference.url",
                memory_class="fact",  # override to avoid 0.7x penalty
            )
            assert mock_store.store.call_args[1]["memory_class"] == "fact"
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


# ─── Reference store tools ──────────────────────────────────────────────────


async def test_reference_store_registered():
    tools = await _get_tools()
    for name in ("reference_store", "reference_lookup", "reference_delete", "reference_export"):
        assert name in tools, f"Missing reference tool: {name}"


async def test_reference_store_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["reference_store"].fn(
            kind="credentials",
            identifier="test",
            value="x",
            description="test desc",
        )


async def test_reference_store_validates_kind():
    """Unknown kinds should be rejected before any DB work."""
    import genesis.mcp.memory_mcp as mod

    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = AsyncMock()
        mod._db = AsyncMock()
        mod._retriever = AsyncMock()
        mod._qdrant = MagicMock()
        tools = await _get_tools()
        with pytest.raises(ValueError, match="unknown kind"):
            await tools["reference_store"].fn(
                kind="nonsense",
                identifier="test",
                value="x",
                description="desc",
            )
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_store_requires_description():
    import genesis.mcp.memory_mcp as mod

    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = AsyncMock()
        mod._db = AsyncMock()
        mod._retriever = AsyncMock()
        mod._qdrant = MagicMock()
        tools = await _get_tools()
        with pytest.raises(ValueError, match="description is required"):
            await tools["reference_store"].fn(
                kind="credentials",
                identifier="test",
                value="x",
                description="",
            )
        with pytest.raises(ValueError, match="description is required"):
            await tools["reference_store"].fn(
                kind="credentials",
                identifier="test",
                value="x",
                description="   ",
            )
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_store_full_roundtrip():
    """End-to-end reference_store → knowledge_units row with expected shape."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="qdrant-cred-1")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "test-embed"

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = AsyncMock()
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            unit_id = await tools["reference_store"].fn(
                kind="credentials",
                identifier="ScarletAndRage forum login",
                value="614Buckeye / hunter2",
                description=(
                    "Login for forum.thescarletandrage.com, used by the "
                    "614Buckeye persona. Ohio State fan forum."
                ),
                tags=["forum", "persona:614buckeye"],
                source={
                    "session_id": "sess-abc",
                    "captured_via": "user_paste",
                    "captured_at": "2026-04-11T12:00:00+00:00",
                },
            )
            assert unit_id

            # Verify the row landed with expected shape
            row = await mod.knowledge.get(real_db, unit_id)
            assert row["project_type"] == "reference"
            assert row["domain"] == "reference.credentials"
            assert row["concept"] == "ScarletAndRage forum login"
            assert "614Buckeye / hunter2" in row["body"]
            assert "Ohio State fan forum" in row["body"]
            assert "forum" in row["tags"]
            assert "persona:614buckeye" in row["tags"]
            assert "reference" in row["tags"]
            assert "credentials" in row["tags"]
            assert row["qdrant_id"] == "qdrant-cred-1"

            # Verify memory_class="fact" was forced to avoid 0.7x penalty
            store_kwargs = mock_store.store.call_args[1]
            assert store_kwargs["memory_class"] == "fact"
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_store_upsert_preserves_id():
    """Re-storing the same (kind, identifier) updates in place."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="qdrant-a")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = AsyncMock()
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            uid_a = await tools["reference_store"].fn(
                kind="network",
                identifier="Container IP",
                value="${CONTAINER_IP:-localhost}",
                description="Incus container running Genesis runtime",
            )

            # Rotate the value (same identifier)
            mock_store.store = AsyncMock(return_value="qdrant-b")
            uid_b = await tools["reference_store"].fn(
                kind="network",
                identifier="Container IP",
                value="10.176.34.207",
                description="Incus container running Genesis runtime (rotated)",
            )
            assert uid_b == uid_a  # stable on conflict

            row = await mod.knowledge.get(real_db, uid_a)
            assert "10.176.34.207" in row["body"]
            assert "rotated" in row["body"]
            assert row["qdrant_id"] == "qdrant-b"

            # The old Qdrant point should have been cleaned up
            mock_store.delete.assert_called_with("qdrant-a")

            # Still only one row for this (kind, identifier)
            cur = await real_db.execute(
                "SELECT COUNT(*) FROM knowledge_units "
                "WHERE project_type='reference' AND domain='reference.network' "
                "AND concept='Container IP'"
            )
            assert (await cur.fetchone())[0] == 1
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_store_different_kinds_no_body_collision():
    """I2 regression: two entries with different (kind, identifier) but
    identical description/value/tags must produce distinct bodies so
    MemoryStore.store's find_exact_duplicate doesn't silently collapse
    them to the same Qdrant point.
    """
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        store_call_contents: list[str] = []

        async def fake_store(content, *args, **kwargs):
            store_call_contents.append(content)
            return f"qdrant-{len(store_call_contents)}"

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(side_effect=fake_store)
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = AsyncMock()
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            await tools["reference_store"].fn(
                kind="network", identifier="server",
                value="10.0.0.1", description="test",
            )
            await tools["reference_store"].fn(
                kind="url", identifier="server",
                value="10.0.0.1", description="test",
            )
            # Bodies passed to store.store must differ — the header line
            # [reference.kind] identifier provides the salt.
            assert len(store_call_contents) == 2
            assert store_call_contents[0] != store_call_contents[1]
            assert "reference.network" in store_call_contents[0]
            assert "reference.url" in store_call_contents[1]
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_lookup_logs_credential_access():
    """Credential-kind hits write to credential_access_log."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q1")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        mock_retriever = AsyncMock()
        mock_retriever.recall = AsyncMock(return_value=[])

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = mock_retriever
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            uid = await tools["reference_store"].fn(
                kind="credentials",
                identifier="Example service login",
                value="user / pass",
                description="Credentials for example.com staging environment",
            )

            # Look it up
            results = await tools["reference_lookup"].fn(
                query="example",
                kind="credentials",
                accessor_context="test",
            )
            assert len(results) >= 1
            assert any(r["unit_id"] == uid for r in results)

            # Verify audit log row exists
            cur = await real_db.execute(
                "SELECT unit_id, accessor_context FROM credential_access_log "
                "WHERE unit_id = ?", (uid,),
            )
            audit_row = await cur.fetchone()
            assert audit_row is not None
            assert audit_row[0] == uid
            assert audit_row[1] == "test"
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_lookup_non_credentials_no_audit():
    """Non-credentials lookups should NOT write credential_access_log rows."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q1")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        mock_retriever = AsyncMock()
        mock_retriever.recall = AsyncMock(return_value=[])

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = mock_retriever
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            await tools["reference_store"].fn(
                kind="url",
                identifier="example forum",
                value="https://example.com/forum",
                description="Public example forum for testing",
            )

            await tools["reference_lookup"].fn(query="example", kind="url")

            cur = await real_db.execute(
                "SELECT COUNT(*) FROM credential_access_log"
            )
            assert (await cur.fetchone())[0] == 0
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_lookup_hybrid_vector_path():
    """I1 regression: reference_lookup must also consult the vector retriever.

    Builds a reference entry via reference_store, then simulates a vector
    hit that returns it (no FTS match for the semantic query), and verifies
    the result surfaces.
    """
    from types import SimpleNamespace

    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-vector-1")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        # Retriever returns a hit whose memory_id matches whatever
        # reference_store ends up generating. We'll patch recall AFTER the
        # store call so the lookup sees the vector hit.
        mock_retriever = AsyncMock()
        mock_retriever.recall = AsyncMock(return_value=[])

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = mock_retriever
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            unit_id = await tools["reference_store"].fn(
                kind="persona_pointer",
                identifier="614Buckeye persona",
                value="~/.claude/personas/614buckeye/persona.md",
                description=(
                    "Ohio State fan persona for low-key forum engagement"
                ),
            )

            # Semantic query that does NOT match any FTS5 token in the body.
            # "ohio buckeyes fan" vs body containing "Ohio State fan persona" —
            # FTS5 matches "ohio" and "fan" so FTS would still return this;
            # test that the vector path ALSO returns it when FTS misses by
            # querying with tokens that aren't in the body.
            mock_retriever.recall = AsyncMock(return_value=[
                SimpleNamespace(memory_id=unit_id, score=0.87),
            ])

            results = await tools["reference_lookup"].fn(
                query="xyzzy-no-match-in-body",  # FTS will return nothing
                kind="persona_pointer",
            )
            assert len(results) == 1
            assert results[0]["unit_id"] == unit_id
            # Verify origin was vector-only since FTS missed
            assert results[0]["origin"] == "vector"
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_lookup_hybrid_merges_both_paths():
    """Vector and FTS hits for the same entry merge to origin='both'."""
    from types import SimpleNamespace

    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-both")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        mock_retriever = AsyncMock()
        mock_retriever.recall = AsyncMock(return_value=[])

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = mock_retriever
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            uid = await tools["reference_store"].fn(
                kind="url",
                identifier="example forum",
                value="https://example.com/forum",
                description="Public example forum for testing",
            )

            mock_retriever.recall = AsyncMock(return_value=[
                SimpleNamespace(memory_id=uid, score=0.9),
            ])

            results = await tools["reference_lookup"].fn(
                query="example forum", kind="url",
            )
            assert len(results) == 1
            assert results[0]["unit_id"] == uid
            assert results[0]["origin"] == "both"
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_delete_roundtrip():
    """reference_delete removes the row + cleans Qdrant point."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-delete-me")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = AsyncMock()
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            uid = await tools["reference_store"].fn(
                kind="fact",
                identifier="to delete",
                value="x",
                description="a fact that will be deleted in this test",
            )
            # Delete
            deleted = await tools["reference_delete"].fn(unit_id=uid)
            assert deleted is True
            # Verify row is gone
            assert await mod.knowledge.get(real_db, uid) is None
            # Qdrant cleanup was invoked
            mock_store.delete.assert_called_with("q-delete-me")
            # Second delete returns False
            assert await tools["reference_delete"].fn(unit_id=uid) is False
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_delete_survives_audit_rows_with_fk_on():
    """B1 regression: deleting a credentials entry that has audit log rows
    must succeed even with PRAGMA foreign_keys=ON. The credential_access_log
    intentionally has no FK so audit history outlives the entry it describes.
    """
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        # Match production connection.py which enables foreign keys
        await real_db.execute("PRAGMA foreign_keys=ON")
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-fk-test")
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        mock_retriever = AsyncMock()
        mock_retriever.recall = AsyncMock(return_value=[])

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = mock_retriever
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            uid = await tools["reference_store"].fn(
                kind="credentials",
                identifier="FK test login",
                value="u / p",
                description="Credentials for FK regression test",
            )
            # Populate credential_access_log by looking up the entry
            await tools["reference_lookup"].fn(
                query="FK test", kind="credentials",
            )
            cur = await real_db.execute(
                "SELECT COUNT(*) FROM credential_access_log WHERE unit_id = ?",
                (uid,),
            )
            assert (await cur.fetchone())[0] >= 1

            # Now delete — must succeed even though audit rows reference the unit
            deleted = await tools["reference_delete"].fn(unit_id=uid)
            assert deleted is True
            # Audit rows should still exist (outlive the entry)
            cur = await real_db.execute(
                "SELECT COUNT(*) FROM credential_access_log WHERE unit_id = ?",
                (uid,),
            )
            assert (await cur.fetchone())[0] >= 1
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_delete_refuses_non_reference_unit():
    """reference_delete should not be usable as a generic knowledge_unit delete."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        # Insert a non-reference knowledge unit directly
        uid = await mod.knowledge.insert(
            real_db,
            project_type="cloud-eng",
            domain="aws",
            source_doc="m1",
            concept="VPC",
            body="VPC content",
        )

        mock_store = AsyncMock()
        mock_store.delete = AsyncMock()

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = AsyncMock()
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            with pytest.raises(ValueError, match="not a reference entry"):
                await tools["reference_delete"].fn(unit_id=uid)
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_export_returns_stats():
    """reference_export returns counts grouped by domain."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(side_effect=["q1", "q2", "q3"])
        mock_store.delete = AsyncMock()
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = AsyncMock()
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            await tools["reference_store"].fn(
                kind="url", identifier="u1", value="https://a.example",
                description="alpha",
            )
            await tools["reference_store"].fn(
                kind="url", identifier="u2", value="https://b.example",
                description="bravo",
            )
            await tools["reference_store"].fn(
                kind="network", identifier="n1", value="10.0.0.1",
                description="charlie",
            )

            summary = await tools["reference_export"].fn()
            assert summary["project_type"] == "reference"
            assert summary["total"] == 3
            assert summary["by_domain"]["reference.url"] == 2
            assert summary["by_domain"]["reference.network"] == 1
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


# ─── evolution_propose test ─────────────────────────────────────────────────


async def test_evolution_propose_stores_pending():
    """Verify evolution_propose creates a pending proposal."""
    import genesis.mcp.memory_mcp as mod

    mock_db = AsyncMock()
    mock_store = AsyncMock()
    mock_retriever = AsyncMock()

    old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
    try:
        mod._store = mock_store
        mod._db = mock_db
        mod._retriever = mock_retriever

        with patch("genesis.mcp.memory_mcp.evolution_proposals") as mock_evo:
            mock_evo.create = AsyncMock(return_value="proposal-789")
            tools = await _get_tools()
            result = await tools["evolution_propose"].fn(
                proposal_type="soul_update",
                current_content="old text",
                proposed_change="new text",
                rationale="clarity",
            )
            assert result == "proposal-789"
            mock_evo.create.assert_called_once()
            call_kwargs = mock_evo.create.call_args[1]
            assert call_kwargs["proposal_type"] == "soul_update"
            assert call_kwargs["rationale"] == "clarity"
    finally:
        mod._store = old_store
        mod._db = old_db
        mod._retriever = old_retriever


# ─── conversation_history tool ────────────────────────────────────────────


async def test_conversation_history_registered():
    tools = await _get_tools()
    assert "conversation_history" in tools


async def test_conversation_history_requires_init():
    tools = await _get_tools()
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["conversation_history"].fn(channel="telegram")


async def test_conversation_history_telegram_returns_messages():
    """Verify conversation_history queries telegram_messages table."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    # Use a real in-memory db with the telegram_messages table
    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        # Insert test messages
        from genesis.db.crud.telegram_messages import store
        await store(
            real_db, chat_id=100, message_id=1, sender="user",
            content="hello", timestamp="2026-03-21T10:00:00",
        )
        await store(
            real_db, chat_id=100, message_id=-1, sender="genesis",
            content="hi there", timestamp="2026-03-21T10:00:01",
        )

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store = MagicMock()
            mod._db = real_db
            mod._retriever = MagicMock()

            tools = await _get_tools()
            result = await tools["conversation_history"].fn(
                channel="telegram", limit=10,
            )
            assert len(result) == 2
            assert result[0]["sender"] == "user"
            assert result[1]["sender"] == "genesis"
        finally:
            mod._store = old_store
            mod._db = old_db
            mod._retriever = old_retriever


async def test_conversation_history_search():
    """Verify conversation_history search filters correctly."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        from genesis.db.crud.telegram_messages import store
        await store(
            real_db, chat_id=100, message_id=1, sender="user",
            content="deploy the app", timestamp="2026-03-21T10:00:00",
        )
        await store(
            real_db, chat_id=100, message_id=2, sender="user",
            content="check the logs", timestamp="2026-03-21T10:00:01",
        )

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store = MagicMock()
            mod._db = real_db
            mod._retriever = MagicMock()

            tools = await _get_tools()
            result = await tools["conversation_history"].fn(
                channel="telegram", search="deploy",
            )
            assert len(result) == 1
            assert "deploy" in result[0]["content"]
        finally:
            mod._store = old_store
            mod._db = old_db
            mod._retriever = old_retriever


async def test_conversation_history_unknown_channel():
    """Unknown channel returns empty list."""
    import genesis.mcp.memory_mcp as mod

    old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = MagicMock()

        tools = await _get_tools()
        result = await tools["conversation_history"].fn(channel="slack")
        assert result == []
    finally:
        mod._store = old_store
        mod._db = old_db
        mod._retriever = old_retriever


# ─── expand_query_terms pass-through tests ───────────────────────────────────


def _make_retrieval_result(mid="a", content="test", score=0.9):
    """Build a minimal RetrievalResult for mocking."""
    from genesis.memory.types import RetrievalResult

    return RetrievalResult(
        memory_id=mid,
        content=content,
        source="test",
        memory_type="episodic",
        score=score,
        vector_rank=1,
        fts_rank=1,
        activation_score=0.5,
        payload={"wing": "", "room": ""},
        memory_class="fact",
    )


async def test_memory_recall_passes_expand_query_terms_true():
    """When expand_query_terms=True, it reaches retriever.recall()."""
    import genesis.mcp.memory_mcp as mod

    mock_retriever = AsyncMock()
    mock_retriever.recall.return_value = [_make_retrieval_result()]

    old_store, old_db, old_retriever, old_qdrant = (
        mod._store, mod._db, mod._retriever, mod._qdrant,
    )
# ─── drift_recall fallback tests ────────────────────────────────────────────


def _drift_patch():
    """Import the drift module and return a patch target for drift_recall."""
    import genesis.memory.drift as drift_mod
    return drift_mod


async def test_memory_recall_drift_fallback_fires_on_sparse_results():
    """When standard recall returns < 3 results, drift_recall is tried."""
    import genesis.mcp.memory_mcp as mod
    from genesis.memory.types import RetrievalResult

    def _make_result(mid: str, pipeline: str = "hybrid") -> RetrievalResult:
        return RetrievalResult(
            memory_id=mid, content=f"content-{mid}", source="test",
            memory_type="episodic", score=0.5, vector_rank=1, fts_rank=1,
            activation_score=0.3, payload={}, source_pipeline=pipeline,
        )

    sparse_results = [_make_result("a")]
    drift_results = [_make_result("x", "drift"), _make_result("y", "drift"),
                     _make_result("z", "drift")]

    mock_retriever = AsyncMock()
    mock_retriever.recall = AsyncMock(return_value=sparse_results)
    mock_retriever._embeddings = MagicMock()

    drift_mod = _drift_patch()
    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()

        tools = await _get_tools()
        await tools["memory_recall"].fn(
            query="configure routing",
            expand_query_terms=True,
            include_graph=False,
        )

        mock_retriever.recall.assert_called_once()
        call_kwargs = mock_retriever.recall.call_args[1]
        assert call_kwargs["expand_query_terms"] is True
    finally:
        mod._store = old_store
        mod._db = old_db
        mod._retriever = old_retriever
        mod._qdrant = old_qdrant


async def test_memory_recall_expand_query_terms_defaults_false():
    """By default, expand_query_terms is False (no expansion)."""
    import genesis.mcp.memory_mcp as mod

    mock_retriever = AsyncMock()
    mock_retriever.recall.return_value = [
        _make_retrieval_result(),
        _make_retrieval_result(mid="b"),
        _make_retrieval_result(mid="c"),
    ]

    old_store, old_db, old_retriever, old_qdrant = (
        mod._store, mod._db, mod._retriever, mod._qdrant,
    )
        with patch.object(drift_mod, "drift_recall",
                          new_callable=AsyncMock, return_value=drift_results):
            tools = await _get_tools()
            results = await tools["memory_recall"].fn(
                query="test query", limit=10, compact=True,
            )
            # Should get drift results (3) instead of sparse (1)
            assert len(results) == 3
            assert all(r["source_pipeline"] == "drift" for r in results)
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_memory_recall_no_drift_when_results_sufficient():
    """When standard recall returns >= 3 results, drift is NOT called."""
    import genesis.mcp.memory_mcp as mod
    from genesis.memory.types import RetrievalResult

    def _make_result(mid: str) -> RetrievalResult:
        return RetrievalResult(
            memory_id=mid, content=f"content-{mid}", source="test",
            memory_type="episodic", score=0.5, vector_rank=1, fts_rank=1,
            activation_score=0.3, payload={}, source_pipeline="hybrid",
        )

    good_results = [_make_result("a"), _make_result("b"), _make_result("c")]

    mock_retriever = AsyncMock()
    mock_retriever.recall = AsyncMock(return_value=good_results)

    drift_mod = _drift_patch()
    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()

        tools = await _get_tools()
        await tools["memory_recall"].fn(
            query="test query",
            include_graph=False,
        )

        call_kwargs = mock_retriever.recall.call_args[1]
        assert call_kwargs["expand_query_terms"] is False
    finally:
        mod._store = old_store
        mod._db = old_db
        mod._retriever = old_retriever
        mod._qdrant = old_qdrant
        with patch.object(drift_mod, "drift_recall",
                          new_callable=AsyncMock) as mock_drift:
            tools = await _get_tools()
            results = await tools["memory_recall"].fn(
                query="test query", limit=10, compact=True,
            )
            assert len(results) == 3
            mock_drift.assert_not_called()
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_memory_recall_drift_fallback_failure_is_silent():
    """If drift_recall raises, original sparse results are returned."""
    import genesis.mcp.memory_mcp as mod
    from genesis.memory.types import RetrievalResult

    sparse = [RetrievalResult(
        memory_id="a", content="c", source="t", memory_type="episodic",
        score=0.5, vector_rank=1, fts_rank=1, activation_score=0.3,
        payload={}, source_pipeline="hybrid",
    )]

    mock_retriever = AsyncMock()
    mock_retriever.recall = AsyncMock(return_value=sparse)
    mock_retriever._embeddings = MagicMock()

    drift_mod = _drift_patch()
    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()

        with patch.object(drift_mod, "drift_recall",
                          new_callable=AsyncMock,
                          side_effect=RuntimeError("embedding provider down")):
            tools = await _get_tools()
            results = await tools["memory_recall"].fn(
                query="test query", limit=10, compact=True,
            )
            # Falls back to original sparse results
            assert len(results) == 1
            assert results[0]["memory_id"] == "a"
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_memory_recall_no_drift_when_limit_below_3():
    """Drift should not fire when limit < 3 (caller only wants 1-2 results)."""
    import genesis.mcp.memory_mcp as mod
    from genesis.memory.types import RetrievalResult

    sparse = [RetrievalResult(
        memory_id="a", content="c", source="t", memory_type="episodic",
        score=0.5, vector_rank=1, fts_rank=1, activation_score=0.3,
        payload={}, source_pipeline="hybrid",
    )]

    mock_retriever = AsyncMock()
    mock_retriever.recall = AsyncMock(return_value=sparse)

    drift_mod = _drift_patch()
    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()

        with patch.object(drift_mod, "drift_recall",
                          new_callable=AsyncMock) as mock_drift:
            tools = await _get_tools()
            results = await tools["memory_recall"].fn(
                query="test", limit=2, compact=True,
            )
            assert len(results) == 1
            mock_drift.assert_not_called()
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_memory_recall_no_drift_when_wing_specified():
    """Drift should not fire when wing filter is set — drift ignores wing/room."""
    import genesis.mcp.memory_mcp as mod
    from genesis.memory.types import RetrievalResult

    sparse = [RetrievalResult(
        memory_id="a", content="c", source="t", memory_type="episodic",
        score=0.5, vector_rank=1, fts_rank=1, activation_score=0.3,
        payload={"wing": "infrastructure"}, source_pipeline="hybrid",
    )]

    mock_retriever = AsyncMock()
    mock_retriever.recall = AsyncMock(return_value=sparse)

    drift_mod = _drift_patch()
    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()

        with patch.object(drift_mod, "drift_recall",
                          new_callable=AsyncMock) as mock_drift:
            tools = await _get_tools()
            results = await tools["memory_recall"].fn(
                query="test", limit=10, wing="infrastructure", compact=True,
            )
            assert len(results) == 1
            mock_drift.assert_not_called()
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old
