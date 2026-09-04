"""Migration 0095 — widen session_ledger.added_by for the ambient extractor.

Covers the rebuild itself (rows, constraint, indexes, idempotency) and, more
importantly, the DRIFT that made this migration necessary in the first place.

The allowed-provenance set lives in THREE places -- the fresh-install schema,
this migration's rebuilt DDL, and the Python `VALID_ADDED_BY` -- and nothing
mechanically tied them together before. That is how a value can be accepted by
one layer and rejected by another, which is exactly the failure a new install
would hit first. The drift tests below pin all three to each other.
"""

from __future__ import annotations

import importlib
import re

import aiosqlite
import pytest

from genesis.db.crud.session_charters import VALID_ADDED_BY
from genesis.db.schema._tables import TABLES

M90 = importlib.import_module("genesis.db.migrations.0095_session_ledger_ambient_extractor")

EXTRACTOR = "ambient_ledger_extractor"

# The pre-0095 table, verbatim, so the migration is exercised against the shape
# it will actually meet on a real install rather than against its own output.
_OLD_DDL = """
    CREATE TABLE session_ledger (
        id          TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL,
        text        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','in_progress','done','absorbed','dropped')),
        source_ref  TEXT,
        added_by    TEXT NOT NULL DEFAULT 'foreground'
                    CHECK(added_by IN ('foreground','ambient','pulse')),
        evidence    TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT
    )
"""


def _allowed_from_ddl(ddl: str) -> set[str]:
    """The added_by CHECK's value set, parsed out of a CREATE TABLE statement."""
    m = re.search(r"CHECK\(added_by IN \(([^)]*)\)\)", ddl, re.S)
    assert m, f"no added_by CHECK found in DDL:\n{ddl}"
    return {v.strip().strip("'") for v in m.group(1).split(",") if v.strip()}


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def _seed_old(db):
    await db.execute(_OLD_DDL)
    await db.execute(
        "CREATE INDEX idx_session_ledger_session ON session_ledger(session_id, status)"
    )
    for i, who in enumerate(("foreground", "ambient", "pulse")):
        await db.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, source_ref, "
            "added_by, evidence, created_at, updated_at) "
            "VALUES (?, 's', ?, 'open', 'ref', ?, 'ev', 'created', 'updated')",
            (f"row{i}", f"text {i}", who),
        )
    await db.commit()


async def _ddl(db) -> str:
    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_ledger'"
    )
    return (await cur.fetchone())[0]


class TestTheRebuild:
    @pytest.mark.asyncio
    async def test_widens_the_constraint(self, db):
        await _seed_old(db)
        assert EXTRACTOR not in await _ddl(db)
        await M90.up(db)
        assert EXTRACTOR in await _ddl(db)

    @pytest.mark.asyncio
    async def test_preserves_every_row_and_column(self, db):
        """A rebuild copies data by hand, so a dropped column is silent."""
        await _seed_old(db)
        cur = await db.execute("SELECT * FROM session_ledger ORDER BY id")
        before = [tuple(r) for r in await cur.fetchall()]

        await M90.up(db)

        cur = await db.execute("SELECT * FROM session_ledger ORDER BY id")
        after = [tuple(r) for r in await cur.fetchall()]
        assert after == before, "the rebuild lost or reordered data"

    @pytest.mark.asyncio
    async def test_restores_the_index(self, db):
        """DROP TABLE takes the table's indexes with it."""
        await _seed_old(db)
        await M90.up(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='session_ledger' AND name NOT LIKE 'sqlite_%'"
        )
        assert {r[0] for r in await cur.fetchall()} == {"idx_session_ledger_session"}

    @pytest.mark.asyncio
    async def test_accepts_the_new_value_and_still_rejects_a_bogus_one(self, db):
        """The positive control.

        A WIDENED constraint and a DROPPED constraint both accept the new value.
        Only the bogus rejection distinguishes them, so asserting acceptance
        alone would pass just as happily against no constraint at all.
        """
        await _seed_old(db)
        await M90.up(db)

        await db.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, added_by, "
            "created_at) VALUES ('new', 's', 't', 'open', ?, 'c')",
            (EXTRACTOR,),
        )
        with pytest.raises(Exception, match="CHECK constraint failed"):
            await db.execute(
                "INSERT INTO session_ledger (id, session_id, text, status, added_by, "
                "created_at) VALUES ('bad', 's', 't', 'open', 'totally_bogus', 'c')"
            )

    @pytest.mark.asyncio
    async def test_is_idempotent(self, db):
        await _seed_old(db)
        await M90.up(db)
        first = await _ddl(db)
        cur = await db.execute("SELECT * FROM session_ledger ORDER BY id")
        rows_first = [tuple(r) for r in await cur.fetchall()]

        await M90.up(db)

        assert await _ddl(db) == first
        cur = await db.execute("SELECT * FROM session_ledger ORDER BY id")
        assert [tuple(r) for r in await cur.fetchall()] == rows_first

    @pytest.mark.asyncio
    async def test_absent_table_is_a_no_op(self, db):
        """A fresh install creates the table from _tables.py; nothing to migrate."""
        await M90.up(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_ledger'"
        )
        assert await cur.fetchone() is None

    @pytest.mark.asyncio
    async def test_recovers_from_an_interrupted_prior_attempt(self, db):
        """A run that died between CREATE and RENAME leaves session_ledger_new."""
        await _seed_old(db)
        await db.execute("CREATE TABLE session_ledger_new (junk TEXT)")
        await db.commit()

        await M90.up(db)

        assert EXTRACTOR in await _ddl(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_ledger_new'"
        )
        assert await cur.fetchone() is None, "temp table left behind"


