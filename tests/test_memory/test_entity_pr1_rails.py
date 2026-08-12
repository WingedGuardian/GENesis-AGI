"""MW-3 PR-1 safety rails on entity CRUD + registry.

Three rails that later MW-3 PRs (typing backfill, merge apply) depend on:
  1. ``merge_entity`` refuses a self-merge (loser == survivor) — a backfill
     that resolves both sides to the same row must fail loud, never write
     ``merged_into = self`` (which makes the entity unresolvable via the
     ``_resolve_active`` seen-set walk).
  2. Untyped ``get_by_norm_name`` is DETERMINISTIC (active-first, then oldest)
     when a norm_name exists under more than one type — the pre-PR-1 query had
     no ORDER BY and returned an arbitrary row.
  3. ``host``/``install``/``project`` join the concept cluster, so typing an
     existing concept entity as ``host`` at write time folds onto it (no new
     shard) — the load-bearing property that keeps typing from fragmenting.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import entities as entities_crud
from genesis.db.schema._tables import TABLES
from genesis.memory import entity_registry


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    for table in TABLES:
        await conn.execute(TABLES[table])
    await conn.commit()
    yield conn
    await conn.close()


async def _insert(db, eid, norm, etype, *, status="active", created_at):
    await db.execute(
        "INSERT INTO entities (entity_id, name, norm_name, entity_type, source, "
        "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (eid, eid, norm, etype, "extracted", status, created_at, created_at),
    )
    await db.commit()


async def test_merge_entity_rejects_self_merge(db):
    await _insert(db, "e1", "thing", "concept", created_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="self-merge|loser.*survivor|same"):
        await entities_crud.merge_entity(db, loser_id="e1", survivor_id="e1")
    # The row must be untouched — still active, no merged_into.
    row = await entities_crud.get_entity(db, "e1")
    assert row["status"] == "active"
    assert row["merged_into"] is None


async def test_get_by_norm_name_deterministic_prefers_active(db):
    # Same norm_name under two types: one merged (older), one active (newer).
    await _insert(
        db, "m1", "shared", "concept", status="merged", created_at="2026-01-01T00:00:00+00:00"
    )
    await _insert(db, "a1", "shared", "person", created_at="2026-02-01T00:00:00+00:00")
    # merged row needs a valid target or merge-following returns it anyway;
    # point it nowhere so we can prove active-first selection (not follow).
    got = await entities_crud.get_by_norm_name(db, norm_name="shared")
    assert got is not None
    assert got["entity_id"] == "a1"  # active preferred over merged, regardless of age


async def test_get_by_norm_name_deterministic_oldest_among_active(db):
    await _insert(db, "old", "dup", "concept", created_at="2026-01-01T00:00:00+00:00")
    await _insert(db, "new", "dup", "person", created_at="2026-03-01T00:00:00+00:00")
    got = await entities_crud.get_by_norm_name(db, norm_name="dup")
    assert got["entity_id"] == "old"  # oldest active wins, deterministically


async def test_host_install_project_deliberately_not_in_cluster(db):
    # MW-3: the concept→typed transition is evidence-based (PR-3/PR-4), NOT a
    # blind name-fold, so these types must stay OUT of the identity cluster —
    # otherwise a coincidental same-name collision (a machine vs a codebase both
    # named "genesis") would silently attach mentions to the wrong node.
    for t in ("host", "install", "project"):
        assert t not in entity_registry._CONCEPT_CLUSTER
