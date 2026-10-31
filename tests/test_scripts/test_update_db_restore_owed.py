"""A rollback restores the pre-update database only when the update changed it.

`_measure_db_restore_owed` (scripts/update.sh, the `db-restore-owed` block)
compares the restore source with the live database on DDL, user_version and the
applied numbered migrations. Before it, a flag set whenever the migration runner
was INVOKED owed the restore on every update past that step: on 2026-10-31 a run
with no pending migration restored a 20-minute-old snapshot and discarded every
write since. Predicting the answer from the merged paths was the next idea; that
update changed one SQL comment in db/schema/, so a path rule restores too.

The block runs here exactly as update.sh ships it, under bash with the script's
own `set -Eeuo pipefail`, against real SQLite files. The headline cases replay the
incident's shape with the real create_all_tables.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"


def _block() -> str:
    m = re.search(
        r"# BEGIN db-restore-owed[^\n]*\n(.*?)# END db-restore-owed",
        UPDATE.read_text(),
        re.DOTALL,
    )
    assert m, "db-restore-owed block not found in update.sh"
    return m.group(1)


def _measure(
    db: Path, *, snapshot_taken: bool = True, code_action: str = "reset", server_down: str = "true"
) -> tuple[str, str]:
    """Run the block as _do_rollback calls it. Defaults: this run's code was rolled
    back (`reset`) with the server confirmed down, the case that measures."""
    script = (
        "set -Eeuo pipefail\n"
        f'DB_FILE="{db}"\n'
        f"DB_SNAPSHOT_TAKEN={1 if snapshot_taken else 0}\n"
        "DB_RESTORE_OWED=unset\n"
        + _block()
        + f"\n_measure_db_restore_owed {code_action} {server_down}\n"
        'echo "OWED=$DB_RESTORE_OWED"\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    owed = re.search(r"^OWED=(.*)$", proc.stdout, re.M)
    assert owed, proc.stdout
    return owed.group(1), proc.stdout


def _snapshot(db: Path) -> Path:
    """Publish the restore source the way update.sh does: self-contained, DELETE mode."""
    dst = Path(f"{db}.pre-update.peer-restore")
    src = sqlite3.connect(db)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.execute("PRAGMA journal_mode=DELETE")
    out.close()
    src.close()
    return dst


def _live(tmp_path: Path) -> Path:
    db = tmp_path / "genesis.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    con.execute(
        "CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL, duration_ms INTEGER)"
    )
    con.execute("INSERT INTO schema_migrations VALUES ('0001', 'base', '2026-01-01', 1)")
    con.commit()
    con.close()
    return db


def _exec(db: Path, *sql: str) -> None:
    con = sqlite3.connect(db)
    for stmt in sql:
        con.execute(stmt)
    con.commit()
    con.close()


# -- the incident's shape, with the real schema code -------------------------


def _real_schema(db: Path) -> None:
    from genesis.db.connection import get_db
    from genesis.db.schema import create_all_tables

    async def run() -> None:
        conn = await get_db(db)
        try:
            await create_all_tables(conn)
            await conn.commit()
        finally:
            await conn.close()

    asyncio.run(run())


def test_rerunning_the_real_schema_code_keeps_the_database(tmp_path):
    """The 2026-10-31 shape: the new code's create_all_tables ran over the
    pre-update database, changed no schema, and the server then wrote rows.
    Nothing the old code depends on changed, so the writes are kept."""
    db = tmp_path / "genesis.db"
    _real_schema(db)
    _snapshot(db)
    _real_schema(db)  # bootstrap / server boot of the new code, idempotent
    _exec(
        db,
        "INSERT INTO events (id, timestamp, subsystem, severity, event_type, message)"
        " VALUES ('e1', 't', 'server', 'info', 'boot', 'written after the snapshot')",
    )
    owed, out = _measure(db)
    assert owed == "0", out
    assert "unchanged since the snapshot" in out


def test_a_real_schema_with_one_added_column_owes_the_restore(tmp_path):
    db = tmp_path / "genesis.db"
    _real_schema(db)
    _snapshot(db)
    _exec(db, "ALTER TABLE events ADD COLUMN probe TEXT")
    owed, out = _measure(db)
    assert owed == "1", out
    assert "events" in out


# -- each axis the comparison covers ----------------------------------------


def test_identical_database_with_uncheckpointed_wal_rows_is_kept(tmp_path):
    db = _live(tmp_path)
    _snapshot(db)
    con = sqlite3.connect(db)  # held open: the rows stay in the WAL
    con.execute("INSERT INTO notes (body) VALUES ('written after the snapshot')")
    con.commit()
    assert Path(f"{db}-wal").stat().st_size > 0
    owed, out = _measure(db)
    con.close()
    assert owed == "0", out


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        ("ALTER TABLE notes ADD COLUMN tag TEXT", "notes"),
        ("CREATE TABLE extra (id INTEGER)", "extra"),
        ("CREATE INDEX idx_notes_body ON notes(body)", "idx_notes_body"),
        ("CREATE TRIGGER trg AFTER INSERT ON notes BEGIN SELECT 1; END", "trg"),
        ("PRAGMA user_version = 7", "user_version 0 -> 7"),
        (
            "INSERT INTO schema_migrations VALUES ('0002', 'rename', '2026-01-02', 1)",
            "1 applied migration(s) differ",
        ),
    ],
)
def test_any_change_the_old_code_depends_on_owes_the_restore(tmp_path, change, expect):
    db = _live(tmp_path)
    _snapshot(db)
    _exec(db, change)
    owed, out = _measure(db)
    assert owed == "1", out
    assert expect in out, out


def test_a_rebuilt_table_with_a_tightened_check_owes_the_restore(tmp_path):
    """create_all_tables' rebuild pattern: same name, new DDL."""
    db = _live(tmp_path)
    _snapshot(db)
    _exec(
        db,
        "CREATE TABLE notes_new (id INTEGER PRIMARY KEY, body TEXT CHECK (body <> ''))",
        "INSERT INTO notes_new SELECT * FROM notes",
        "DROP TABLE notes",
        "ALTER TABLE notes_new RENAME TO notes",
    )
    owed, out = _measure(db)
    assert owed == "1", out


