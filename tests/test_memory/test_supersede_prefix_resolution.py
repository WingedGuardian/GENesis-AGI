"""memory_store(supersedes=...) must resolve the short handles the ecosystem hands out.

The proactive recall hook prints memories as ``id:<8-char-prefix>`` and
``memory_expand`` resolves those handles on the READ side
(``mcp/memory/core.py::_resolve_id_prefixes``, whose docstring says the handles
"must resolve here"). The WRITE side never got the same treatment: ``supersedes``
went to an exact-match UPDATE that matched nothing, ``mark_superseded``'s
"did I find it" return value was discarded, and the only artifact of the whole
operation was a dangling ``succeeded_by`` edge whose source is not a memory.

MEASURED on the live DB 2026-09-06: three such rows created in a 14-minute
window (0 historically); one of the three targets is still ``deprecated=0``,
i.e. a memory that a session believed it had corrected is still live in recall.

Real SQLite here, not mocks — the defect IS which rows the SQL matches, and a
mocked connection would happily confirm whatever the test asserted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.memory.store import MemoryStore

OLD = "abcd1234-0000-4000-8000-000000000001"
NEW = "efab5678-0000-4000-8000-000000000002"
PREFIX = OLD[:8]


@pytest.fixture()
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    # DDL copied from production (sqlite_master, 2026-09-06) rather than
    # hand-rolled: a fixture missing MW-2's proposed_type made the regression
    # lock fail for the WRONG reason on the first RED run, which proves nothing.
    await conn.execute(
        """CREATE TABLE memory_metadata (
               memory_id        TEXT PRIMARY KEY,
               created_at       TEXT NOT NULL,
               collection       TEXT NOT NULL DEFAULT 'episodic_memory',
               confidence       REAL,
               embedding_status TEXT NOT NULL DEFAULT 'embedded',
               memory_class TEXT DEFAULT 'fact', wing TEXT, room TEXT,
               valid_at TEXT, invalid_at TEXT, source_subsystem TEXT,
               deprecated INTEGER NOT NULL DEFAULT 0, dream_cycle_run_id TEXT,
               superseded_by TEXT, superseded_at TEXT, origin_class TEXT,
               provenance_class TEXT, trust_level TEXT, attribution TEXT,
               origin_ref TEXT, capture_clarity REAL, deprecated_at TEXT,
               speech_act TEXT, speech_act_confidence REAL,
               assertion_provenance TEXT, durability TEXT, expires_at TEXT
           )"""
    )
    await conn.execute(
        """CREATE TABLE memory_links (
               source_id   TEXT NOT NULL,
               target_id   TEXT NOT NULL,
               link_type   TEXT NOT NULL CHECK (
                   link_type IN (
                       'supports','contradicts','extends','elaborates',
                       'discussed_in','evaluated_for','decided',
                       'action_item_for','categorized_as','related_to',
                       'succeeded_by','preceded_by'
                   )
               ),
               strength    REAL NOT NULL DEFAULT 0.5,
               created_at  TEXT NOT NULL,
               proposed_type TEXT, confidence REAL, classifier TEXT,
               review_state TEXT, safe_for_boost INTEGER,
               PRIMARY KEY (source_id, target_id, link_type)
           )"""
    )
    for mid in (OLD, NEW):
        await conn.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, embedding_status) "
            "VALUES (?, '2026-09-06T00:00:00+00:00', 'fts5_only')",
            (mid,),
        )
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture()
def store(db):
    ep = MagicMock()
    ep.embed = AsyncMock(return_value=[0.1] * 1024)
    return MemoryStore(
        embedding_provider=ep,
        qdrant_client=MagicMock(),
        db=db,
        linker=MagicMock(),
    )


async def _row(db, mid):
    cur = await db.execute(
        "SELECT deprecated, superseded_by FROM memory_metadata WHERE memory_id = ?", (mid,)
    )
    return await cur.fetchone()


async def _links(db):
    cur = await db.execute("SELECT source_id, target_id, link_type FROM memory_links")
    return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio()
async def test_eight_char_prefix_deprecates_the_memory_it_names(store, db):
    """THE LIVE BUG: an 8-char handle must supersede, not silently no-op."""
    await store._mark_superseded(PREFIX, NEW, "2026-09-06T19:53:21+00:00")

    row = await _row(db, OLD)
    assert row["deprecated"] == 1, (
        f"supersedes={PREFIX!r} left the memory live — this is the measured "
        "live defect: the caller was told the store succeeded."
    )
    assert row["superseded_by"] == NEW


@pytest.mark.asyncio()
async def test_prefix_never_becomes_a_dangling_link_source(store, db):
    """The only artifact of the broken path was an edge from a non-memory."""
    await store._mark_superseded(PREFIX, NEW, "2026-09-06T19:53:21+00:00")

    links = await _links(db)
    assert (PREFIX, NEW, "succeeded_by") not in links, (
        "wrote a succeeded_by edge whose source is an 8-char prefix, not a memory"
    )
    assert (OLD, NEW, "succeeded_by") in links


@pytest.mark.asyncio()
async def test_unknown_id_is_reported_not_swallowed(store, db):
    """A prefix matching nothing must be LOUD — today it is silent."""
    from genesis.memory.store import SupersedeUnresolved

    with pytest.raises(SupersedeUnresolved) as exc:
        await store._mark_superseded("deadbeef", NEW, "2026-09-06T19:53:21+00:00")

    assert exc.value.reason == "not_found"
    assert exc.value.stored_memory_id == NEW, "caller needs a handle on what DID land"
    assert await _links(db) == [], "no edge may be written for an unresolved target"


@pytest.mark.asyncio()
async def test_ambiguous_prefix_is_never_guessed(store, db):
    """Two memories share a prefix — supersede neither, name both."""
    from genesis.memory.store import SupersedeUnresolved

    twin = OLD[:8] + "-0000-0000-0000-000000000000"
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, embedding_status) "
        "VALUES (?, '2026-09-06T00:00:00+00:00', 'fts5_only')",
        (twin,),
    )
    await db.commit()

    with pytest.raises(SupersedeUnresolved) as exc:
        await store._mark_superseded(PREFIX, NEW, "2026-09-06T19:53:21+00:00")

    assert exc.value.reason == "ambiguous"
    assert set(exc.value.candidates) == {OLD, twin}
    assert (await _row(db, OLD))["deprecated"] == 0
    assert (await _row(db, twin))["deprecated"] == 0
    assert await _links(db) == []


@pytest.mark.asyncio()
async def test_full_id_still_works(store, db):
    """Regression lock: the 36-char path is unchanged."""
    await store._mark_superseded(OLD, NEW, "2026-09-06T19:53:21+00:00")

    assert (await _row(db, OLD))["deprecated"] == 1
    assert (OLD, NEW, "succeeded_by") in await _links(db)



@pytest.mark.asyncio()
async def test_a_full_id_naming_no_memory_is_reported_not_swallowed(store, db):
    """The PASSTHROUGH hole: a 36-char id skips resolution entirely.

    ``resolve_unique_prefix`` returns PASSTHROUGH for anything full-length or
    non-hex, so those ids reach the UPDATE unverified and ``mark_superseded``'s
    return is the ONLY thing that catches them. Deleting that check left all 84
    tests green — every other test either hands the resolver a prefix (caught
    earlier, at NOT_FOUND) or mocks the CRUD to return True.
    """
    from genesis.memory.store import SupersedeUnresolved

    ghost = "deadbeef-0000-4000-8000-000000000009"  # 36 chars, no such row
    with pytest.raises(SupersedeUnresolved) as exc:
        await store._mark_superseded(ghost, NEW, "2026-09-06T19:53:21+00:00")

    assert exc.value.reason == "not_found"
    assert await _links(db) == [], "no succeeded_by edge may be written"


@pytest.mark.asyncio()
async def test_a_truncated_paste_in_the_32_to_35_band_resolves(store, db):
    """Locks ``full_len=36``, and pins the band that value actually changes.

    A COMPLETE 36-char id is PASSTHROUGH under either full_len, so it cannot
    lock this parameter. The 32-to-35 band is the real difference: under the
    resolver's default of 32 such a truncation counts as full-length and goes
    to an exact-match lookup that cannot hit.
    """
    truncated = OLD[:33]
    assert 32 <= len(truncated) < 36, "fixture must sit in the band under test"

    await store._mark_superseded(truncated, NEW, "2026-09-06T19:53:21+00:00")

    assert (await _row(db, OLD))["deprecated"] == 1
    assert (OLD, NEW, "succeeded_by") in await _links(db)


@pytest.mark.asyncio()
async def test_a_saturated_candidate_list_is_flagged_as_truncated(store, db):
    """The resolver reads with LIMIT 3, so 3 matches may mean "3 or more".

    Reporting three ids as though they were the whole collision set is the
    truncated-listing trap: the caller cannot tell a complete answer from a
    clipped one unless the clip is declared.
    """
    from genesis.memory.store import SupersedeUnresolved

    for n in range(3):
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, embedding_status) "
            "VALUES (?, '2026-09-06T00:00:00+00:00', 'fts5_only')",
            (f"{OLD[:8]}-ffff-4000-8000-00000000000{n}",),
        )
    await db.commit()

    with pytest.raises(SupersedeUnresolved) as exc:
        await store._mark_superseded(OLD[:8], NEW, "2026-09-06T19:53:21+00:00")

    assert exc.value.reason == "ambiguous"
    assert exc.value.truncated is True
    assert "possibly more" in str(exc.value)
