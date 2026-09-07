"""zero_drop_findings must be IDENTICAL on both build paths.

A fresh install builds its schema from ``db/schema/_tables.py``; an existing
one gets the table from the migration. Two hand-maintained copies of one DDL is
how a CHECK constraint or a UNIQUE key silently exists on only one of them —
and here the UNIQUE(class, branch) key IS the finding's identity, so a path
missing it would let the same standing condition become a new row on every
sweep, resetting recurrence and disarming escalation on exactly the installs
that upgraded rather than reinstalled.
"""

import importlib

import aiosqlite
import pytest

from genesis.db.schema._tables import INDEXES, TABLES

MIGRATION = "20260905215957_zero_drop_findings"
TABLE = "zero_drop_findings"


def _migration():
    return importlib.import_module(f"genesis.db.migrations.{MIGRATION}")


def _normalize(sql: str) -> str:
    """Whitespace-insensitive comparison of two SQL bodies.

    The two literals sit at different indentation levels by house convention
    (the migration's closing quotes are at column 0, the dict entry's are
    indented), so 'byte-identical' means the SQL, not the surrounding python.
    """
    return " ".join(sql.split())


def test_table_ddl_matches_between_build_paths():
    assert _normalize(_migration()._TABLE_DDL) == _normalize(TABLES[TABLE])


def test_index_ddl_matches_between_build_paths():
    """Compares the IMPORTED constants, not a regex over the source.

    A source scrape reads only the first fragment of a statement split across
    adjacent string literals for line length — so it would compare half an
    index DDL against a whole one and report drift that is not there (it did,
    on the first run of this file). Importing compares the values python
    actually builds on each path.
    """
    mig_indexes = {_normalize(s) for s in _migration()._INDEX_DDL}
    tables_indexes = {_normalize(s) for s in INDEXES if "idx_zero_drop_findings" in s}
    assert len(tables_indexes) == 2, f"expected 2 indexes in _tables, got {tables_indexes}"
    assert mig_indexes == tables_indexes, (
        f"index DDL drift: migration={mig_indexes} tables={tables_indexes}"
    )


@pytest.fixture
async def legacy_conn(tmp_path):
    """A DB with the full schema MINUS this table — the upgrade path's start."""
    conn = await aiosqlite.connect(str(tmp_path / "legacy.db"))
    conn.row_factory = aiosqlite.Row
    try:
        from genesis.db.schema import create_all_tables

        await create_all_tables(conn)
        await conn.execute(f"DROP TABLE {TABLE}")
        await conn.commit()
        yield conn
    finally:
        await conn.close()


async def _table_names(conn) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in await cur.fetchall()}


async def test_up_creates_the_table_and_is_idempotent(legacy_conn):
    mig = importlib.import_module(f"genesis.db.migrations.{MIGRATION}")
    await mig.up(legacy_conn)
    await mig.up(legacy_conn)  # re-run: a retried migration must not explode
    await legacy_conn.commit()
    assert TABLE in await _table_names(legacy_conn)

    cur = await legacy_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_zero_drop%'"
    )
    assert len(await cur.fetchall()) == 2


async def test_down_removes_it_and_tolerates_absence(legacy_conn):
    mig = importlib.import_module(f"genesis.db.migrations.{MIGRATION}")
    await mig.up(legacy_conn)
    await mig.down(legacy_conn)
    await mig.down(legacy_conn)  # absent table: a no-op, not a crash
    await legacy_conn.commit()
    assert TABLE not in await _table_names(legacy_conn)


async def test_identity_is_enforced_by_the_schema_itself(db):
    """UNIQUE(class, branch) is the finding's identity. Without it the store's
    recurrence semantics are unenforceable at the DB level."""
    await db.execute(
        f"INSERT INTO {TABLE} (id, class, branch, status, first_seen_at, last_seen_at, "
        "created_at, updated_at) VALUES ('1','unpushed_branch','b','open','t','t','t','t')"
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            f"INSERT INTO {TABLE} (id, class, branch, status, first_seen_at, last_seen_at, "
            "created_at, updated_at) VALUES ('2','unpushed_branch','b','open','t','t','t','t')"
        )
    # A different CLASS on the same branch is a different finding.
    await db.execute(
        f"INSERT INTO {TABLE} (id, class, branch, status, first_seen_at, last_seen_at, "
        "created_at, updated_at) VALUES ('3','dirty_worktree','b','open','t','t','t','t')"
    )


@pytest.mark.parametrize("column,value", [("class", "made_up_class"), ("status", "snoozed")])
async def test_closed_sets_are_enforced_not_advisory(db, column, value):
    """class and status are closed sets. A typo'd class would create a finding
    no surface queries and no sweep ever resolves — invisible forever."""
    cols = {"class": "unpushed_branch", "status": "open"}
    cols[column] = value
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            f"INSERT INTO {TABLE} (id, class, branch, status, first_seen_at, last_seen_at, "
            "created_at, updated_at) VALUES ('x',?,'b',?,'t','t','t','t')",
            (cols["class"], cols["status"]),
        )