def test_rows_alone_do_not_owe_the_restore(tmp_path):
    db = _live(tmp_path)
    _snapshot(db)
    _exec(
        db,
        "INSERT INTO notes (body) VALUES ('a')",
        "DELETE FROM notes WHERE body = 'a'",
        "INSERT INTO notes (body) VALUES ('b')",
    )
    owed, _ = _measure(db)
    assert owed == "0"


def test_a_database_without_migration_tracking_compares_its_ddl(tmp_path):
    db = tmp_path / "genesis.db"
    _exec(db, "CREATE TABLE notes (id INTEGER)")
    _snapshot(db)
    assert _measure(db)[0] == "0"


# -- cannot measure: restore, loudly ----------------------------------------


def test_a_missing_restore_source_owes_the_restore(tmp_path):
    db = _live(tmp_path)
    owed, out = _measure(db)
    assert owed == "1", out
    assert "Could not compare" in out


def test_an_unreadable_live_database_owes_the_restore(tmp_path):
    db = _live(tmp_path)
    _snapshot(db)
    db.write_bytes(b"not a database" * 100)
    for side in ("-wal", "-shm"):
        Path(f"{db}{side}").unlink(missing_ok=True)
    owed, out = _measure(db)
    assert owed == "1", out
    assert "Could not compare" in out


@pytest.mark.parametrize("odd", ["a?b", "a#b", "a%41b", "a b"])
def test_an_install_path_with_uri_characters_is_still_compared(tmp_path, odd):
    """A raw "file:{path}" URI cut the path at '?' or '#' and dropped mode=ro;
    both sides then opened as the same empty file and compared identical."""
    root = tmp_path / odd
    root.mkdir()
    db = _live(root)
    _snapshot(db)
    _exec(db, "CREATE TABLE extra (id INTEGER)")
    owed, out = _measure(db)
    assert owed == "1", out
    assert "extra" in out, out
    assert sorted(p.name for p in tmp_path.iterdir()) == [odd], "nothing may be created outside"


def test_an_empty_database_is_never_called_identical(tmp_path):
    db = tmp_path / "genesis.db"
    db.touch()
    Path(f"{db}.pre-update.peer-restore").touch()
    owed, out = _measure(db)
    assert owed == "1", out
    assert "Could not compare" in out


def test_no_snapshot_and_no_database_owes_nothing(tmp_path):
    """The snapshot step skips only a database that does not exist yet."""
    owed, out = _measure(tmp_path / "genesis.db", snapshot_taken=False)
    assert owed == "0", out


