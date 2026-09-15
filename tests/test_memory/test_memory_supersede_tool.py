"""The supersede as its own operation.

``memory_store(supersedes=...)`` couples two things: storing content, and
deprecating a different memory. That coupling is where the whole class of
supersede defects came from — the successor was whatever the store happened to
produce, so the caller never named it and never saw it; the deprecation lived
inside the store's control flow, so an early return skipped it; and a failure
after the content landed left a partial outcome that had to be narrated back
through a return value.

Naming both ids removes all of it. Every check is a precondition, every
rejection is pre-write, and a failure is safe to retry because nothing was
stored. That is why this raises instead of reporting: there is no durable
content whose fate the caller would have to interpret alongside the error.

Real SQLite, not mocks — the invariant under test is which rows the SQL touches
and, for the rejection cases, that it touches NONE.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.memory.store import (
    MemoryStore,
    SupersedeIncomplete,
    SupersedeUnresolved,
)

OLD = "abcd1234-0000-4000-8000-000000000001"
NEW = "efab5678-0000-4000-8000-000000000002"
DEAD = "beef9999-0000-4000-8000-000000000003"
EXPIRED = "cafe0000-0000-4000-8000-000000000004"


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
    # Live (not deprecated) but temporally invalid: recall's bitemporal
    # filter hides it exactly like a deprecated row.
    await conn.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, embedding_status, invalid_at) "
        "VALUES (?, '2026-09-06T00:00:00+00:00', 'fts5_only', "
        "'2020-01-01T00:00:00+00:00')",
        (EXPIRED,),
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
        # Successor failures are the SUCCESSOR's fault: role must say new_id,
        # or the caller is sent to fix the handle it got right (Codex P2
        # 3981896399 / CodeRabbit 3996743250 — the old expectation here
        # encoded the buggy attribution).
        (OLD, DEAD, "successor_deprecated", "new_id"),
        (OLD, EXPIRED, "successor_expired", "new_id"),
    ],
    ids=[
        "target-unknown",
        "successor-unknown",
        "self",
        "successor-deprecated",
        "successor-expired",
    ],
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

    No `superseded: false` branch, no reason taxonomy, no retry advice — a
    failure raises, and the caller has lost nothing by it.
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

    assert result == {"superseded": True, "old_id": OLD[:8], "new_id": NEW}
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
async def test_a_future_invalid_at_successor_is_accepted(store, db):
    """Control for the expiry check: bitemporal validity that has not run out
    is a LIVE successor — the rejection is about hidden-from-recall, not about
    the column being set."""
    await db.execute(
        "UPDATE memory_metadata SET invalid_at = '2035-01-01T00:00:00+00:00' WHERE memory_id = ?",
        (NEW,),
    )
    await db.commit()

    await store.supersede(OLD, NEW)

    assert (await _row(db, OLD))["deprecated"] == 1


@pytest.mark.asyncio()
async def test_a_qdrant_failure_raises_instead_of_reporting_success(store, db, monkeypatch):
    """The standalone path must not say `superseded: true` over a stale vector
    mirror (Codex P1 3981896382 / CodeRabbit 3996743248): vector search only
    excludes a memory through the Qdrant `deprecated` payload, so a swallowed
    update_payload failure leaves the superseded fact surfacing forever."""
    from genesis.memory import store as store_mod

    await db.execute(
        "UPDATE memory_metadata SET embedding_status = 'embedded' WHERE memory_id = ?",
        (OLD,),
    )
    await db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(store_mod, "update_payload", _boom)

    with pytest.raises(SupersedeIncomplete) as exc:
        await store.supersede(OLD, NEW)

    assert exc.value.stage == "qdrant"
    # HONEST partial state: SQLite committed — the error must describe a
    # half-finished operation, not pretend nothing happened.
    assert (await _row(db, OLD))["deprecated"] == 1


@pytest.mark.asyncio()
async def test_retrying_after_a_mirror_failure_completes_the_supersession(store, db, monkeypatch):
    """SupersedeIncomplete's contract: the retry IS the repair."""
    from genesis.memory import store as store_mod

    await db.execute(
        "UPDATE memory_metadata SET embedding_status = 'embedded' WHERE memory_id = ?",
        (OLD,),
    )
    await db.commit()

    calls = {"n": 0}

    def _fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(store_mod, "update_payload", _fail_once)

    with pytest.raises(SupersedeIncomplete):
        await store.supersede(OLD, NEW)

    await store.supersede(OLD, NEW)

    assert (await _row(db, OLD))["deprecated"] == 1
    assert (OLD, NEW, "succeeded_by") in await _links(db)


