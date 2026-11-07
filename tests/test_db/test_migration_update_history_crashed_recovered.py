"""Migration 20261018175924 — ``update_history.status = 'crashed_recovered'``.

bootstrap.sh records the crash recovery of an interrupted update with
``status='crashed_recovered'``; after 0024 the CHECK allowed only
('success', 'failed', 'rolled_back', 'conflicts_pending'), so that INSERT raised
IntegrityError and the recovery was never recorded.

The pre-migration table is built by the REAL migrations that create and reshape
it (0001, then 0024), not by a hand-copied DDL, so the fixture cannot drift from
what an install actually carries. The last test runs the full migration chain
through the real runner, with HOME and secrets sandboxed to tmp (0086 is the one
migration that touches a user file), so nothing reads or writes the live install.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.migrations.runner import MigrationRunner

MIGRATION = "genesis.db.migrations.20261018175924_update_history_crashed_recovered_status"

_INSERT = (
    "INSERT INTO update_history (id, old_tag, new_tag, old_commit, new_commit, status, "
    "rollback_tag, failure_reason, degraded_subsystems, started_at, completed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# One row per status the 0024 table admits, each with every column populated.
_EXISTING = [
    (
        "r-success",
        "v1",
        "v2",
        "aaa",
        "bbb",
        "success",
        None,
        None,
        "check:x",
        "2026-10-01T00:00:00+00:00",
        "2026-10-01T00:05:00+00:00",
    ),
    (
        "r-failed",
        "v2",
        "v3",
        "bbb",
        "ccc",
        "failed",
        "pre-v3",
        "health check",
        None,
        "2026-10-02T00:00:00+00:00",
        "2026-10-02T00:05:00+00:00",
    ),
    (
        "r-rolled",
        "v3",
        "v4",
        "ccc",
        "ddd",
        "rolled_back",
        "pre-v4",
        "boom",
        None,
        "2026-10-03T00:00:00+00:00",
        "2026-10-03T00:05:00+00:00",
    ),
    (
        "r-conflict",
        "v4",
        "v5",
        "ddd",
        "eee",
        "conflicts_pending",
        "pre-v5",
        None,
        None,
        "2026-10-04T00:00:00+00:00",
        "2026-10-04T00:05:00+00:00",
    ),
]

_CRASH_ROW = (
    "r-crash",
    "v5",
    "",
    "eee",
    "",
    "crashed_recovered",
    "pre-v6",
    "Crashed in phase: merging",
    None,
    "2026-10-05T00:00:00+00:00",
    "2026-10-05T00:01:00+00:00",
)


async def _rows(conn: aiosqlite.Connection) -> list[tuple]:
    cursor = await conn.execute(
        "SELECT id, old_tag, new_tag, old_commit, new_commit, status, rollback_tag, "
        "failure_reason, degraded_subsystems, started_at, completed_at "
        "FROM update_history ORDER BY id"
    )
    return [tuple(r) for r in await cursor.fetchall()]


async def _ddl(conn: aiosqlite.Connection) -> str:
    cursor = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='update_history'"
    )
    return (await cursor.fetchone())[0]


@pytest.fixture()
async def conn(tmp_path):
    """update_history as the real 0001 + 0024 migrations leave it, with rows."""
    m0001 = importlib.import_module("genesis.db.migrations.0001_add_update_history")
    m0024 = importlib.import_module("genesis.db.migrations.0024_update_history_conflicts_status")
    async with aiosqlite.connect(str(tmp_path / "uh.db")) as db:
        await m0001.up(db)
        await m0024.up(db)
        for row in _EXISTING:
            await db.execute(_INSERT, row)
        await db.commit()
        yield db


@pytest.mark.asyncio
async def test_fixture_rejects_crashed_recovered_before_migration(conn) -> None:
    """The defect: the 0024 table refuses the status bootstrap writes."""
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(_INSERT, _CRASH_ROW)


@pytest.mark.asyncio
async def test_crashed_recovered_accepted_and_rows_survive(conn) -> None:
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    await conn.commit()

    await conn.execute(_INSERT, _CRASH_ROW)
    await conn.commit()

    assert await _rows(conn) == sorted([*_EXISTING, _CRASH_ROW])


@pytest.mark.asyncio
async def test_unknown_status_still_rejected(conn) -> None:
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    bogus = ("r-bogus", *_CRASH_ROW[1:5], "bogus_status", *_CRASH_ROW[6:])
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(_INSERT, bogus)


@pytest.mark.asyncio
async def test_idempotent(conn) -> None:
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    await conn.execute(_INSERT, _CRASH_ROW)
    ddl_after_first = await _ddl(conn)
    await mod.up(conn)  # second run must be a no-op: nothing dropped or rebuilt
    await conn.commit()
    assert await _ddl(conn) == ddl_after_first
    assert await _rows(conn) == sorted([*_EXISTING, _CRASH_ROW])


@pytest.mark.asyncio
async def test_install_added_index_and_trigger_survive(conn) -> None:
    """An index or trigger an install put on the table is recreated, not lost."""
    await conn.execute("CREATE INDEX idx_uh_extra ON update_history(started_at)")
    await conn.execute("CREATE TABLE uh_audit (id TEXT)")
    await conn.execute(
        "CREATE TRIGGER trg_uh_audit AFTER INSERT ON update_history "
        "BEGIN INSERT INTO uh_audit VALUES (NEW.id); END"
    )
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    cursor = await conn.execute(
        "SELECT type, name FROM sqlite_master WHERE tbl_name='update_history' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY name"
    )
    assert [tuple(r) for r in await cursor.fetchall()] == [
        ("index", "idx_uh_extra"),
        ("trigger", "trg_uh_audit"),
    ]
    await conn.execute(_INSERT, _CRASH_ROW)
    cursor = await conn.execute("SELECT id FROM uh_audit")
    assert [r[0] for r in await cursor.fetchall()] == ["r-crash"]


@pytest.mark.asyncio
async def test_refuses_an_unknown_live_column(conn) -> None:
    """A column the rebuild target does not declare raises instead of being dropped."""
    await conn.execute("ALTER TABLE update_history ADD COLUMN install_note TEXT")
    mod = importlib.import_module(MIGRATION)
    with pytest.raises(RuntimeError, match="install_note"):
        await mod.up(conn)
    assert len(await _rows(conn)) == len(_EXISTING)


@pytest.mark.asyncio
async def test_refuses_an_existing_scratch_table(conn) -> None:
    """The scratch name belongs to whoever created it; the migration never drops it."""
    await conn.execute("CREATE TABLE update_history_new (keep TEXT)")
    await conn.execute("INSERT INTO update_history_new VALUES ('operator data')")
    mod = importlib.import_module(MIGRATION)
    with pytest.raises(RuntimeError, match="update_history_new"):
        await mod.up(conn)
    cursor = await conn.execute("SELECT keep FROM update_history_new")
    assert [r[0] for r in await cursor.fetchall()] == ["operator data"]


@pytest.mark.asyncio
async def test_absent_table_is_a_noop(tmp_path) -> None:
    mod = importlib.import_module(MIGRATION)
    async with aiosqlite.connect(str(tmp_path / "empty.db")) as db:
        await mod.up(db)
        await mod.down(db)
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE name='update_history'")
        assert await cursor.fetchone() is None


@pytest.mark.asyncio
async def test_down_keeps_crash_rows_as_failed(conn) -> None:
    """down() restores the 0024 constraint without losing a recorded crash."""
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    await conn.execute(_INSERT, _CRASH_ROW)
    await mod.down(conn)
    await conn.commit()

    assert "'crashed_recovered'" not in await _ddl(conn)
    rows = {r[0]: r for r in await _rows(conn)}
    assert rows["r-crash"][5] == "failed"
    assert rows["r-crash"][7] == "Crashed in phase: merging"
    assert len(rows) == len(_EXISTING) + 1
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(_INSERT, ("r-crash-2", *_CRASH_ROW[1:]))
    await mod.down(conn)  # already reverted: no-op


@pytest.mark.asyncio
async def test_full_migration_chain_accepts_crashed_recovered(tmp_path, monkeypatch) -> None:
    """Every real migration, run in order by the real runner, admits the status."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "isolated-secrets.env"))
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    async with aiosqlite.connect(str(tmp_path / "chain.db")) as db:
        results = await MigrationRunner(db).run_pending()
        assert results and all(r.success for r in results)
        await db.execute(_INSERT, _CRASH_ROW)
        await db.commit()
        cursor = await db.execute("SELECT status FROM update_history WHERE id='r-crash'")
        assert (await cursor.fetchone())[0] == "crashed_recovered"


