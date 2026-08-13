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


def test_row_to_dict_tuple_fallback_matches_physical_order():
    # Codex P2 (freshness pass): the two card columns were inserted after
    # `summary` and BEFORE `source`, so a hand-indexed positional map that still
    # read row[5] as `source` would mis-decode every field after `summary` on
    # any connection yielding plain tuples (row_factory unset). The fallback must
    # track the CURRENT 12-column physical order.
    row = (
        "eid",
        "Name",
        "norm",
        "concept",  # 0-3
        "card text",  # 4  summary
        "2026-05-01T00:00:00+00:00",  # 5  summary_updated_at
        1,  # 6  summary_dirty
        "extracted",  # 7  source
        "active",  # 8  status
        "surv",  # 9  merged_into
        "2026-01-01T00:00:00+00:00",  # 10 created_at
        "2026-02-01T00:00:00+00:00",  # 11 updated_at
    )
    d = entities_crud._row_to_dict(None, row)
    assert d["summary"] == "card text"
    assert d["summary_updated_at"] == "2026-05-01T00:00:00+00:00"
    assert d["summary_dirty"] == 1
    assert d["source"] == "extracted"
    assert d["status"] == "active"
    assert d["merged_into"] == "surv"
    assert d["created_at"] == "2026-01-01T00:00:00+00:00"
    assert d["updated_at"] == "2026-02-01T00:00:00+00:00"


async def test_tuple_decode_independent_of_physical_column_order():
    # Codex R5-C: a partial upgrade can leave the card columns ALTER-appended at
    # the END of the physical order (legacy 10 cols first) while all names are
    # present. Reads must decode by COLUMN NAME (explicit SELECT list), never by
    # physical position — order-independence by construction, not by matching a
    # hand-kept list to one canonical layout.
    conn = await aiosqlite.connect(":memory:")  # no row_factory → tuples
    try:
        await conn.execute(
            """
            CREATE TABLE entities (
                entity_id   TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                norm_name   TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                summary     TEXT,
                source      TEXT NOT NULL DEFAULT 'extracted',
                status      TEXT NOT NULL DEFAULT 'active',
                merged_into TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )
        # The partial-upgrade shape: names present, physical order DIFFERENT
        # from the canonical CREATE (appended at the end).
        await conn.execute("ALTER TABLE entities ADD COLUMN summary_updated_at TEXT")
        await conn.execute(
            "ALTER TABLE entities ADD COLUMN summary_dirty INTEGER NOT NULL DEFAULT 0"
        )
        await conn.execute(
            "INSERT INTO entities (entity_id, name, norm_name, entity_type, summary, "
            "source, status, merged_into, created_at, updated_at, "
            "summary_updated_at, summary_dirty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "e9",
                "Appended",
                "appended",
                "concept",
                "card",
                "extracted",
                "active",
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
                "2026-05-01T00:00:00+00:00",
                1,
            ),
        )
        await conn.commit()
        got = await entities_crud.get_entity(conn, "e9")
        assert got["source"] == "extracted"
        assert got["status"] == "active"
        assert got["summary_updated_at"] == "2026-05-01T00:00:00+00:00"
        assert got["summary_dirty"] == 1
        assert got["created_at"] == "2026-01-01T00:00:00+00:00"
    finally:
        await conn.close()


async def test_get_entity_decodes_tuple_connection_correctly():
    # Full-path regression: a connection WITHOUT aiosqlite.Row yields tuples, so
    # SELECT * + _row_to_dict must decode against the real physical order. Proves
    # the fallback isn't corrupted by the card columns inserted before `source`.
    conn = await aiosqlite.connect(":memory:")  # NOTE: no row_factory → tuples
    try:
        for table in TABLES:
            await conn.execute(TABLES[table])
        await conn.execute(
            "INSERT INTO entities (entity_id, name, norm_name, entity_type, summary, "
            "summary_updated_at, summary_dirty, source, status, merged_into, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "e1",
                "Genesis",
                "genesis",
                "concept",
                "a card",
                "2026-05-01T00:00:00+00:00",
                1,
                "extracted",
                "active",
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
            ),
        )
        await conn.commit()
        got = await entities_crud.get_entity(conn, "e1")
        assert got["source"] == "extracted"
        assert got["status"] == "active"
        assert got["summary_dirty"] == 1
        assert got["summary_updated_at"] == "2026-05-01T00:00:00+00:00"
        assert got["merged_into"] is None
    finally:
        await conn.close()
