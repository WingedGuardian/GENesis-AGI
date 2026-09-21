"""The supersede as its own operation.

``memory_store(supersedes=...)`` couples two things: storing content, and
deprecating a different memory. That coupling is where the whole class of
supersede defects came from — the successor was whatever the store happened to
produce, so the caller never named it and never saw it; the deprecation lived
inside the store's control flow, so an early return skipped it; and a failure
after the content landed left a partial outcome that had to be narrated back
through a return value.

Naming both ids removes most of it. Every check is a precondition and every
rejection is pre-write, so a REJECTION raises. But the deprecation itself
writes three stores independently — SQLite, then the Qdrant payload, then the
link — so a failure partway through is partial: something may already be
durable. That case reports ``superseded: false`` with a retry remedy rather
than raising a failure whose first half already landed; every step is
idempotent, so the same call again completes it.

Real SQLite, not mocks — the invariant under test is which rows the SQL touches
and, for the rejection cases, that it touches NONE.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.memory.store import MemoryStore, SupersedeUnresolved

OLD = "abcd1234-0000-4000-8000-000000000001"
NEW = "efab5678-0000-4000-8000-000000000002"
DEAD = "beef9999-0000-4000-8000-000000000003"


@pytest.fixture()
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    # DDL copied from production (sqlite_master), not hand-rolled: a fixture
    # missing a column makes a regression lock fail for the wrong reason.
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
    await conn.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, embedding_status, deprecated) "
        "VALUES (?, '2026-09-06T00:00:00+00:00', 'fts5_only', 1)",
        (DEAD,),
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
        "SELECT deprecated, superseded_by FROM memory_metadata WHERE memory_id = ?",
        (mid,),
    )
    return await cur.fetchone()


async def _links(db):
    cur = await db.execute("SELECT source_id, target_id, link_type FROM memory_links")
    return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio()
async def test_supersede_deprecates_the_target_and_links_it(store, db):
    await store.supersede(OLD, NEW)

    row = await _row(db, OLD)
    assert row["deprecated"] == 1
    assert row["superseded_by"] == NEW
    assert (OLD, NEW, "succeeded_by") in await _links(db)


@pytest.mark.asyncio()
async def test_supersede_accepts_short_handles_on_both_sides(store, db):
    """The successor gets resolution too — which the coupled call could never
    do, because there the successor was minted internally and never named."""
    await store.supersede(OLD[:8], NEW[:8])

    assert (await _row(db, OLD))["deprecated"] == 1
    assert (OLD, NEW, "succeeded_by") in await _links(db)


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("old_handle", "new_handle", "reason", "role"),
    [
        ("deadbeef", NEW, "not_found", "supersedes"),
        (OLD, "deadbeef", "not_found", "new_id"),
        (OLD, OLD, "self_supersede", "supersedes"),
        (OLD, DEAD, "successor_deprecated", "supersedes"),
    ],
    ids=["target-unknown", "successor-unknown", "self", "successor-deprecated"],
)
async def test_a_rejected_pair_changes_nothing(store, db, old_handle, new_handle, reason, role):
    """Every rejection is pre-write, so a failed supersede is a no-op.

    That is the property the coupled call could not have: there, a rejection
    arrived with content already stored, which is why it had to report a partial
    outcome instead of simply failing.
    """
    before = await _row(db, OLD)

    with pytest.raises(SupersedeUnresolved) as exc:
        await store.supersede(old_handle, new_handle)

    assert exc.value.reason == reason
    assert exc.value.role == role, "the error must name the parameter at fault"
    after = await _row(db, OLD)
    assert (after["deprecated"], after["superseded_by"]) == (
        before["deprecated"],
        before["superseded_by"],
    )
    assert await _links(db) == [], "a rejected supersede wrote an edge"


@pytest.mark.asyncio()
async def test_an_ambiguous_handle_is_never_guessed(store, db):
    twin = OLD[:8] + "-0000-0000-0000-000000000000"
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, embedding_status) "
        "VALUES (?, '2026-09-06T00:00:00+00:00', 'fts5_only')",
        (twin,),
    )
    await db.commit()

    with pytest.raises(SupersedeUnresolved) as exc:
        await store.supersede(OLD[:8], NEW)

    assert exc.value.reason == "ambiguous"
    assert set(exc.value.candidates) == {OLD, twin}
    assert (await _row(db, OLD))["deprecated"] == 0
    assert (await _row(db, twin))["deprecated"] == 0


@pytest.mark.asyncio()
async def test_the_tool_reports_the_ids_it_acted_on(db):
    """The MCP surface: a flat result, because there is one outcome to report.

    The ids come back RESOLVED — the caller passed a short handle, but what it
    may need to retry against is the canonical id.
    """
    from genesis.mcp.memory import core

    real = MemoryStore(
        embedding_provider=MagicMock(),
        qdrant_client=MagicMock(),
        db=db,
        linker=MagicMock(),
    )
    tools = await core.mcp.get_tools()
    with pytest.MonkeyPatch.context() as mp:
        mod = MagicMock()
        mod._store = real
        mp.setattr(core, "_memory_mod", lambda: mod)
        result = await tools["memory_supersede"].fn(OLD[:8], NEW)

    assert result == {"superseded": True, "old_id": OLD, "new_id": NEW}
    assert (await _row(db, OLD))["deprecated"] == 1


@pytest.mark.asyncio()
async def test_the_tool_raises_rather_than_reporting_a_failure(db):
    """Contrast with `memory_store`, which must report: there, content is
    already durable when the supersede fails. Here nothing is, so an exception
    is the honest signal and the caller can simply fix the ids and retry."""
    from genesis.mcp.memory import core

    real = MemoryStore(
        embedding_provider=MagicMock(),
        qdrant_client=MagicMock(),
        db=db,
        linker=MagicMock(),
    )
    tools = await core.mcp.get_tools()
    with pytest.MonkeyPatch.context() as mp:
        mod = MagicMock()
        mod._store = real
        mp.setattr(core, "_memory_mod", lambda: mod)
        with pytest.raises(SupersedeUnresolved):
            await tools["memory_supersede"].fn("deadbeef", NEW)

    assert (await _row(db, OLD))["deprecated"] == 0


@pytest.mark.asyncio()
async def test_a_partway_failure_reports_partial_and_retry_completes(store, db):
    """The writes commit per store, so a mid-deprecation failure is partial —
    and must be REPORTED as such, not raised as if nothing happened.

    Regression lock for the Devin Review finding on PR #1937: when the Qdrant
    payload update throws, SQLite has already committed ``deprecated=1``.
    Raising that case tells the caller "it failed" while recall state has
    already changed in one direction; the honest contract is
    ``superseded: false`` plus a remedy, and the remedy — the same call again —
    must actually work, because every step is idempotent.
    """
    import genesis.memory.store as store_mod

    await db.execute(
        "UPDATE memory_metadata SET embedding_status = 'embedded' "
        "WHERE memory_id = ?",
        (OLD,),
    )
    await db.commit()

    real_update_payload = store_mod.update_payload

    def _failing_payload(*a, **kw):
        raise ConnectionError("qdrant down")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "update_payload", _failing_payload)
        result = await store.supersede(OLD, NEW)

    assert result["superseded"] is False
    assert result["old_id"] == OLD and result["new_id"] == NEW
    assert "memory_supersede" in result["warning"]
    # Partial state is real: SQLite already deprecated the target.
    assert (await _row(db, OLD))["deprecated"] == 1

    # The advertised remedy — retry the same call — completes it.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store_mod, "update_payload", real_update_payload)
        retry = await store.supersede(OLD, NEW)

    assert retry["superseded"] is True
    assert (OLD, NEW, "succeeded_by") in await _links(db)
