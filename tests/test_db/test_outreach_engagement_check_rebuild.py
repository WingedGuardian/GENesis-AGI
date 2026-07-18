"""WS-2 P1b (f/u 54e0fa72): the engagement_outcome enforcing-CHECK rebuild.

The original CHECK ``IN (..., NULL)`` was a no-op (SQL three-valued logic:
non-matches compare NULL, which passes) — writers drifted the vocabulary for
months. The fix is rebuild #4 in the ``_migrate_add_columns`` chain, and its
DDL must preserve the EXACT category-CHECK fragments the three earlier
rebuild probes match on, or a later boot would re-trigger an older rebuild
and undo it. That downgrade loop is pinned here.
"""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from genesis.db.schema._migrations import _migrate_add_columns

# The PRE-fix table exactly as a live 2026-07 install has it: 10-value
# category CHECK (post-'notification' rebuild), no-op engagement CHECK.
_OLD_DDL = """
    CREATE TABLE outreach_history (
        id                  TEXT PRIMARY KEY,
        person_id           TEXT,
        signal_type         TEXT NOT NULL,
        topic               TEXT NOT NULL,
        category            TEXT NOT NULL CHECK (category IN (
            'blocker', 'alert', 'finding', 'insight', 'opportunity',
            'digest', 'surplus', 'approval', 'content', 'notification'
        )),
        salience_score      REAL NOT NULL,
        channel             TEXT NOT NULL,
        message_content     TEXT NOT NULL,
        drive_alignment     TEXT,
        labeled_surplus     INTEGER DEFAULT 0,
        content_hash        TEXT,
        delivery_id         TEXT,
        delivered_at        TEXT,
        opened_at           TEXT,
        user_response       TEXT,
        action_taken        TEXT,
        engagement_outcome  TEXT CHECK (engagement_outcome IN (
            'useful', 'not_useful', 'ambivalent', 'ignored', NULL
        )),
        engagement_signal   TEXT,
        prediction_error    REAL,
        created_at          TEXT NOT NULL
    )
"""

# One row per live-census archetype, plus the drift the no-op CHECK admitted.
_SEED = [
    ("r-useful", "useful"),
    ("r-ignored", "ignored"),
    ("r-ambivalent", "ambivalent"),
    ("r-acked", "acknowledged"),  # drifted-but-canonical
    ("r-engaged", "engaged"),  # dashboard writer
    ("r-empty", ""),  # '' → NULL
    ("r-replied", "replied"),  # legacy MCP passthrough → 'useful'
    ("r-junk", "delivered"),  # lifecycle junk → NULL
    ("r-null", None),
]


async def _seeded_db(tmp_path):
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    db.row_factory = aiosqlite.Row
    # Full schema first (_migrate_add_columns touches many tables), then
    # swap outreach_history for the exact PRE-fix live shape.
    await create_all_tables(db)
    await db.execute("DROP TABLE outreach_history")
    await db.execute(_OLD_DDL)
    for rid, outcome in _SEED:
        await db.execute(
            "INSERT INTO outreach_history (id, signal_type, topic, category,"
            " salience_score, channel, message_content, created_at, engagement_outcome)"
            " VALUES (?, 't', 't', 'insight', 0.5, 'telegram', 'm',"
            " '2026-07-01T00:00:00+00:00', ?)",
            (rid, outcome),
        )
    await db.commit()
    return db


