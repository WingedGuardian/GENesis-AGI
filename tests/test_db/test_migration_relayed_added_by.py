"""Migration: widen session_ledger.added_by to admit 'relayed'.

Exercised against the two shapes it really meets: a table at the PREVIOUS
schema as a migrated install carries it (the precedent migration's output,
whose stored DDL names the table quoted after its RENAME), and the fresh-install
schema from _tables.py, which already carries the value.
"""

from __future__ import annotations

import importlib
import re

import aiosqlite
import pytest

from genesis.db.crud.session_charters import VALID_ADDED_BY
from genesis.db.schema._tables import TABLES

PREV = importlib.import_module(
    "genesis.db.migrations.20260904231054_session_ledger_ambient_extractor"
)
MIG = importlib.import_module(
    "genesis.db.migrations.20261028041420_session_ledger_relayed_added_by"
)

RELAYED = "relayed"

# The table as it stood before the precedent migration; running PREV over it
# yields the exact previous-schema shape a live install holds today.
_PRE_PREV_DDL = """
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

_COLS = (
    "id, session_id, text, status, source_ref, added_by, evidence, "
    "source_quote, created_at, updated_at"
)


def _allowed_from_ddl(ddl: str) -> set[str]:
    m = re.search(r"CHECK\(added_by IN \(([^)]*)\)\)", ddl, re.S)
    assert m, f"no added_by CHECK found in DDL:\n{ddl}"
    return {v.strip().strip("'") for v in m.group(1).split(",") if v.strip()}


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def _seed_previous_schema(db):
    """A table at the previous schema, with one row per then-valid value."""
    await db.execute(_PRE_PREV_DDL)
    await db.execute(
        "CREATE INDEX idx_session_ledger_session ON session_ledger(session_id, status)"
    )
    await PREV.up(db)
    for i, who in enumerate(("foreground", "ambient", "pulse", "ambient_ledger_extractor")):
        await db.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, source_ref, "
            "added_by, evidence, source_quote, created_at, updated_at) "
            "VALUES (?, 's', ?, 'open', 'ref', ?, 'ev', 'q', 'created', 'updated')",
            (f"row{i}", f"text {i}", who),
        )
    await db.commit()


async def _ddl(db) -> str:
    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_ledger'"
    )
    return (await cur.fetchone())[0]


async def _insert(db, item_id: str, added_by: str):
    await db.execute(
        "INSERT INTO session_ledger (id, session_id, text, status, added_by, "
        "created_at) VALUES (?, 's', 't', 'open', ?, 'c')",
        (item_id, added_by),
    )


class TestOnThePreviousSchema:
    @pytest.mark.asyncio
    async def test_previous_schema_refuses_relayed(self, db):
        """Guard-the-guard: the fixture really is the pre-change shape."""
        await _seed_previous_schema(db)
        assert '"session_ledger"' in await _ddl(db), "fixture is not the post-RENAME shape"
        with pytest.raises(Exception, match="CHECK constraint failed"):
            await _insert(db, "pre", RELAYED)

    @pytest.mark.asyncio
    async def test_accepts_relayed_and_still_rejects_a_bogus_value(self, db):
        """Acceptance alone would also pass against a DROPPED constraint."""
        await _seed_previous_schema(db)
        await MIG.up(db)
        await _insert(db, "new", RELAYED)
        with pytest.raises(Exception, match="CHECK constraint failed"):
            await _insert(db, "bad", "totally_bogus")
        assert _allowed_from_ddl(await _ddl(db)) == set(VALID_ADDED_BY)

    @pytest.mark.asyncio
    async def test_preserves_every_row_and_column(self, db):
        await _seed_previous_schema(db)
        cur = await db.execute(f"SELECT {_COLS} FROM session_ledger ORDER BY id")
        before = [tuple(r) for r in await cur.fetchall()]
        assert len(before) == 4
        await MIG.up(db)
        cur = await db.execute(f"SELECT {_COLS} FROM session_ledger ORDER BY id")
        assert [tuple(r) for r in await cur.fetchall()] == before

    @pytest.mark.asyncio
    async def test_restores_the_index_and_leaves_no_temp_table(self, db):
        await _seed_previous_schema(db)
        await MIG.up(db)
        cur = await db.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND (tbl_name='session_ledger' OR name='session_ledger_new')"
        )
        assert {tuple(r) for r in await cur.fetchall()} == {
            ("table", "session_ledger"),
            ("index", "idx_session_ledger_session"),
        }

    @pytest.mark.asyncio
    async def test_a_fork_added_column_index_and_trigger_survive(self, db):
        """The rebuild copies from the live DDL, so nothing it does not know is lost."""
        await _seed_previous_schema(db)
        await db.execute("ALTER TABLE session_ledger ADD COLUMN fork_notes TEXT DEFAULT 'x'")
        await db.execute("UPDATE session_ledger SET fork_notes='kept' WHERE id='row0'")
        await db.execute("CREATE INDEX idx_fork ON session_ledger(fork_notes)")
        await db.execute("CREATE TABLE fork_log (item TEXT)")
        await db.execute(
            "CREATE TRIGGER trg_fork AFTER INSERT ON session_ledger "
            "BEGIN INSERT INTO fork_log VALUES (NEW.id); END"
        )
        await db.commit()

        await MIG.up(db)

        cur = await db.execute("SELECT fork_notes FROM session_ledger WHERE id='row0'")
        assert (await cur.fetchone())[0] == "kept"
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name='session_ledger' "
            "AND type IN ('index','trigger')"
        )
        assert {r[0] for r in await cur.fetchall()} >= {"idx_fork", "trg_fork"}
        await _insert(db, "after", RELAYED)
        cur = await db.execute("SELECT item FROM fork_log")
        assert [r[0] for r in await cur.fetchall()] == ["after"]

    @pytest.mark.asyncio
    async def test_is_idempotent(self, db):
        await _seed_previous_schema(db)
        await MIG.up(db)
        first = await _ddl(db)
        await MIG.up(db)
        assert await _ddl(db) == first

    @pytest.mark.asyncio
    async def test_preserves_rowids(self, db):
        await _seed_previous_schema(db)
        cur = await db.execute("SELECT rowid, id FROM session_ledger ORDER BY id")
        before = [tuple(r) for r in await cur.fetchall()]
        await db.execute("DELETE FROM session_ledger WHERE id='row1'")  # leave a gap
        before = [r for r in before if r[1] != "row1"]
        await MIG.up(db)
        cur = await db.execute("SELECT rowid, id FROM session_ledger ORDER BY id")
        assert [tuple(r) for r in await cur.fetchall()] == before

    @pytest.mark.asyncio
    async def test_a_scratch_name_squatter_is_refused_not_dropped(self, db):
        """The runner rolls our own failed attempts back, so an object holding
        the scratch name is the operator's — never the migration's to drop."""
        await _seed_previous_schema(db)
        await db.execute("CREATE TABLE session_ledger_new (keep TEXT)")
        await db.execute("INSERT INTO session_ledger_new VALUES ('mine')")
        await db.commit()
        with pytest.raises(RuntimeError, match="session_ledger_new"):
            await MIG.up(db)
        cur = await db.execute("SELECT keep FROM session_ledger_new")
        assert [r[0] for r in await cur.fetchall()] == ["mine"]
        assert RELAYED not in _allowed_from_ddl(await _ddl(db))

    @pytest.mark.asyncio
    async def test_a_foreign_key_child_is_refused_before_any_cascade(self, db):
        """With foreign_keys=ON (how init_db opens the DB) DROP TABLE runs an
        implicit DELETE that would cascade into the child silently."""
        await _seed_previous_schema(db)
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            "CREATE TABLE fork_child (item TEXT REFERENCES session_ledger(id) ON DELETE CASCADE)"
        )
        await db.execute("INSERT INTO fork_child VALUES ('row0')")
        await db.commit()
        with pytest.raises(RuntimeError, match="fork_child"):
            await MIG.up(db)
        cur = await db.execute("SELECT COUNT(*) FROM fork_child")
        assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_a_view_and_another_tables_trigger_survive(self, db):
        """ALTER ... RENAME reparses the whole schema: a view or a trigger on
        ANOTHER table naming session_ledger would abort it if left in place."""
        await _seed_previous_schema(db)
        await db.execute("CREATE VIEW v_open AS SELECT id FROM session_ledger WHERE status='open'")
        await db.execute("CREATE TABLE other (x TEXT)")
        await db.execute(
            "CREATE TRIGGER trg_other AFTER INSERT ON other "
            "BEGIN UPDATE session_ledger SET evidence = NEW.x WHERE id = 'row0'; END"
        )
        await db.commit()

        await MIG.up(db)

        cur = await db.execute("SELECT COUNT(*) FROM v_open")
        assert (await cur.fetchone())[0] == 4
        await db.execute("INSERT INTO other VALUES ('fired')")
        cur = await db.execute("SELECT evidence FROM session_ledger WHERE id='row0'")
        assert (await cur.fetchone())[0] == "fired"

    @pytest.mark.asyncio
    async def test_a_comment_inside_the_value_list_is_refused(self, db):
        """Appending after a trailing `-- comment` would land inside it."""
        await db.execute(_PRE_PREV_DDL.replace("'pulse')", "'pulse' -- provenance\n)"))
        await db.commit()
        assert "-- provenance" in await _ddl(db)
        with pytest.raises(RuntimeError, match="not a shape"):
            await MIG.up(db)

    @pytest.mark.asyncio
    async def test_an_unrecognised_ddl_shape_raises_before_any_drop(self, db):
        """No guessing: an unparseable table is left exactly as it was."""
        await db.execute("CREATE TABLE session_ledger (id TEXT PRIMARY KEY, added_by TEXT)")
        await db.execute("INSERT INTO session_ledger VALUES ('keep', 'foreground')")
        await db.commit()
        with pytest.raises(RuntimeError, match="not a shape"):
            await MIG.up(db)
        cur = await db.execute("SELECT id FROM session_ledger")
        assert [r[0] for r in await cur.fetchall()] == ["keep"]


class TestFreshInstall:
    def test_fresh_schema_admits_relayed(self):
        assert RELAYED in _allowed_from_ddl(TABLES["session_ledger"])

    @pytest.mark.asyncio
    async def test_fresh_schema_is_a_no_op(self, db):
        await db.execute(TABLES["session_ledger"])
        await db.commit()
        before = await _ddl(db)
        await MIG.up(db)
        assert await _ddl(db) == before

    @pytest.mark.asyncio
    async def test_absent_table_is_a_no_op(self, db):
        await MIG.up(db)
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_ledger'"
        )
        assert await cur.fetchone() is None
