"""The 'marketing' outreach_history category rebuild (marketing Telegram topic).

The marketing campaign's tick digest routes via ``outreach_send(category=
'marketing', ...)`` so it lands in its OWN "Marketing" forum topic instead of
the shared Morning Reports topic. ``outreach_history.category`` carries a CHECK
constraint, so 'marketing' must be added to it — via the canonical ``_tables.py``
DDL (fresh installs) AND a guarded table-rebuild in the ``_migrate_add_columns``
chain (existing installs). This is rebuild #5, appended AFTER the enforcing
engagement_outcome rebuild (#4); it must preserve every earlier probe fragment
AND the enforcing engagement CHECK, or a later boot re-fires an older rebuild.
"""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from genesis.db.schema._migrations import _migrate_add_columns

# The current canonical shape a live install has AFTER the engagement rebuild
# (#4): 10-value category CHECK, ENFORCING engagement CHECK. Seeding this
# isolates the marketing rebuild (all four earlier probes already satisfied).
_POST_ENGAGEMENT_DDL = """
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
        engagement_outcome  TEXT CHECK (
            engagement_outcome IS NULL OR engagement_outcome IN (
            'useful', 'engaged', 'acted_on', 'acknowledged',
            'not_useful', 'ambivalent', 'ignored'
        )),
        engagement_signal   TEXT,
        prediction_error    REAL,
        created_at          TEXT NOT NULL
    )
"""

# The OLDEST pre-fix shape (10-value category CHECK, NO-OP engagement CHECK) —
# used to prove the whole chain converges to 'marketing' AND enforcing outcomes.
_OLDEST_DDL = """
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

_MARKETING_INSERT = (
    "INSERT INTO outreach_history (id, signal_type, topic, category,"
    " salience_score, channel, message_content, created_at)"
    " VALUES (?, 'marketing', 't', 'marketing', 0.0, 'telegram', 'm',"
    " '2026-09-04T00:00:00+00:00')"
)
_BOGUS_INSERT = (
    "INSERT INTO outreach_history (id, signal_type, topic, category,"
    " salience_score, channel, message_content, created_at)"
    " VALUES (?, 'x', 't', 'not_a_category', 0.0, 'telegram', 'm',"
    " '2026-09-04T00:00:00+00:00')"
)


async def _db_with(tmp_path, ddl):
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    await db.execute("DROP TABLE outreach_history")
    await db.execute(ddl)
    # A representative pre-existing row must survive the rebuild.
    await db.execute(
        "INSERT INTO outreach_history (id, signal_type, topic, category,"
        " salience_score, channel, message_content, created_at)"
        " VALUES ('pre', 'digest', 't', 'digest', 0.0, 'telegram', 'm',"
        " '2026-09-01T00:00:00+00:00')"
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_marketing_accepted_after_rebuild_from_enforcing_shape(tmp_path):
    """A post-engagement install gains the 'marketing' category, keeps enforcing."""
    db = await _db_with(tmp_path, _POST_ENGAGEMENT_DDL)
    try:
        await _migrate_add_columns(db)

        # Pre-existing row preserved.
        cur = await db.execute("SELECT COUNT(*) FROM outreach_history")
        assert (await cur.fetchone())[0] == 1

        # 'marketing' now inserts cleanly.
        await db.execute(_MARKETING_INSERT, ("mkt",))

        # The category CHECK still rejects garbage.
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(_BOGUS_INSERT, ("bad",))

        # The enforcing engagement CHECK was NOT downgraded by the rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO outreach_history (id, signal_type, topic, category,"
                " salience_score, channel, message_content, created_at,"
                " engagement_outcome) VALUES ('eo', 'x', 't', 'marketing', 0.0,"
                " 'telegram', 'm', '2026-09-04T00:00:00+00:00', 'bogus')"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_full_chain_converges_to_marketing_and_enforcing(tmp_path):
    """From the OLDEST shape, the whole chain lands 'marketing' + enforcing CHECK."""
    db = await _db_with(tmp_path, _OLDEST_DDL)
    try:
        await _migrate_add_columns(db)
        await db.execute(_MARKETING_INSERT, ("mkt",))
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(_BOGUS_INSERT, ("bad",))
        # enforcing engagement CHECK reached (the #4 fix survived #5)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO outreach_history (id, signal_type, topic, category,"
                " salience_score, channel, message_content, created_at,"
                " engagement_outcome) VALUES ('eo', 'x', 't', 'digest', 0.0,"
                " 'telegram', 'm', '2026-09-04T00:00:00+00:00', 'bogus')"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_install_accepts_marketing(tmp_path):
    """The canonical create_all_tables DDL accepts 'marketing' with no migration."""
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(str(tmp_path / "fresh.db"))
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    try:
        await db.execute(_MARKETING_INSERT, ("mkt",))
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(_BOGUS_INSERT, ("bad",))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_marketing_rebuild_idempotent(tmp_path):
    """Running the chain twice leaves the outreach_history DDL byte-identical."""
    db = await _db_with(tmp_path, _POST_ENGAGEMENT_DDL)
    try:
        await _migrate_add_columns(db)
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE name='outreach_history'")
        first = (await cur.fetchone())[0]
        await _migrate_add_columns(db)
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE name='outreach_history'")
        assert (await cur.fetchone())[0] == first
    finally:
        await db.close()


def test_every_outreach_category_is_in_the_db_check():
    """LOCK: every OutreachCategory enum value must be a legal category value in
    the canonical outreach_history CHECK — else outreach_send validates a value
    the delivery-log write then rejects. Catches 'added the enum, forgot the
    CHECK' for any future category, not just marketing."""
    import re

    from genesis.db.schema import TABLES
    from genesis.outreach.types import OutreachCategory

    ddl = TABLES["outreach_history"]
    m = re.search(r"category\s+TEXT NOT NULL CHECK \(category IN \(([^)]+)\)\)", ddl)
    assert m, "category CHECK missing from canonical outreach_history DDL"
    check_values = set(re.findall(r"'(\w+)'", m.group(1)))
    enum_values = {c.value for c in OutreachCategory}
    missing = enum_values - check_values
    assert not missing, f"OutreachCategory values absent from DB CHECK: {missing}"