async def _outcome(db, rid):
    cur = await db.execute("SELECT engagement_outcome FROM outreach_history WHERE id = ?", (rid,))
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_rebuild_enforces_and_normalizes(tmp_path):
    db = await _seeded_db(tmp_path)
    try:
        await _migrate_add_columns(db)

        # row count preserved
        cur = await db.execute("SELECT COUNT(*) FROM outreach_history")
        assert (await cur.fetchone())[0] == len(_SEED)

        # canonical values survive untouched
        for rid, expected in [
            ("r-useful", "useful"),
            ("r-ignored", "ignored"),
            ("r-ambivalent", "ambivalent"),
            ("r-acked", "acknowledged"),
            ("r-engaged", "engaged"),
        ]:
            assert await _outcome(db, rid) == expected

        # normalization: '' and junk → NULL, legacy 'replied' → 'useful'
        assert await _outcome(db, "r-empty") is None
        assert await _outcome(db, "r-junk") is None
        assert await _outcome(db, "r-null") is None
        assert await _outcome(db, "r-replied") == "useful"

        # the CHECK now actually enforces (the whole point)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO outreach_history (id, signal_type, topic, category,"
                " salience_score, channel, message_content, created_at,"
                " engagement_outcome) VALUES ('bad', 't', 't', 'insight', 0.5,"
                " 'telegram', 'm', '2026-07-01T00:00:00+00:00', 'bogus')"
            )
        # NULL stays legal (via nullability, not IN-list membership)
        await db.execute(
            "INSERT INTO outreach_history (id, signal_type, topic, category,"
            " salience_score, channel, message_content, created_at)"
            " VALUES ('ok-null', 't', 't', 'insight', 0.5, 'telegram', 'm',"
            " '2026-07-01T00:00:00+00:00')"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rebuild_recreates_all_seven_indexes(tmp_path):
    db = await _seeded_db(tmp_path)
    try:
        await _migrate_add_columns(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='outreach_history' AND sql IS NOT NULL"
        )
        names = {row[0] for row in await cur.fetchall()}
        assert names == {
            "idx_outreach_channel",
            "idx_outreach_category",
            "idx_outreach_delivered",
            "idx_outreach_outcome",
            "idx_outreach_dedup",
            "idx_outreach_content_hash",
            "idx_outreach_person",
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rebuild_is_idempotent(tmp_path):
    db = await _seeded_db(tmp_path)
    try:
        await _migrate_add_columns(db)
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE name='outreach_history'")
        first = (await cur.fetchone())[0]
        await _migrate_add_columns(db)
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE name='outreach_history'")
        assert (await cur.fetchone())[0] == first
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_final_ddl_preserves_all_chain_probe_fragments(tmp_path):
    """THE DOWNGRADE-LOOP LOCK. _migrate_add_columns' earlier outreach_history
    rebuilds probe these exact DDL fragments on every boot; if the enforcing
    rebuild's DDL (or the canonical _tables.py DDL) loses one, the matching
    older rebuild re-fires next boot and reinstalls the no-op CHECK."""
    fragments = [
        "'digest', 'surplus', 'approval'",  # rebuild 1 ('approval')
        "'approval', 'content'",  # rebuild 2 ('content')
        "'content', 'notification'",  # rebuild 3 ('notification')
        "'engaged'",  # rebuild 4 (this fix)
    ]
    db = await _seeded_db(tmp_path)
    try:
        await _migrate_add_columns(db)
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE name='outreach_history'")
        ddl = (await cur.fetchone())[0]
        for frag in fragments:
            assert frag in ddl, f"rebuilt DDL lost probe fragment {frag!r}"
    finally:
        await db.close()

    # the fresh-install canonical DDL must satisfy the same probes
    from genesis.db.schema import TABLES

    canonical = TABLES["outreach_history"]
    for frag in fragments:
        assert frag in canonical, f"_tables.py DDL lost probe fragment {frag!r}"


def test_vocabulary_single_source_matches_check():
    """ENGAGEMENT_OUTCOMES (the edge-validation set) must exactly match the
    enforcing CHECK's vocabulary in the canonical DDL."""
    import re

    from genesis.db.schema import TABLES
    from genesis.outreach.types import ENGAGEMENT_OUTCOMES

    ddl = TABLES["outreach_history"]
    m = re.search(r"engagement_outcome IS NULL OR engagement_outcome IN \(([^)]+)\)", ddl)
    assert m, "enforcing engagement_outcome CHECK missing from canonical DDL"
    in_check = set(re.findall(r"'(\w+)'", m.group(1)))
    assert in_check == set(ENGAGEMENT_OUTCOMES)
