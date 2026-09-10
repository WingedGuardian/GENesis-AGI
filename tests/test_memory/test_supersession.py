"""Tests for memory supersession — store with supersedes, mark_superseded, retrieval filtering."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.store import MemoryStore


@pytest.fixture()
def embedding_provider():
    ep = MagicMock()
    ep.embed = AsyncMock(return_value=[0.1] * 1024)
    ep.enrich = MagicMock(return_value="episodic: test content")
    return ep


@pytest.fixture()
def qdrant():
    return MagicMock()


@pytest.fixture()
def db():
    mock = AsyncMock()
    # Default: execute returns a cursor with fetchone returning None
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=None)
    mock.execute = AsyncMock(return_value=cursor)
    return mock


@pytest.fixture()
def linker():
    lnk = MagicMock()
    lnk.auto_link = AsyncMock(return_value=[])
    return lnk


@pytest.fixture()
def store(embedding_provider, qdrant, db, linker):
    return MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
        linker=linker,
    )


@pytest.mark.asyncio()
async def test_store_with_supersedes_marks_old_deprecated(store, db):
    """Storing with supersedes should mark the old memory as deprecated in SQLite."""
    old_id = "old-memory-id"

    # Mock the metadata lookup for the old memory (embedded, episodic_memory)
    metadata_cursor = AsyncMock()
    metadata_cursor.fetchone = AsyncMock(return_value=("episodic_memory", "embedded"))

    # We need to track all execute calls
    call_results = []
    original_execute = db.execute

    async def track_execute(sql, params=None):
        result = await original_execute(sql, params)
        call_results.append((sql, params))
        # Return the metadata cursor for the SELECT query
        if isinstance(sql, str) and "SELECT collection" in sql:
            return metadata_cursor
        return result

    db.execute = AsyncMock(side_effect=track_execute)

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=(old_id, "new"))

        new_id = await store.store(
            "CC upgraded to v2.1.154",
            "conversation",
            supersedes=old_id,
        )

    assert isinstance(new_id, str)
    assert len(new_id) == 36

    # Verify mark_superseded was called with old_id and new_id
    mock_mem.mark_superseded.assert_awaited_once()
    call_args = mock_mem.mark_superseded.call_args
    assert call_args[0][1] == old_id  # old_id
    assert call_args[0][2] == new_id  # new_id


@pytest.mark.asyncio()
async def test_store_with_supersedes_updates_qdrant(store, db):
    """Storing with supersedes should update the old memory's Qdrant payload."""
    old_id = "old-memory-id"

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload") as mock_update, \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=(old_id, "new"))

        new_id = await store.store(
            "new fact",
            "conversation",
            supersedes=old_id,
        )

    # Verify Qdrant update_payload was called
    mock_update.assert_called_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs.kwargs["collection"] == "episodic_memory"
    assert call_kwargs.kwargs["point_id"] == old_id
    assert call_kwargs.kwargs["payload"]["deprecated"] is True
    assert call_kwargs.kwargs["payload"]["merged_into"] == new_id


@pytest.mark.asyncio()
async def test_store_with_supersedes_skips_qdrant_for_fts5_only(store, db):
    """FTS5-only memories should not get a Qdrant update_payload call."""
    old_id = "fts5-only-memory"

    metadata_cursor = AsyncMock()
    metadata_cursor.fetchone = AsyncMock(return_value=("episodic_memory", "fts5_only"))

    async def route_execute(sql, params=None):
        if isinstance(sql, str) and "SELECT collection" in sql:
            return metadata_cursor
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        return cursor

    db.execute = AsyncMock(side_effect=route_execute)

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload") as mock_update, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        # The pre-flight confirms a PASSTHROUGH id names a real row before any
        # write, and _mark_superseded reads the same row for the collection.
        # Without this the supersede raised in pre-flight and this test passed
        # vacuously — update_payload was "not called" because nothing ran.
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "fts5_only", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)

        await store.store("new fact", "conversation", supersedes=old_id)

    # Qdrant update_payload should NOT be called for fts5_only memories
    mock_update.assert_not_called()
    # ...and the supersede must actually have RUN, or the assertion above is
    # vacuous rather than a skip.
    mock_mem.mark_superseded.assert_awaited_once()


@pytest.mark.asyncio()
async def test_store_with_supersedes_creates_succeeded_by_link(store, db):
    """Supersession should create a succeeded_by link in memory_links."""
    old_id = "old-memory-id"

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=(old_id, "new"))

        new_id = await store.store("new fact", "conversation", supersedes=old_id)

    mock_links.create.assert_awaited_once()
    link_kwargs = mock_links.create.call_args.kwargs
    assert link_kwargs["source_id"] == old_id
    assert link_kwargs["target_id"] == new_id
    assert link_kwargs["link_type"] == "succeeded_by"
    assert link_kwargs["strength"] == 1.0