def test_no_snapshot_but_a_database_now_owes_the_restore(tmp_path):
    """No snapshot was taken because no database existed; one exists now, so this
    update created it. The restore is owed (and cannot happen, which the decision
    reports as incomplete). A stale source file from an earlier run is not read."""
    db = _live(tmp_path)
    stale = _snapshot(db)
    owed, out = _measure(db, snapshot_taken=False)
    assert owed == "1", out
    assert stale.exists()


@pytest.mark.parametrize("code_action", ["none", "untouched"])
def test_a_rollback_this_runs_code_never_ran_owes_nothing(tmp_path, code_action):
    """`none`: the checkout is at the rollback tag; `untouched`: this run never
    merged and someone else moved the checkout. Either way this update did not
    change the database, and a restore under someone else's code would put it on
    an old schema."""
    db = _live(tmp_path)
    _snapshot(db)
    _exec(db, "ALTER TABLE notes ADD COLUMN tag TEXT")
    owed, out = _measure(db, code_action=code_action)
    assert owed == "0", out


def test_a_server_not_confirmed_down_owes_the_restore(tmp_path):
    """A running server could change the database after it is measured."""
    db = _live(tmp_path)
    _snapshot(db)
    owed, out = _measure(db, server_down="false")
    assert owed == "1", out
    assert "not confirmed down" in out


def test_the_comparison_runs_on_system_python_not_the_venv():
    """The venv may be the thing that broke; the worker must not depend on it."""
    block = _block()
    assert 'python3 -I -S - "$DB_FILE.pre-update.peer-restore" "$DB_FILE"' in block
    assert "VENV_DIR" not in block


def test_the_rollback_measures_immediately_before_deciding():
    text = UPDATE.read_text()
    body = text[text.index("_do_rollback() {") :]
    measure = body.index('    _measure_db_restore_owed "$code_action" "$server_down"\n')
    decide = body.index("# BEGIN rollback-db-decision")
    assert measure < decide
    assert body[measure:decide].count("\n") <= 1, "nothing may run between measuring and deciding"


def test_no_step_sets_the_flag_outside_the_measurement():
    text = UPDATE.read_text()
    block = _block()
    outside = text.replace(block, "")
    assignments = re.findall(r"^\s*DB_RESTORE_OWED=\S*", outside, re.M)
    assert assignments == ["DB_RESTORE_OWED=0"], (
        f"only the initialisation may assign the flag outside the measurement: {assignments}"
    )
    assert "MIGRATIONS_RAN" not in text


# -- the measurement driving the shipped decision ---------------------------


def _decide(db: Path, *, snapshot_taken: bool = True) -> str:
    """The measurement and the rollback-db-decision block, as _do_rollback runs
    them for a run whose code was rolled back with the server down."""
    m = re.search(
        r"# BEGIN rollback-db-decision[^\n]*\n(.*?)    # END rollback-db-decision",
        UPDATE.read_text(),
        re.DOTALL,
    )
    assert m
    script = (
        "set -Eeuo pipefail\n"
        f'DB_FILE="{db}"\nDB_SNAPSHOT_TAKEN={1 if snapshot_taken else 0}\nDB_RESTORE_OWED=0\n'
        + _block()
        + "\nrollback() {\n    local code_action=reset code_kept=false server_down=true\n"
        '    _measure_db_restore_owed "$code_action" "$server_down"\n'
        + m.group(1)
        + '    echo "DB_OK=$db_ok"\n}\nrollback\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _rows(db: Path) -> list:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT body FROM notes ORDER BY id").fetchall()
    finally:
        con.close()


def test_an_unchanged_database_is_kept_with_its_writes(tmp_path):
    db = _live(tmp_path)
    _snapshot(db)
    _exec(db, "INSERT INTO notes (body) VALUES ('kept')")
    out = _decide(db)
    assert "DB_OK=true" in out and "Restored" not in out, out
    assert _rows(db) == [("kept",)]


def test_a_changed_database_is_restored_from_the_snapshot(tmp_path):
    db = _live(tmp_path)
    _snapshot(db)
    _exec(db, "ALTER TABLE notes ADD COLUMN tag TEXT", "INSERT INTO notes (body) VALUES ('lost')")
    out = _decide(db)
    assert "DB_OK=true" in out and "Restored pre-migration database snapshot" in out, out
    assert _rows(db) == []


def test_a_database_this_update_created_without_a_snapshot_is_reported_incomplete(tmp_path):
    db = _live(tmp_path)
    out = _decide(db, snapshot_taken=False)
    assert "DB_OK=false" in out and "no current DB snapshot exists" in out, out