# ── Install-added schema the fixed rebuild template would lose (PR review) ──


async def _recreate_with(conn: aiosqlite.Connection, old: str | None, new: str) -> None:
    """Recreate update_history from its live DDL with one textual edit, rows kept.

    ``old=None`` appends ``new`` as a table-level clause before the closing paren.
    """
    ddl = await _ddl(conn)
    if old is None:
        cut = ddl.rindex(")")
        edited = ddl[:cut].rstrip() + ",\n " + new + "\n)"
    else:
        assert old in ddl, f"fixture edit anchor {old!r} not in live DDL"
        edited = ddl.replace(old, new, 1)
    rows = await _rows(conn)
    await conn.execute("DROP TABLE update_history")
    await conn.execute(edited)
    for row in rows:
        await conn.execute(_INSERT, row)
    await conn.commit()


@pytest.mark.parametrize(
    ("old", "new", "named"),
    [
        (None, "UNIQUE (old_commit, started_at)", "UNIQUE"),
        (None, "CHECK (length(id) > 0)", "CHECK"),
        ("rollback_tag TEXT", "rollback_tag TEXT DEFAULT 'none'", "DEFAULT"),
        ("failure_reason TEXT", "failure_reason TEXT COLLATE NOCASE", "COLLATE"),
        (None, "FOREIGN KEY (rollback_tag) REFERENCES tags(name)", "FOREIGN KEY"),
    ],
    ids=["unique", "extra-check", "default", "collate", "own-foreign-key"],
)
@pytest.mark.asyncio
async def test_refuses_a_constraint_the_rebuild_would_drop(conn, old, new, named) -> None:
    await _recreate_with(conn, old, new)
    ddl_before = await _ddl(conn)
    mod = importlib.import_module(MIGRATION)
    with pytest.raises(RuntimeError, match="Nothing was changed") as exc:
        await mod.up(conn)
    assert named in str(exc.value)
    assert await _ddl(conn) == ddl_before
    assert await _rows(conn) == sorted(_EXISTING)