@pytest.mark.asyncio()
async def test_store_without_supersedes_skips_deprecation(store, db):
    """Normal store (no supersedes) should not trigger any deprecation logic."""
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload") as mock_update, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)

        await store.store("normal content", "conversation")

    mock_update.assert_not_called()


@pytest.mark.asyncio()
async def test_an_infrastructure_failure_during_supersede_does_not_block_the_store(
    store, db
):
    """A deprecation that fails AFTER pre-flight must not lose the new memory.

    The target was resolved and confirmed to exist before any write, so a
    failure here is an infrastructure fault (a locked DB, a dead connection),
    not a bad handle. The content is already durable at that point, and turning
    it into a raised error reads as "the store failed" and invites a retry that
    duplicates the memory.
    """
    old_id = "nonexistent-id"

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        # Pre-flight PASSES — the row exists...
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "fts5_only", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        # ...and the deprecation itself then fails on infrastructure.
        mock_mem.mark_superseded = AsyncMock(
            side_effect=RuntimeError("simulated DB error")
        )
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)

        result = await store.store(
            "new content",
            "conversation",
            supersedes=old_id,
        )

    # Store should succeed despite supersession failure
    assert isinstance(result, str)
    assert len(result) == 36
    mock_mem.mark_superseded.assert_awaited_once(), "the failure must be the real one"


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("handle", "resolver_verdict"),
    [
        ("deadbeef", ([], "not_found")),
        (
            "deadbeef-0000-4000-8000-000000000009",
            (["deadbeef-0000-4000-8000-000000000009"], "passthrough"),
        ),
    ],
    ids=["prefix-matches-nothing", "full-id-names-nothing"],
)
async def test_an_unresolvable_target_is_rejected_before_anything_is_written(
    store, db, handle, resolver_verdict
):
    """The hoist, locked: pre-flight runs BEFORE the first write.

    Two things depend on this ordering, and neither is provable from
    `_mark_superseded` alone:

    * Nothing durable exists when the rejection happens, so it can be a plain
      raise instead of a half-completed operation the caller has to reason
      about.
    * Resolution cannot see the row this very call is about to write. When it
      ran afterwards, a short prefix could match the NEW memory and deprecate
      it as its own successor.

    Both resolver verdicts are covered. They fail in DIFFERENT places — a
    prefix is rejected by the resolver, while a full-length id is waved through
    as PASSTHROUGH and is only known to be bad once its row is looked up — so
    one case passing says nothing about the other.
    """
    from genesis.memory.store import SupersedeUnresolved

    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.resolve_id = AsyncMock(return_value=resolver_verdict)
        # A full-length id passes resolution untouched; only this lookup can
        # tell that it names nothing.
        mock_mem.get_metadata = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)

        with pytest.raises(SupersedeUnresolved) as exc:
            await store.store("a correction", "conversation", supersedes=handle)

    assert exc.value.reason == "not_found"
    mock_mem.create_metadata.assert_not_awaited(), "wrote the memory before checking"
    mock_upsert.assert_not_called()
    mock_mem.mark_superseded.assert_not_awaited()


@pytest.mark.asyncio()
async def test_search_ranked_excludes_deprecated_by_default():
    """search_ranked should exclude deprecated memories by default."""
    import aiosqlite

    db = AsyncMock(spec=aiosqlite.Connection)
    db.execute_fetchall = AsyncMock(return_value=[])

    from genesis.db.crud.memory import search_ranked
    await search_ranked(db, query="test query")

    # Verify the SQL includes the deprecated filter
    sql = db.execute_fetchall.call_args[0][0]
    assert "deprecated" in sql
    assert "deprecated = 0" in sql or "deprecated IS NULL" in sql


@pytest.mark.asyncio()
async def test_search_ranked_includes_deprecated_when_requested():
    """search_ranked with include_deprecated=True should not filter deprecated."""
    import aiosqlite

    db = AsyncMock(spec=aiosqlite.Connection)
    db.execute_fetchall = AsyncMock(return_value=[])

    from genesis.db.crud.memory import search_ranked
    await search_ranked(db, query="test query", include_deprecated=True)

    # Verify the SQL does NOT include the deprecated filter
    sql = db.execute_fetchall.call_args[0][0]
    assert "deprecated = 0" not in sql