@pytest.mark.asyncio()
async def test_a_link_failure_raises_in_strict_mode(store, db, monkeypatch):
    """The succeeded_by edge is half the advertised outcome — losing it
    silently orphans the correction from graph traversal."""
    from genesis.memory import store as store_mod

    async def _boom(*args, **kwargs):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(store_mod.memory_links_crud, "create", _boom)

    with pytest.raises(SupersedeIncomplete) as exc:
        await store.supersede(OLD, NEW)

    assert exc.value.stage == "link"


@pytest.mark.asyncio()
async def test_the_store_path_stays_best_effort_on_mirror_failure(store, db, monkeypatch):
    """Control: `store(supersedes=...)` keeps swallowing mirror failures — the
    new content's fate dominates that call's outcome. Only the standalone
    path is strict. Drives `_mark_superseded` at its default, the exact seam
    `store()` uses."""
    from genesis.memory import store as store_mod

    await db.execute(
        "UPDATE memory_metadata SET embedding_status = 'embedded' WHERE memory_id = ?",
        (OLD,),
    )
    await db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(store_mod, "update_payload", _boom)

    # Default (non-strict) — must NOT raise.
    await store._mark_superseded(OLD, NEW, "2026-09-15T00:00:00+00:00")

    assert (await _row(db, OLD))["deprecated"] == 1


@pytest.mark.asyncio()
async def test_supersede_serializes_with_a_lock_on_the_successor(store, db):
    """The validate→mark window (CodeRabbit 3996743245): a delete or
    supersession of new_id must not commit between the successor check and
    the write. supersede() holds BOTH id locks (sorted order), so an
    operation already holding the successor's lock blocks it."""
    import asyncio

    from genesis.memory._locks import memory_id_lock

    async with memory_id_lock(NEW):
        task = asyncio.create_task(store.supersede(OLD, NEW))
        await asyncio.sleep(0.05)
        assert not task.done(), (
            "supersede proceeded without the successor's lock — the validate→mark window is open"
        )
    await asyncio.wait_for(task, timeout=2)

    assert (await _row(db, OLD))["deprecated"] == 1


@pytest.mark.asyncio()
async def test_supersede_serializes_with_a_lock_on_the_target(store, db):
    """BOTH ids are locked, not just the successor.

    `MemoryStore.delete(old_id)` holds the target's id-lock and writes the
    same memory across SQLite AND Qdrant — the exact interleaving `_locks.py`
    exists to prevent. Locking only the successor would leave supersede free
    to run through a concurrent delete of its own target.

    Added because the successor-side test ALONE let a mutation that decoys the
    target's lock survive: the property was true and pinned by nothing.
    """
    import asyncio

    from genesis.memory._locks import memory_id_lock

    async with memory_id_lock(OLD):
        task = asyncio.create_task(store.supersede(OLD, NEW))
        await asyncio.sleep(0.05)
        assert not task.done(), (
            "supersede proceeded without the target's lock — it can interleave "
            "with delete(old_id), which writes the same memory in both stores"
        )
    await asyncio.wait_for(task, timeout=2)

    assert (await _row(db, OLD))["deprecated"] == 1
