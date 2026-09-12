"""MW-2 — 0082 edge-metadata columns on ``memory_links``: migration + persistence.

Lean-keystone STORE side: five judgment-metadata columns (`proposed_type`,
`confidence`, `classifier`, `review_state`, `safe_for_boost`) exist on both
schema build paths (numbered migration for legacy DBs, canonical CREATE for
fresh DBs), the migration is idempotent, and ``memory_links.create`` can stamp
them. NOTHING writes them in production yet — they are the stamping location
MW-5's merge gate (and the MW-2 measurement probe) will use.

Deliberately ABSENT (deferred MW-2b, per the 2026-08-10 measurement): no
``candidate_similar`` link type, no CHECK change, no table rebuild — the CHECK
must stay byte-identical, locked by a test here.
"""

from __future__ import annotations

import importlib

import aiosqlite

from genesis.db.crud import memory_links
from genesis.db.schema import TABLES

_mig = importlib.import_module("genesis.db.migrations.0082_mw2_link_metadata")

_METADATA_COLS = {
    "proposed_type",
    "confidence",
    "classifier",
    "review_state",
    "safe_for_boost",
}

# A memory_links shaped like a legacy DB that predates MW-2 (no metadata cols).
_LEGACY_DDL = """
    CREATE TABLE memory_links (
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
        PRIMARY KEY (source_id, target_id, link_type)
    )
"""


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_migration_adds_all_five_columns_to_legacy_db():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_LEGACY_DDL)
        await conn.commit()
        assert _METADATA_COLS & await _columns(conn, "memory_links") == set()

        await _mig.up(conn)
        await conn.commit()

        assert await _columns(conn, "memory_links") >= _METADATA_COLS
    finally:
        await conn.close()


async def test_migration_is_idempotent_and_preserves_rows():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_LEGACY_DDL)
        await conn.execute(
            "INSERT INTO memory_links VALUES ('a', 'b', 'supports', 0.8, '2026-08-10')"
        )
        await conn.commit()

        await _mig.up(conn)
        await _mig.up(conn)  # second run must not raise (duplicate-column guard)
        await conn.commit()

        assert await _columns(conn, "memory_links") >= _METADATA_COLS
        cur = await conn.execute(
            "SELECT strength, proposed_type FROM memory_links WHERE source_id='a' AND target_id='b'"
        )
        row = await cur.fetchone()
        assert row[0] == 0.8  # data preserved
        assert row[1] is None  # new cols NULL (no backfill)
    finally:
        await conn.close()


async def test_fresh_db_create_carries_columns():
    """The canonical CREATE TABLE (fresh-DB path) must already carry the cols —
    else a fresh install diverges from a migrated one (base-path parity)."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_links"])
        await conn.commit()
        assert await _columns(conn, "memory_links") >= _METADATA_COLS
    finally:
        await conn.close()


async def test_check_constraint_unchanged_no_candidate_similar():
    """MW-2b is DEFERRED: the CHECK allowlist must stay the 12 types — a
    ``candidate_similar`` INSERT must still be rejected on both build paths,
    and a legacy ``extends`` INSERT must still work."""
    for ddl in (TABLES["memory_links"], _LEGACY_DDL):
        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.execute(ddl)
            if ddl is _LEGACY_DDL:
                await _mig.up(conn)
            await conn.commit()

            # Legacy type still accepted.
            await conn.execute(
                "INSERT INTO memory_links (source_id, target_id, link_type, "
                "strength, created_at) VALUES ('a', 'b', 'extends', 0.9, 't')"
            )
            # 13th type still rejected (CHECK unchanged).
            try:
                await conn.execute(
                    "INSERT INTO memory_links (source_id, target_id, link_type, "
                    "strength, created_at) VALUES ('a', 'c', 'candidate_similar', 0.9, 't')"
                )
                raise AssertionError("candidate_similar INSERT should have violated CHECK")
            except aiosqlite.IntegrityError:
                pass
        finally:
            await conn.close()


async def test_pk_still_three_column():
    """PK must remain (source_id, target_id, link_type) — supports AND
    contradicts for the same pair coexist (DLI-04)."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_links"])
        await conn.commit()
        for ltype in ("supports", "contradicts"):
            await conn.execute(
                "INSERT INTO memory_links (source_id, target_id, link_type, "
                f"strength, created_at) VALUES ('a', 'b', '{ltype}', 0.5, 't')"
            )
        cur = await conn.execute(
            "SELECT COUNT(*) FROM memory_links WHERE source_id='a' AND target_id='b'"
        )
        assert (await cur.fetchone())[0] == 2
    finally:
        await conn.close()


async def test_create_stamps_metadata_kwargs():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_links"])
        await conn.commit()

        await memory_links.create(
            conn,
            source_id="m1",
            target_id="m2",
            # link_type stays an allowlist value; the VERDICT rides proposed_type.
            link_type="supports",
            strength=0.9,
            created_at="2026-08-10T00:00:00+00:00",
            proposed_type="duplicate",
            confidence=0.93,
            classifier="llm:dream_cycle_relationship_classify",
            review_state="classified",
            safe_for_boost=1,
        )
        cur = await conn.execute(
            "SELECT proposed_type, confidence, classifier, review_state, "
            "safe_for_boost FROM memory_links WHERE source_id='m1'"
        )
        row = await cur.fetchone()
        assert row == ("duplicate", 0.93, "llm:dream_cycle_relationship_classify", "classified", 1)
    finally:
        await conn.close()


async def test_create_defaults_null_when_unstamped():
    """Every existing caller passes no metadata — the columns stay NULL (legacy
    semantics: NULL safe_for_boost = boost-eligible, NULL review_state = typed
    truth). This is the zero-regression contract."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["memory_links"])
        await conn.commit()

        await memory_links.create(
            conn,
            source_id="m1",
            target_id="m2",
            link_type="extends",
            strength=0.91,
            created_at="2026-08-10T00:00:00+00:00",
        )
        cur = await conn.execute(
            "SELECT proposed_type, confidence, classifier, review_state, "
            "safe_for_boost FROM memory_links WHERE source_id='m1'"
        )
        assert await cur.fetchone() == (None, None, None, None, None)
    finally:
        await conn.close()