@pytest.mark.asyncio()
async def test_qdrant_search_excludes_deprecated_by_default():
    """Qdrant search() should include deprecated must_not filter by default."""
    from unittest.mock import MagicMock

    from genesis.qdrant.collections import search

    client = MagicMock()
    client.query_points = MagicMock(return_value=MagicMock(points=[]))

    search(
        client,
        collection="episodic_memory",
        query_vector=[0.1] * 1024,
        limit=5,
    )

    # Verify the filter includes deprecated must_not
    call_kwargs = client.query_points.call_args.kwargs
    query_filter = call_kwargs["query_filter"]
    must_not = query_filter.must_not
    assert must_not is not None
    assert any(
        getattr(cond, "key", None) == "deprecated"
        for cond in must_not
    )


@pytest.mark.asyncio()
async def test_qdrant_search_skips_deprecated_filter_when_included():
    """Qdrant search() with include_deprecated=True should not filter deprecated."""
    from unittest.mock import MagicMock

    from genesis.qdrant.collections import search

    client = MagicMock()
    client.query_points = MagicMock(return_value=MagicMock(points=[]))

    search(
        client,
        collection="episodic_memory",
        query_vector=[0.1] * 1024,
        limit=5,
        include_deprecated=True,
    )

    # Verify the filter does NOT include deprecated must_not
    call_kwargs = client.query_points.call_args.kwargs
    query_filter = call_kwargs["query_filter"]
    must_not = query_filter.must_not
    # must_not should be None or empty (no deprecated filter)
    if must_not:
        assert not any(
            getattr(cond, "key", None) == "deprecated"
            for cond in must_not
        )


@pytest.mark.asyncio()
@pytest.mark.parametrize("status", ["pending", "failed", "fts5_only"])
async def test_supersede_skips_qdrant_for_non_embedded(store, status):
    """D5 F3: _mark_superseded must NOT touch Qdrant for a memory with no
    vector. Only 'embedded' rows have a point; 'pending'/'failed'/'fts5_only'
    have none, so an update_payload on them is a doomed write. The old guard
    (`!= 'fts5_only'`) fired it for 'pending'/'failed' — this is the RED proof.
    """
    with patch("genesis.memory.store.update_payload") as mock_update, \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": "old", "collection": "episodic_memory",
            "embedding_status": status, "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=("old", "new"))

        await store._mark_superseded("old", "new", "2026-03-11T12:00:00")

    mock_update.assert_not_called()


@pytest.mark.asyncio()
async def test_supersede_link_invalidates_the_graph_cache(store, db):
    """The cached graph must learn about the new succeeded_by edge.

    The CRUD create deliberately does not invalidate (its callers do, by
    convention) — and this caller didn't either, so every supersede left the
    cached projection missing the edge until some unrelated write refreshed
    it. One of the two known invalidation gaps (issue #1641).
    """
    old_id = "old-memory-id"
    calls = []

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links, \
         patch("genesis.memory.graph.invalidate_graph_cache",
               side_effect=lambda: calls.append(1)):
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(return_value=(old_id, "new"))

        await store.store("new fact", "conversation", supersedes=old_id)

    assert calls, "supersede created a graph edge without invalidating the cached projection"


@pytest.mark.asyncio()
async def test_failed_supersede_link_does_not_invalidate(store, db):
    """CONTROL: a link create that RAISES must not invalidate — there is no
    new edge for the cache to learn."""
    old_id = "old-memory-id"
    calls = []

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links, \
         patch("genesis.memory.graph.invalidate_graph_cache",
               side_effect=lambda: calls.append(1)):
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # _mark_superseded now resolves short handles before the UPDATE (see
        # test_supersede_prefix_resolution). These ids are non-hex, so the real
        # resolver returns them PASSTHROUGH untouched with no DB read — mirror
        # that exactly rather than inventing a verdict the resolver never gives.
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": old_id, "collection": "episodic_memory",
            "embedding_status": "embedded", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_links.create = AsyncMock(side_effect=RuntimeError("db down"))

        await store.store("new fact", "conversation", supersedes=old_id)

    assert not calls, "a failed link create must not dirty the cache"


@pytest.mark.asyncio()
async def test_dedup_short_circuit_still_supersedes(store, db):
    """The dedup early-return skipped the supersede entirely.

    ``store()`` returns at the dedup check ~300 lines above the supersede
    block, so a correction whose content already existed deprecated nothing
    while the caller was told the store succeeded. It is also the LIKELIEST
    path, because retrying a failed supersede re-sends the same content.
    """
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links, \
         patch.object(MemoryStore, "_mark_superseded", new=AsyncMock()) as ms, \
         patch.object(
             MemoryStore, "_validate_supersede_pair", new=AsyncMock()
         ) as validate:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        # The pre-flight confirms a PASSTHROUGH handle names a real row.
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": "old-memory-id", "collection": "episodic_memory",
            "embedding_status": "fts5_only", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        # The content is already stored — the dedup short-circuit fires.
        mock_mem.find_exact_duplicate = AsyncMock(return_value="pre-existing-id")
        mock_links.create = AsyncMock(return_value=("old", "new"))

        out = await store.store(
            "a correction", "conversation", supersedes="old-memory-id",
        )

    assert out == "pre-existing-id"
    ms.assert_awaited_once()
    # and it must supersede TOWARD the memory that actually exists
    assert ms.await_args[0][1] == "pre-existing-id"
    # ...having validated the pair first, because THIS successor was chosen by
    # find_exact_duplicate rather than minted by this call. Same validator
    # memory_supersede uses.
    validate.assert_awaited_once_with("old-memory-id", "pre-existing-id")