class TestTheThreeSitesCannotDrift:
    """The set is written in three places. Nothing tied them together before.

    A value present in one and missing from another means a row that one layer
    writes and another refuses -- and on a FRESH install the schema is the only
    one of the three that runs, so a drift there ships broken to new users while
    every migrated install looks fine.
    """

    def test_fresh_install_schema_matches_the_python_allow_list(self):
        allowed = _allowed_from_ddl(TABLES["session_ledger"])
        assert allowed == set(VALID_ADDED_BY), (
            "the fresh-install schema and VALID_ADDED_BY disagree: "
            f"schema-only={allowed - set(VALID_ADDED_BY)} "
            f"python-only={set(VALID_ADDED_BY) - allowed}"
        )

    @pytest.mark.asyncio
    async def test_migrated_schema_matches_the_python_allow_list(self, db):
        await _seed_old(db)
        await M90.up(db)
        allowed = _allowed_from_ddl(await _ddl(db))
        assert allowed == set(VALID_ADDED_BY), (
            "a MIGRATED install and VALID_ADDED_BY disagree: "
            f"schema-only={allowed - set(VALID_ADDED_BY)} "
            f"python-only={set(VALID_ADDED_BY) - allowed}"
        )

    @pytest.mark.asyncio
    async def test_migrated_and_fresh_installs_agree(self, db):
        """The two paths must converge on the same constraint."""
        await _seed_old(db)
        await M90.up(db)
        assert _allowed_from_ddl(await _ddl(db)) == _allowed_from_ddl(TABLES["session_ledger"])

    def test_the_extractor_value_is_distinct_from_ambient(self):
        """Reusing 'ambient' would silently void the leak invariant.

        `_default_added_by()` returns 'ambient' for any dispatched CC session,
        and the shadow report's leak check keys on the extractor's value to
        assert it has written nothing live. One shared value makes that check
        unable to tell the two apart on the day it starts mattering.
        """
        assert EXTRACTOR in VALID_ADDED_BY
        assert "ambient" in VALID_ADDED_BY
        assert EXTRACTOR != "ambient"


class TestThePromotionStateColumn:
    """`promoted_item_id` on shadow events — the retryable-promotion sweep's
    state. NULL means unpromoted and therefore still retryable, so the column
    existing is what lets promotion failure cost nothing but time."""

    _EVENTS_DDL_PRE = """
        CREATE TABLE session_ledger_shadow_events (
            id              TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL,
            observed_at     TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            kind            TEXT NOT NULL CHECK(kind IN ('agreement','pivot')),
            text            TEXT NOT NULL,
            turn_ref        TEXT,
            quote_preview   TEXT,
            quote_hash      TEXT,
            quote_verified  INTEGER NOT NULL DEFAULT 0,
            match_kind      TEXT NOT NULL DEFAULT 'none'
                            CHECK(match_kind IN ('exact','fuzzy','none')),
            matched_item_id TEXT,
            match_score     REAL,
            duplicate_of    TEXT,
            mode            TEXT NOT NULL DEFAULT 'shadow'
        )
    """

    async def _cols(self, db) -> set[str]:
        cur = await db.execute("PRAGMA table_info(session_ledger_shadow_events)")
        return {r[1] for r in await cur.fetchall()}

    @pytest.mark.asyncio
    async def test_adds_the_column_and_preserves_existing_rows(self, db):
        await db.execute(self._EVENTS_DDL_PRE)
        await db.execute(
            "INSERT INTO session_ledger_shadow_events "
            "(id, run_id, observed_at, session_id, kind, text) "
            "VALUES ('e1', 'r1', 'now', 's', 'agreement', 'keep me')"
        )
        await db.commit()
        assert "promoted_item_id" not in await self._cols(db)

        await M90.up(db)

        assert "promoted_item_id" in await self._cols(db)
        cur = await db.execute("SELECT text, promoted_item_id FROM session_ledger_shadow_events")
        # tuple(): the shared `db` fixture sets a row_factory, so rows compare
        # by identity rather than value without it.
        rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [("keep me", None)], (
            "existing events must survive and read as unpromoted (retryable)"
        )

    @pytest.mark.asyncio
    async def test_is_idempotent(self, db):
        await db.execute(self._EVENTS_DDL_PRE)
        await db.commit()
        await M90.up(db)
        await M90.up(db)  # must not raise "duplicate column name"
        assert "promoted_item_id" in await self._cols(db)

    @pytest.mark.asyncio
    async def test_absent_events_table_is_a_no_op(self, db):
        """A fresh install creates it from _tables.py, already with the column."""
        await M90.up(db)
        assert await self._cols(db) == set()

    def test_fresh_install_schema_carries_the_column(self):
        """The drift this whole file exists to prevent, for the new column:
        migrated and fresh-install schemas must not disagree."""
        assert "promoted_item_id" in TABLES["session_ledger_shadow_events"]
