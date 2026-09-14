"""Tests for memory-mcp server — verify all tools are registered with correct signatures."""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.mcp.memory_mcp import mcp


async def _get_tools():
    return await mcp.get_tools()


async def test_all_tools_registered():
    tools = await _get_tools()
    expected = [
        "memory_recall", "memory_store", "memory_extract", "memory_proactive",
        "memory_synthesize",
        "memory_core_facts", "memory_stats",
        "observation_write", "observation_query", "observation_resolve",
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


async def test_procedure_store_registered():
    tools = await _get_tools()
    assert "procedure_store" in tools


async def test_procedure_recall_registered():
    tools = await _get_tools()
    assert "procedure_recall" in tools


async def test_procedure_store_recall_roundtrip():
    """End-to-end regression test for the procedure_store→procedure_recall bug.

    Pre-fix: procedure_store wrote draft=1, success_count=0,
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
                "SELECT draft, success_count, confidence, activation_tier "
                "FROM procedural_memory WHERE id = ?",
                (pid,),
            )
            row = await cursor.fetchone()
            assert row[0] == 0  # draft
            assert row[1] == 1  # success_count
            assert abs(row[2] - 2 / 3) < 1e-9  # Laplace
            assert row[3] == "LIBRARY"  # activation_tier

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


async def test_procedure_recall_counts_reads():
    """Recalling a procedure (surfacing it to the model) bumps invocation_count
    — the top-of-funnel usage signal that was previously never recorded."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store, mod._db, mod._retriever = MagicMock(), real_db, MagicMock()
            tools = await _get_tools()
            pid = await tools["procedure_store"].fn(
                task_type="discourse-forum-registration",
                principle="Browser is required; the raw API returns fake success.",
                steps=["navigate", "fill", "submit", "verify"],
                tools_used=["browser_navigate"],
                context_tags=["discourse", "forum", "registration", "browser"],
            )

            async def invocations() -> int:
                cur = await real_db.execute(
                    "SELECT invocation_count FROM procedural_memory WHERE id = ?", (pid,))
                return (await cur.fetchone())[0]

            assert await invocations() == 0
            await tools["procedure_recall"].fn(
                task_description="register on discourse forum",
                context_tags=["discourse", "forum", "registration"],
            )
            assert await invocations() >= 1   # read counted
        finally:
            mod._store, mod._db, mod._retriever = old_store, old_db, old_retriever


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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
                identifier="HobbyForum forum login",
                value="ForumUser42 / hunter2",
                description=(
                    "Login for forum.example-community.org, used by the "
                    "ForumUser42 persona. hobby community forum."
                ),
                tags=["forum", "persona:example"],
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
            assert row["concept"] == "HobbyForum forum login"
            assert "ForumUser42 / hunter2" in row["body"]
            assert "hobby community forum" in row["body"]
            assert "forum" in row["tags"]
            assert "persona:example" in row["tags"]
            assert "reference" in row["tags"]
            assert "credentials" in row["tags"]
            assert row["qdrant_id"] == "qdrant-cred-1"

            # Verify memory_class="fact" was forced to avoid 0.7x penalty
            store_kwargs = mock_store.store.call_args[1]
            assert store_kwargs["memory_class"] == "fact"

            # Verify reference routes to episodic_memory, not knowledge_base
            assert store_kwargs["collection"] == "episodic_memory"
            assert store_kwargs["memory_type"] == "episodic"
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
                value="10.0.0.101",
                description="Incus container running Genesis runtime (rotated)",
            )
            assert uid_b == uid_a  # stable on conflict

            row = await mod.knowledge.get(real_db, uid_a)
            assert "10.0.0.101" in row["body"]
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
    """Regression: a vector hit must hydrate by Qdrant point ID, not primary key.

    Builds a reference entry via reference_store, then simulates a vector hit
    that returns it (no FTS match for the semantic query), and verifies the
    result surfaces. The retriever returns ``memory_id`` = the Qdrant point ID
    (what ``store.store`` returns, stored as ``knowledge_units.qdrant_id``) —
    NOT the primary key. Hydrating that via ``knowledge.get`` (primary-key
    lookup) matched nothing and dropped every reference; this asserts the
    qdrant_id-keyed hydration path returns the entry.
    """
    from types import SimpleNamespace

    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-vector-1")
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
                identifier="ForumUser42 persona",
                value="~/.claude/personas/example/persona.md",
                description=(
                    "hobby community persona for low-key forum engagement"
                ),
            )

            # Anti-remasking guard: the Qdrant point ID and the primary key are
            # different UUIDs. The prior test mocked memory_id=unit_id, which
            # masked the bug. Production returns the Qdrant point ID.
            assert unit_id != "q-vector-1"

            # Retriever returns the Qdrant point ID (== store.store's return,
            # stored as qdrant_id), NOT the primary key.
            mock_retriever.recall = AsyncMock(return_value=[
                SimpleNamespace(memory_id="q-vector-1", score=0.87),
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
    """Vector and FTS hits for the same entry merge to origin='both'.

    Genuinely exercises cross-id-space dedup: the vector hit carries the Qdrant
    point ID while FTS carries the primary key, so the merge must resolve the
    vector hit to its primary key to recognise them as the same entry.
    """
    from types import SimpleNamespace

    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-both")
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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

            # Vector hit carries the Qdrant point ID (store.store's return),
            # FTS carries the primary key — dedup must still merge them.
            assert uid != "q-both"
            mock_retriever.recall = AsyncMock(return_value=[
                SimpleNamespace(memory_id="q-both", score=0.9),
            ])

            results = await tools["reference_lookup"].fn(
                query="example forum", kind="url",
            )
            assert len(results) == 1
            assert results[0]["unit_id"] == uid
            assert results[0]["origin"] == "both"
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_lookup_vector_hit_without_knowledge_unit_skipped():
    """A vector hit with no knowledge_units row is excluded, not an error.

    The episodic collection holds plain memories alongside stored references;
    a vector hit whose Qdrant point ID has no knowledge_units row is not a
    reference and must be dropped cleanly (no exception, no bogus row).
    """
    from types import SimpleNamespace

    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-orphan")
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "m"

        mock_retriever = AsyncMock()
        # Qdrant point ID with no matching knowledge_units.qdrant_id row.
        mock_retriever.recall = AsyncMock(return_value=[
            SimpleNamespace(memory_id="not-a-knowledge-unit", score=0.95),
        ])

        old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        try:
            mod._store = mock_store
            mod._db = real_db
            mod._retriever = mock_retriever
            mod._qdrant = MagicMock()

            tools = await _get_tools()
            results = await tools["reference_lookup"].fn(
                query="xyzzy-no-match-in-body",  # FTS returns nothing either
            )
            assert results == []
        finally:
            mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_reference_lookup_hydration_error_degrades_to_fts():
    """A vector-hydration DB error degrades to FTS-only, never a total failure.

    reference_lookup's vector-retriever and FTS paths are both fail-open; the
    qdrant_id hydration step must be too, so a transient DB fault on that one
    query can't sink an otherwise-successful lookup.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as real_db:
        await create_all_tables(real_db)

        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-degrade")
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
                identifier="degrade forum",
                value="https://example.com/degrade",
                description="Public example forum for degrade testing",
            )

            # Vector hydration raises; FTS still matches "degrade forum".
            mock_retriever.recall = AsyncMock(return_value=[
                SimpleNamespace(memory_id="q-degrade", score=0.9),
            ])
            with patch.object(
                mod.knowledge, "get_by_qdrant_ids",
                new_callable=AsyncMock, side_effect=RuntimeError("db locked"),
            ):
                results = await tools["reference_lookup"].fn(
                    query="degrade forum", kind="url",
                )
            # FTS path still surfaces the entry — degraded, not empty, no raise.
            assert len(results) == 1
            assert results[0]["unit_id"] == uid
            assert results[0]["origin"] == "fts"
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})

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
        mock_store.delete = AsyncMock(return_value={"metadata": 1, "fts5": 1})
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


async def test_conversation_history_cc_before_filters_older(tmp_path, monkeypatch):
    """The CC 'before' cursor must exclude records at/after the timestamp so
    scroll-up pages further back. Previously the CC branch ignored 'before' and
    returned the tail regardless — the advertised pagination silently no-oped
    for CC. A record with no timestamp is excluded once 'before' is set (can't
    prove it precedes the cursor)."""
    import json as _json

    import genesis.mcp.memory_mcp as mod
    from genesis.mcp.memory import conversation as conv

    proj = "proj-x"
    proj_dir = tmp_path / ".claude" / "projects" / proj
    proj_dir.mkdir(parents=True)
    # Written OUT of chronological order — the tool must sort globally by
    # timestamp before applying the cursor/limit (mirrors the real two-file
    # concatenation scramble).
    records = [
        {"type": "assistant", "message": "middle", "timestamp": "2026-08-20T10:05:00Z"},
        {"type": "user", "message": "oldest", "timestamp": "2026-08-20T10:00:00Z"},
        {"type": "user", "message": "newest", "timestamp": "2026-08-20T10:10:00Z"},
        {"type": "user", "message": "notime"},  # missing timestamp
    ]
    (proj_dir / "s.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in records) + "\n",
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conv, "cc_project_dir", lambda: proj)

    old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
    try:
        mod._store = MagicMock()
        mod._db = MagicMock()
        mod._retriever = MagicMock()

        tools = await _get_tools()
        result = await tools["conversation_history"].fn(
            channel="cc", limit=10, before="2026-08-20T10:10:00Z",
        )
        contents = [m["content"] for m in result]
        # at/after the cursor (and undated) excluded; older kept.
        assert "newest" not in contents
        assert "notime" not in contents
        assert "oldest" in contents
        assert "middle" in contents
        # Returned chronologically (oldest→newest), regardless of file order.
        assert contents == ["oldest", "middle"]
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
        mod._store, mod._db, mod._retriever, mod._qdrant = old


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


async def test_memory_recall_defaults_to_both():
    """memory_recall with no explicit source should pass source='both' to retriever."""
    import genesis.mcp.memory_mcp as mod

    old = mod._store, mod._db, mod._retriever, mod._qdrant
    mod._store = MagicMock()
    mod._db = AsyncMock()
    mod._retriever = AsyncMock()
    mod._retriever.recall = AsyncMock(return_value=[])
    mod._qdrant = MagicMock()

    try:
        tools = await _get_tools()
        await tools["memory_recall"].fn(query="what is my Salesforce experience")

        # Verify retriever.recall was called with source="both" (searches both collections)
        mod._retriever.recall.assert_called_once()
        call_kwargs = mod._retriever.recall.call_args.kwargs
        assert call_kwargs.get("source") == "both", (
            f"Expected source='both', got source={call_kwargs.get('source')!r}"
        )
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_memory_recall_explicit_both_still_works():
    """Passing source='both' explicitly should override the episodic default."""
    import genesis.mcp.memory_mcp as mod

    old = mod._store, mod._db, mod._retriever, mod._qdrant
    mod._store = MagicMock()
    mod._db = AsyncMock()
    mod._retriever = AsyncMock()
    mod._retriever.recall = AsyncMock(return_value=[])
    mod._qdrant = MagicMock()

    try:
        tools = await _get_tools()
        await tools["memory_recall"].fn(query="test", source="both")

        mod._retriever.recall.assert_called_once()
        call_kwargs = mod._retriever.recall.call_args.kwargs
        assert call_kwargs.get("source") == "both"
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


# ── MEM-003: exactly one recall_fired per logical recall ────────────────────


def _ri_result(mid: str):
    from genesis.memory.types import RetrievalResult

    return RetrievalResult(
        memory_id=mid, content=f"c-{mid}", source="test", memory_type="episodic",
        score=0.5, vector_rank=1, fts_rank=1, activation_score=0.3, payload={},
        source_pipeline="hybrid", collection="episodic_memory",
    )


async def test_memory_recall_standard_emits_exactly_one_recall_fired(db):
    """MEM-003: the standard path emits ONE recall_fired (the retriever's),
    enriched in place by the MCP layer with mode/pipeline_used — not a 2nd."""
    import genesis.mcp.memory_mcp as mod
    from genesis.db.crud import j9_eval
    from genesis.eval.j9_hooks import emit_recall_fired

    results = [_ri_result("a"), _ri_result("b"), _ri_result("c")]

    async def fake_recall(*_a, event_id_sink=None, **_k):
        # Mirror the real retriever: emit the inner event + populate the sink.
        eid = await emit_recall_fired(
            db, query="q", result_count=len(results),
            top_scores=[r.score for r in results],
            memory_ids=[r.memory_id for r in results],
            latency_ms=1.0, source="both",
        )
        if event_id_sink is not None and eid is not None:
            event_id_sink.append(eid)
        return list(results)

    mock_retriever = AsyncMock()
    mock_retriever.recall = fake_recall
    mock_retriever._embeddings = MagicMock()

    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = db
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()
        tools = await _get_tools()
        await tools["memory_recall"].fn(
            query="q", source="both", limit=10, compact=True,
        )
        events = await j9_eval.get_events(db, event_type="recall_fired")
        assert len(events) == 1  # ONE event, enriched in place
        m = events[0]["metrics"]
        assert m["pipeline_used"] == "standard"  # MCP-layer attribution merged
        assert m["mode"] == "auto"
        assert m["result_count"] == 3
        # Realigned to the final returned set (the inner emit sent no mean_score).
        assert m["mean_score"] == pytest.approx(0.5)
        assert "latency_ms" in m
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


async def test_memory_recall_drift_mode_emits_exactly_one_recall_fired(db):
    """MEM-003: drift mode calls drift_recall (no inner emit) → the MCP layer
    emits exactly one fresh recall_fired (empty sink → INSERT)."""
    import genesis.mcp.memory_mcp as mod
    from genesis.db.crud import j9_eval

    drift_results = [_ri_result("x"), _ri_result("y")]

    mock_retriever = AsyncMock()
    mock_retriever._embeddings = MagicMock()

    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = db
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()
        with patch.object(_drift_patch(), "drift_recall",
                          new_callable=AsyncMock, return_value=drift_results):
            tools = await _get_tools()
            await tools["memory_recall"].fn(
                query="q", source="both", mode="drift", limit=10, compact=True,
            )
        events = await j9_eval.get_events(db, event_type="recall_fired")
        assert len(events) == 1  # exactly one, freshly inserted
        assert events[0]["metrics"]["pipeline_used"] == "drift"
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


@pytest.mark.asyncio
async def test_memory_recall_mcp_enrichment_uses_raw_scores(db):
    """mem-007 (MCP layer): the MEM-003 in-place enrichment realigns the
    retriever's recall_fired event with the final result set — it must
    re-derive top_scores/mean_score from ``retrieval_score`` (pre-diversity-
    penalty), NOT ``score``, or it clobbers the retriever's raw values with
    the halved dedup artifact."""
    import genesis.mcp.memory_mcp as mod
    from genesis.db.crud import j9_eval
    from genesis.eval.j9_hooks import emit_recall_fired
    from genesis.memory.types import RetrievalResult

    def _penalized(mid: str, score: float, raw: float):
        return RetrievalResult(
            memory_id=mid, content=f"c-{mid}", source="test",
            memory_type="episodic", score=score, vector_rank=1, fts_rank=1,
            activation_score=0.3, payload={}, source_pipeline="hybrid",
            collection="episodic_memory", retrieval_score=raw,
        )

    # winner unpenalized (raw == final); echo halved (raw 1.0 → final 0.5)
    results = [_penalized("win", 1.0, 1.0), _penalized("echo", 0.5, 1.0)]

    async def fake_recall(*_a, event_id_sink=None, **_k):
        eid = await emit_recall_fired(
            db, query="q", result_count=len(results),
            top_scores=[r.retrieval_score for r in results],
            memory_ids=[r.memory_id for r in results],
            latency_ms=1.0, source="both",
        )
        if event_id_sink is not None and eid is not None:
            event_id_sink.append(eid)
        return list(results)

    mock_retriever = AsyncMock()
    mock_retriever.recall = fake_recall
    mock_retriever._embeddings = MagicMock()

    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = db
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()
        tools = await _get_tools()
        await tools["memory_recall"].fn(
            query="q", source="both", limit=10, compact=True,
        )
        events = await j9_eval.get_events(db, event_type="recall_fired")
        assert len(events) == 1
        m = events[0]["metrics"]
        # Raw scores survive the MCP realignment: both 1.0, not [1.0, 0.5]
        assert m["top_scores"] == pytest.approx([1.0, 1.0])
        assert m["mean_score"] == pytest.approx(1.0)
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


def test_apply_authority_boost_propagates_to_retrieval_score():
    """The authority boost is a quality signal and must scale BOTH the
    ordering score and the raw (pre-diversity-penalty) score by the same
    factor — and must not require the raw key to be present."""
    from genesis.mcp.memory.knowledge import _apply_authority_boost

    boosted = _apply_authority_boost([
        {"unit_id": "a", "score": 0.6, "retrieval_score": 0.8,
         "source_pipeline": "curated"},          # 1.5x tier
        {"unit_id": "b", "score": 0.6, "source_pipeline": "curated"},
    ])
    by_id = {r["unit_id"]: r for r in boosted}
    assert by_id["a"]["score"] == pytest.approx(0.9)
    assert by_id["a"]["retrieval_score"] == pytest.approx(1.2)
    assert by_id["b"]["score"] == pytest.approx(0.9)
    assert "retrieval_score" not in by_id["b"]  # guarded, no KeyError


async def test_knowledge_recall_enrichment_uses_raw_scores(db):
    """knowledge_recall's J-9 enrichment must log pre-diversity-penalty
    scores (with the authority boost applied), not the halved dedup
    artifact carried by ``score``."""
    import genesis.mcp.memory_mcp as mod
    from genesis.db.crud import j9_eval
    from genesis.eval.j9_hooks import emit_recall_fired
    from genesis.memory.types import RetrievalResult

    def _kb(mid: str, score: float, raw: float):
        return RetrievalResult(
            memory_id=mid, content=f"c-{mid}", source="test",
            memory_type="knowledge", score=score, vector_rank=1,
            fts_rank=None, activation_score=0.3, payload={},
            source_pipeline="curated",  # 1.5x authority tier
            collection="knowledge_base", retrieval_score=raw,
        )

    # echo penalized for ordering (0.6 -> 0.3) but raw stays 0.6;
    # after the 1.5x curated boost: scores [0.9, 0.45], raws [0.9, 0.9]
    results = [_kb("kb-win", 0.6, 0.6), _kb("kb-echo", 0.3, 0.6)]

    async def fake_recall(*_a, event_id_sink=None, **_k):
        eid = await emit_recall_fired(
            db, query="q", result_count=len(results),
            top_scores=[r.retrieval_score for r in results],
            memory_ids=[r.memory_id for r in results],
            latency_ms=1.0, source="knowledge",
        )
        if event_id_sink is not None and eid is not None:
            event_id_sink.append(eid)
        return list(results)

    mock_retriever = AsyncMock()
    mock_retriever.recall = fake_recall

    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store = MagicMock()
        mod._db = db
        mod._retriever = mock_retriever
        mod._qdrant = MagicMock()
        tools = await _get_tools()
        out = await tools["knowledge_recall"].fn(
            query="q", limit=5, corrective=False,
        )
        # Ordering/return still keyed on the penalized+boosted score
        assert [r["unit_id"] for r in out] == ["kb-win", "kb-echo"]
        events = await j9_eval.get_events(db, event_type="recall_fired")
        assert len(events) == 1  # enriched in place, no second emit
        m = events[0]["metrics"]
        assert m["top_scores"] == pytest.approx([0.9, 0.9]), (
            "J-9 must see boosted RAW scores, not the penalized ordering "
            f"values — got {m['top_scores']}"
        )
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old


@pytest.mark.asyncio
async def test_procedure_store_gate1_emit_honors_session_origin(monkeypatch):
    """WS-3 gate-1 at the explicit-teach path: an external-influenced session
    (GENESIS_SESSION_ORIGIN=external_untrusted) produces ONE shadow would-block
    row; an unset env (foreground/internal session) coalesces to first_party
    and produces NONE (never-block invariant) — never a raw None into the
    gate's fail-closed normalizer."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.db.crud import immunity_shadow as ishadow_crud

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        old_verified = ishadow_crud._table_verified
        try:
            ishadow_crud._table_verified = False
            mod._store = MagicMock()
            mod._db = real_db
            mod._retriever = MagicMock()
            tools = await _get_tools()

            # Unset env → first_party → no row.
            monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
            await tools["procedure_store"].fn(
                task_type="internal-teach",
                principle="A first-party taught procedure.",
                steps=["do the thing"],
                tools_used=["Bash"],
                context_tags=["internal"],
            )
            assert await ishadow_crud.count(real_db) == 0

            # External session env → exactly one gate=procedure row.
            monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "external_untrusted")
            await tools["procedure_store"].fn(
                task_type="external-teach",
                principle="A procedure taught from an external-influenced session.",
                steps=["do the other thing"],
                tools_used=["Bash"],
                context_tags=["external"],
            )
            rows = await ishadow_crud.list_recent(real_db)
            assert len(rows) == 1
            assert rows[0]["gate"] == "procedure"
            assert rows[0]["origin_class"] == "external_untrusted"
            assert rows[0]["source_ref"] == "mcp/memory/procedural.py::procedure_store"
        finally:
            ishadow_crud._table_verified = old_verified
            mod._store, mod._db, mod._retriever = old_store, old_db, old_retriever


@pytest.mark.asyncio
async def test_reference_store_forwards_session_origin(monkeypatch):
    """WS-3: reference_store forwards the dispatched session's env origin into
    the knowledge ingest (previously doubly blind: first-party pipeline AND
    exempt from injection-gate counting)."""
    import genesis.mcp.memory_mcp as mod
    from genesis.memory import knowledge_ingest as ki

    captured: dict = {}

    async def _fake_ingest(**kwargs):
        captured.update(kwargs)
        return "unit-1"

    old = (mod._store, mod._db, mod._qdrant, mod._retriever)
    try:
        mod._store, mod._db, mod._qdrant, mod._retriever = (
            MagicMock(), MagicMock(), MagicMock(), MagicMock(),
        )
        monkeypatch.setattr(ki, "ingest_knowledge_unit", _fake_ingest)
        monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "external_untrusted")
        tools = await _get_tools()
        out = await tools["reference_store"].fn(
            kind="url",
            identifier="example service",
            value="https://example.invalid/x",
            description="synthetic test reference",
        )
        assert out == "unit-1"
        assert captured["origin_class"] == "external_untrusted"
    finally:
        mod._store, mod._db, mod._qdrant, mod._retriever = old


# ─── WS-3 B4: memory_core_facts wraps stored-external items + emits gate 4 ───


async def test_memory_core_facts_wraps_external_and_emits(monkeypatch):
    """core_facts scrolls episodic directly (bypasses HybridRetriever); B4
    gates it: stored-external items are wrapped and shadow-recorded, first
    party items untouched. Shadow-only — nothing is excluded."""
    from types import SimpleNamespace

    import aiosqlite

    import genesis.mcp.memory_mcp as mod
    from genesis.security import immunity_shadow

    def _point(mid, origin_class, content):
        payload = {
            "content": content,
            "source": "test",
            "confidence": 0.9,
            "created_at": "2026-01-01T00:00:00+00:00",
            "retrieved_count": 1,
            "memory_class": "fact",
            "wing": "",
            "room": "",
            "source_pipeline": "conversation",
        }
        if origin_class is not None:
            payload["origin_class"] = origin_class
        return SimpleNamespace(id=mid, payload=payload)

    emit = AsyncMock()
    monkeypatch.setattr(immunity_shadow, "record_would_block", emit)

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        # Scrolled points must have metadata rows — a point without one is a
        # deleted memory's ghost and the core_facts ghost filter drops it.
        for _mid in ("ext-1", "fp-1", "old-1"):
            await real_db.execute(
                "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
                (_mid, "2026-01-01T00:00:00+00:00"),
            )
        await real_db.commit()

        qdrant = MagicMock()
        qdrant.scroll.return_value = (
            [
                _point("ext-1", "external_untrusted", "poisoned external text"),
                _point("fp-1", "first_party", "own first party text"),
                _point("old-1", None, "pre-0054 row with no stored class"),
            ],
            None,
        )
        qdrant.retrieve.return_value = []  # skip the retrieved_count writeback

        old = (mod._store, mod._db, mod._qdrant, mod._retriever)
        try:
            mod._store = MagicMock()
            mod._db = real_db
            mod._qdrant = qdrant
            mod._retriever = MagicMock()

            tools = await _get_tools()
            results = await tools["memory_core_facts"].fn(limit=10)
        finally:
            mod._store, mod._db, mod._qdrant, mod._retriever = old

    by_id = {r["memory_id"]: r for r in results}
    assert "<external-content" in by_id["ext-1"]["content"]
    assert "poisoned external text" in by_id["ext-1"]["content"]
    assert by_id["fp-1"]["content"] == "own first party text"
    # No stored class + episodic collection -> fallback says not blockable.
    assert by_id["old-1"]["content"] == "pre-0054 row with no stored class"
    # Provenance rides the output dicts.
    assert by_id["ext-1"]["origin_class"] == "external_untrusted"

    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["gate"] == "injection"
    assert kwargs["source_ref"] == "mcp/memory/core.py::memory_core_facts"
    assert kwargs["blockable_count"] == 1


async def test_conversation_history_chat_scoped_and_paginated():
    """chat_id scopes the telegram branch to ONE chat (a DM scroll-up must not
    leak other chats); `before` pages further back; the unscoped default is
    unchanged (reflection sessions rely on the cross-chat view)."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        from genesis.db.crud.telegram_messages import store
        for i in range(4):
            await store(
                real_db, chat_id=100, message_id=i, sender="user",
                content=f"dm-{i}", timestamp=f"2026-08-13T04:0{i}:00",
            )
        await store(
            real_db, chat_id=200, message_id=90, sender="genesis",
            content="group-noise", timestamp="2026-08-13T04:02:30",
        )

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store, mod._retriever = MagicMock(), MagicMock()
            mod._db = real_db

            tools = await _get_tools()
            fn = tools["conversation_history"].fn

            scoped = await fn(channel="telegram", chat_id=100, limit=10)
            assert [m["content"] for m in scoped] == [
                "dm-0", "dm-1", "dm-2", "dm-3",
            ]

            paged = await fn(
                channel="telegram", chat_id=100,
                before="2026-08-13T04:02:00", limit=10,
            )
            assert [m["content"] for m in paged] == ["dm-0", "dm-1"]

            unscoped = await fn(channel="telegram", limit=10)
            assert any(m["content"] == "group-noise" for m in unscoped), (
                "default must stay unscoped (cross-chat)"
            )

            sscoped = await fn(channel="telegram", chat_id=100, search="dm-2")
            assert [m["content"] for m in sscoped] == ["dm-2"]
            snoise = await fn(channel="telegram", chat_id=100, search="group-noise")
            assert snoise == []
        finally:
            mod._store, mod._db, mod._retriever = old_store, old_db, old_retriever


async def test_conversation_history_search_honors_scoping_and_cursor():
    """Codex round-3 lock: the search branch must honor thread_id and before —
    otherwise paged search repeats the newest matches forever and a topic
    search leaks other topics."""
    import aiosqlite

    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as real_db:
        real_db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables
        await create_all_tables(real_db)
        await real_db.commit()

        from genesis.db.crud.telegram_messages import store
        for i in range(3):
            await store(
                real_db, chat_id=100, message_id=i, sender="user",
                content=f"needle-{i}", thread_id=7,
                timestamp=f"2026-08-13T04:0{i}:00",
            )
        await store(
            real_db, chat_id=100, message_id=50, sender="user",
            content="needle-other-topic", thread_id=8,
            timestamp="2026-08-13T04:01:30",
        )

        old_store, old_db, old_retriever = mod._store, mod._db, mod._retriever
        try:
            mod._store, mod._retriever = MagicMock(), MagicMock()
            mod._db = real_db
            tools = await _get_tools()
            fn = tools["conversation_history"].fn

            scoped = await fn(
                channel="telegram", chat_id=100, thread_id=7, search="needle",
            )
            assert [m["content"] for m in scoped] == [
                "needle-0", "needle-1", "needle-2",
            ], "topic search must not leak other topics"

            paged = await fn(
                channel="telegram", chat_id=100, thread_id=7, search="needle",
                before="2026-08-13T04:01:00",
            )
            assert [m["content"] for m in paged] == ["needle-0"], (
                "search must honor the before cursor"
            )
        finally:
            mod._store, mod._db, mod._retriever = old_store, old_db, old_retriever


# --- memory_store: `wing` is a controlled vocabulary at the agent boundary ---
# The tool takes a `wing` parameter (it has since 2026-04-14), but nothing
# validated it: any string was accepted and written through to the FTS5 tag,
# the Qdrant payload and memory_metadata.wing. A background session once
# reported that no memory tool exposed `wing` at all and encoded its intent as
# a free-text tag instead — a caller that can be told the valid set should be.


@pytest.mark.asyncio
async def test_memory_store_rejects_unknown_wing():
    from genesis.mcp.memory import core

    tools = await _get_tools()
    with patch.object(core, "_memory_mod") as mod:
        mod.return_value._store = MagicMock()
        mod.return_value._store.store = AsyncMock(return_value="mem-id")
        with pytest.raises(ValueError) as exc:
            await tools["memory_store"].fn("content", "src", wing="portfolio")

    msg = str(exc.value)
    assert "portfolio" in msg
    # The error must TEACH the vocabulary, not merely refuse.
    assert "career" in msg and "infrastructure" in msg, msg
    # And it must refuse BEFORE writing anything.
    mod.return_value._store.store.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_store_accepts_a_valid_wing():
    from genesis.mcp.memory import core

    tools = await _get_tools()
    with patch.object(core, "_memory_mod") as mod:
        mod.return_value._store = MagicMock()
        mod.return_value._store.store = AsyncMock(return_value="mem-id")
        result = await tools["memory_store"].fn("content", "src", wing="career")

    assert result == "mem-id"
    assert mod.return_value._store.store.await_args.kwargs["wing"] == "career"


@pytest.mark.asyncio
async def test_memory_store_without_wing_is_unaffected():
    """Omitting `wing` must still reach the store for auto-classification."""
    from genesis.mcp.memory import core

    tools = await _get_tools()
    with patch.object(core, "_memory_mod") as mod:
        mod.return_value._store = MagicMock()
        mod.return_value._store.store = AsyncMock(return_value="mem-id")
        result = await tools["memory_store"].fn("content", "src")

    assert result == "mem-id"
    assert mod.return_value._store.store.await_args.kwargs["wing"] is None


@pytest.mark.asyncio
async def test_memory_synthesize_rejects_unknown_wing():
    """The SECOND agent-facing door into the same boundary.

    Provenance on the 18 out-of-vocabulary rows in the live store: 17 have
    dream_cycle_run_id IS NULL, i.e. they came through the MCP boundary rather
    than the LLM-internal synthesis path. Guarding memory_store while leaving
    memory_synthesize open just moves the door.
    """
    from genesis.mcp.memory import core

    tools = await _get_tools()
    with patch.object(core, "_memory_mod") as mod:
        mod.return_value._store = MagicMock()
        mod.return_value._store.store = AsyncMock(return_value="mem-id")
        with pytest.raises(ValueError) as exc:
            await tools["memory_synthesize"].fn("content", wing="portfolio")

    assert "portfolio" in str(exc.value)
    assert "career" in str(exc.value), str(exc.value)
    mod.return_value._store.store.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_synthesize_accepts_a_valid_wing():
    from genesis.mcp.memory import core

    tools = await _get_tools()
    with patch.object(core, "_memory_mod") as mod:
        mod.return_value._store = MagicMock()
        mod.return_value._store.store = AsyncMock(return_value="mem-id")
        result = await tools["memory_synthesize"].fn("content", wing="memory")

    assert result == "mem-id"


# --- memory_recall: the THIRD door, and the only one that READS ---
# Enumerated rather than spot-checked: exactly three MCP tools take a `wing`
# (memory_recall, memory_store, memory_synthesize). The two writers above
# validate; the reader did not. Qdrant applies wing as a hard `must` condition
# and the FTS5 path post-filters on it, so a typo returns [] — which an agent
# reads as "no such memories exist" rather than "no such wing".


@pytest.mark.asyncio
async def test_memory_recall_reports_an_unknown_wing_instead_of_returning_empty():
    """A bad wing must be DISTINGUISHABLE from an honest empty result."""
    from genesis.mcp.memory import core

    tools = await _get_tools()
    with patch.object(core, "_memory_mod") as mod:
        result = await tools["memory_recall"].fn(query="anything", wing="architecture")

    assert len(result) == 1 and "error" in result[0], result
    msg = result[0]["error"]
    assert "architecture" in msg
    # Must TEACH the vocabulary, exactly as the write path does.
    assert "career" in msg and "infrastructure" in msg, msg
    # And it must refuse BEFORE the expensive search, not filter afterwards.
    mod.assert_not_called()


def test_memory_recall_docstring_lists_exactly_the_wing_vocabulary():
    """The docstring IS the agent-facing schema, so it must not rot.

    This PR's thesis is "stop hand-copying a closed vocabulary" — and its own
    fix to `memory_recall`'s docstring introduced a fresh hand-copied copy, in
    the one place with the highest blast radius: the MCP tool schema every
    agent reads to decide what to pass. The prompt copy 200 lines away is
    derived AND locked; this one was neither.

    Deriving it is not available here (the docstring is the schema, extracted
    statically), so it gets the lock instead. `WINGS` has changed membership
    twice in this repo's history, so "correct today" is not a guarantee.
    """
    import re

    from genesis.mcp.memory import core
    from genesis.memory.taxonomy import WINGS

    doc = core.memory_recall.fn.__doc__
    m = re.search(r"one of: (.*?)\. Enumerated", doc, re.S)
    assert m, "the `wing` docstring entry no longer enumerates the vocabulary"
    listed = {w.strip() for w in m.group(1).replace("\n", " ").split(",")}
    assert listed == WINGS, f"docstring drifted from WINGS: {listed ^ WINGS}"


@pytest.mark.asyncio
async def test_memory_recall_forwards_the_wing_it_validated_not_the_raw_one():
    """A padded wing must not pass validation and then match zero rows.

    `_validate_wing` tests `wing.strip()`, but Qdrant applies `wing` as a hard
    `must` FieldCondition and the FTS5 path compares it verbatim. Forwarding the
    RAW value would let `" memory "` clear the guard and still return [] — the
    exact silent-empty-result this guard exists to eliminate, surviving on a
    whitespace edge. Blank means "no filter", not "filter on empty".
    """
    from genesis.mcp.memory import core

    tools = await _get_tools()
    seen = {}

    with patch.object(core, "_memory_mod") as mod:

        async def _capture(*args, **kwargs):
            seen["wing"] = kwargs.get("wing")
            raise RuntimeError("stop after the filter is decided")

        mod.return_value._retriever.recall = _capture
        with contextlib.suppress(RuntimeError):
            await tools["memory_recall"].fn(query="q", wing="  memory  ")
    assert seen.get("wing") == "memory", seen

    seen.clear()
    with patch.object(core, "_memory_mod") as mod:

        async def _capture2(*args, **kwargs):
            seen["wing"] = kwargs.get("wing")
            raise RuntimeError("stop after the filter is decided")

        mod.return_value._retriever.recall = _capture2
        with contextlib.suppress(RuntimeError):
            await tools["memory_recall"].fn(query="q", wing="   ")
    assert seen.get("wing") is None, seen


@pytest.mark.asyncio
async def test_memory_recall_wing_validation_shares_one_definition_with_the_writers():
    """No second vocabulary. Every WINGS member is accepted by the reader.

    A per-tool copy of the list is the defect this whole PR is about — the
    WING_AUDIT prompt carried one and put `architecture` into 11 live rows.

    Scope, stated because this test's green is narrower than its name: it
    catches a NARROWED vocabulary (verified RED by validating against
    ``WINGS - {"employment"}``), which is the copy-the-list defect. It does NOT
    catch validation being deleted outright — with no check at all every wing
    is trivially "accepted" and this still passes. The sibling test above is
    the deletion guard; verify both when either changes.
    """
    from genesis.mcp.memory import core
    from genesis.memory.taxonomy import WINGS

    tools = await _get_tools()
    for wing in sorted(WINGS):
        with patch.object(core, "_memory_mod") as mod:
            mod.side_effect = RuntimeError("reached the search path")
            with pytest.raises(RuntimeError):
                await tools["memory_recall"].fn(query="q", wing=wing)