@pytest.mark.asyncio()
async def test_an_unresolvable_target_never_reaches_the_dedup_lookup(store, db):
    """The duplication this closes, and the ordering that makes it impossible.

    Found in review of PR #1831: the supersede call added to the dedup path sat
    INSIDE the lookup's ``try``, whose handler is a bare ``except Exception``.
    A supersede failure was caught there, logged as "Dedup check failed"
    (misattributing the cause to a lookup that had just succeeded), and fell
    through into the full store pipeline — writing a SECOND copy of content the
    lookup had proved already existed.

    Handler placement was the patch. The fix is ORDER: the target is resolved
    before the lookup runs at all, so an unresolvable handle cannot reach the
    handler, cannot reach the pipeline, and cannot duplicate anything. Asserted
    as the lookup never happening, plus `create_metadata` — the duplication
    itself, not a proxy for it.
    """
    from genesis.memory.store import SupersedeUnresolved

    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        # Content that WOULD dedup, so a fall-through would be a duplicate.
        mock_mem.find_exact_duplicate = AsyncMock(return_value="pre-existing-id")
        # ...and a handle that names no memory: the resolver's NOT_FOUND.
        mock_mem.resolve_id = AsyncMock(return_value=([], "not_found"))
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_links.create = AsyncMock(return_value=("old", "new"))

        with pytest.raises(SupersedeUnresolved):
            await store.store(
                "a correction", "conversation", supersedes="deadbeef",
            )

    mock_mem.create_metadata.assert_not_awaited()
    mock_mem.find_exact_duplicate.assert_not_awaited(), (
        "resolution must precede the lookup, or the old fall-through is still "
        "reachable by a different route"
    )
    mock_mem.mark_superseded.assert_not_awaited()


@pytest.mark.asyncio()
@pytest.mark.parametrize("dedup_hit", [False, True], ids=["normal-path", "dedup-path"])
async def test_an_infrastructure_failure_is_recorded_not_just_logged(store, db, dedup_hit):
    """Both supersede call sites must REPORT a failure, not only log it.

    Silence here is the original defect arriving by a different door: the store
    succeeds, the deprecation does not, and the caller is told nothing. Covered
    at both sites because they are different code paths — the dedup one returns
    ~300 lines earlier — and a single test cannot reach both.
    """
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch.object(
             MemoryStore, "_validate_supersede_pair", new=AsyncMock()
         ):
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": "old-memory-id", "collection": "episodic_memory",
            "embedding_status": "fts5_only", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_mem.find_exact_duplicate = AsyncMock(
            return_value="pre-existing-id" if dedup_hit else None
        )
        # Pre-flight and pair validation pass; the deprecation itself throws.
        mock_mem.mark_superseded = AsyncMock(
            side_effect=RuntimeError("simulated SQLite fault")
        )

        outcome: dict = {}
        result = await store.store(
            "new content", "conversation",
            supersedes="old-memory-id", supersede_outcome=outcome,
        )

    assert isinstance(result, str) and result, "the store itself must survive"
    assert outcome == {"superseded": False}, (
        "the failure was logged and nothing else — the report would have "
        "claimed the supersede landed"
    )


@pytest.mark.asyncio()
async def test_a_successful_supersede_is_recorded_too(store, db):
    """Written on BOTH branches, so 'absent' never has to mean 'fine'."""
    with patch("genesis.memory.store.upsert_point"), \
         patch("genesis.memory.store.update_payload"), \
         patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.memory_links_crud") as mock_links:
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        mock_mem.resolve_id = AsyncMock(
            side_effect=lambda _db, mid: ([mid], "passthrough")
        )
        mock_mem.get_metadata = AsyncMock(return_value={
            "memory_id": "old-memory-id", "collection": "episodic_memory",
            "embedding_status": "fts5_only", "deprecated": 0,
            "superseded_by": None, "superseded_at": None,
        })
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.mark_superseded = AsyncMock(return_value=True)
        mock_links.create = AsyncMock(return_value=("old", "new"))

        outcome: dict = {}
        await store.store(
            "new content", "conversation",
            supersedes="old-memory-id", supersede_outcome=outcome,
        )

    assert outcome == {"superseded": True}