@pytest.mark.asyncio
async def test_refuses_a_cascading_child_with_foreign_keys_on(conn) -> None:
    """DROP TABLE under foreign_keys=ON deletes child rows: refuse first."""
    await conn.execute(
        "CREATE TABLE uh_child (uid TEXT REFERENCES update_history(id) ON DELETE CASCADE)"
    )
    await conn.execute("INSERT INTO uh_child VALUES ('r-success')")
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys = ON")
    mod = importlib.import_module(MIGRATION)
    with pytest.raises(RuntimeError, match="uh_child") as exc:
        await mod.up(conn)
    assert "Nothing was changed" in str(exc.value)
    await conn.rollback()
    cursor = await conn.execute("SELECT uid FROM uh_child")
    assert [r[0] for r in await cursor.fetchall()] == ["r-success"]
    assert "'crashed_recovered'" not in await _ddl(conn)


@pytest.mark.asyncio
async def test_a_view_over_the_table_survives(conn) -> None:
    await conn.execute("CREATE VIEW recent_updates AS SELECT id, status FROM update_history")
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    await conn.execute(_INSERT, _CRASH_ROW)
    cursor = await conn.execute("SELECT status FROM recent_updates WHERE id = 'r-crash'")
    assert (await cursor.fetchone())[0] == "crashed_recovered"


@pytest.mark.asyncio
async def test_a_trigger_on_another_table_naming_update_history_is_recreated(conn) -> None:
    await conn.execute("CREATE TABLE ping (x TEXT)")
    await conn.execute(
        "CREATE TRIGGER trg_ping AFTER INSERT ON ping BEGIN "
        "INSERT INTO update_history (id, old_tag, new_tag, old_commit, new_commit, status, "
        "started_at, completed_at) VALUES (NEW.x, '', '', '', '', 'crashed_recovered', "
        "'2026-10-06T00:00:00+00:00', '2026-10-06T00:00:01+00:00'); END"
    )
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    assert [r[0] for r in await cursor.fetchall()] == ["trg_ping"]
    await conn.execute("INSERT INTO ping VALUES ('via-trigger')")
    cursor = await conn.execute("SELECT status FROM update_history WHERE id = 'via-trigger'")
    assert (await cursor.fetchone())[0] == "crashed_recovered"


@pytest.mark.asyncio
async def test_the_literal_in_another_clause_does_not_skip_the_migration(conn) -> None:
    """The skip test reads the status CHECK, not the whole DDL: a DEFAULT naming
    'crashed_recovered' beside a still-narrow status CHECK must not be recorded
    as migrated. Here the rebuild refuses the DEFAULT it would drop, loudly."""
    await _recreate_with(conn, "failure_reason TEXT", "failure_reason TEXT DEFAULT 'crashed_recovered'")
    mod = importlib.import_module(MIGRATION)
    with pytest.raises(RuntimeError, match="Nothing was changed"):
        await mod.up(conn)


@pytest.mark.asyncio
async def test_the_literal_in_a_comment_does_not_skip_the_migration(conn) -> None:
    await _recreate_with(conn, "rollback_tag TEXT", "rollback_tag TEXT -- not 'crashed_recovered'\n")
    mod = importlib.import_module(MIGRATION)
    await mod.up(conn)
    await conn.execute(_INSERT, _CRASH_ROW)
    await conn.commit()
    assert _CRASH_ROW in await _rows(conn)


@pytest.mark.asyncio
async def test_down_reads_the_status_check_too(conn) -> None:
    """down() on a narrow table that mentions the literal elsewhere is a no-op."""
    await _recreate_with(conn, "rollback_tag TEXT", "rollback_tag TEXT -- not 'crashed_recovered'\n")
    ddl_before = await _ddl(conn)
    mod = importlib.import_module(MIGRATION)
    await mod.down(conn)
    assert await _ddl(conn) == ddl_before
