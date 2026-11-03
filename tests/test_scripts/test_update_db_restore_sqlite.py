"""The rollback restores the pre-update database through SQLite, never by cp.

On 2026-10-31 a rollback copied the snapshot over the live database and removed
its WAL while twelve Claude Code MCP servers held it open. Each idle WAL-mode
connection holds a SHARED lock for its whole life, so the restarted server could
not switch the restored rollback-journal file back to WAL ("database is locked")
and stayed down until the readers were killed by hand; the readers meanwhile kept
serving pre-restore pages.

`_restore_db_snapshot` (scripts/update.sh, `db-restore-through-sqlite`) runs here
exactly as shipped, against real SQLite files, with readers and writers in OTHER
processes (the lock that mattered is per process). The control arm runs the
removed cp + rm code against the same fixture and must fail where the new code
passes.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._update_source import update_text

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"

# Holds an idle WAL connection (the MCP server shape) until told to read again.
READER = (
    "import sqlite3, sys\n"
    "r = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
    "r.execute('PRAGMA journal_mode=WAL')\n"
    "if sys.argv[2] == 'txn':\n"
    "    r.execute('BEGIN')\n"
    "print('ready', r.execute('SELECT count(*) FROM notes').fetchone()[0], flush=True)\n"
    "sys.stdin.readline()\n"
    "print('then', r.execute('SELECT count(*) FROM notes').fetchone()[0], flush=True)\n"
    "if sys.argv[2] == 'txn':\n"
    "    r.execute('COMMIT')\n"
    "    print('after', r.execute('SELECT count(*) FROM notes').fetchone()[0], flush=True)\n"
    "sys.stdin.readline()\n"
)


def _block() -> str:
    m = re.search(
        r"# BEGIN db-restore-through-sqlite[^\n]*\n(.*?)# END db-restore-through-sqlite",
        update_text(),
        re.DOTALL,
    )
    assert m, "db-restore-through-sqlite block not found"
    return m.group(1)


def _restore(db: Path, limit: str = "600", prelude: str = "") -> tuple[int, str]:
    script = (
        "set -Eeuo pipefail\n"
        f'DB_FILE="{db}"\nGENESIS_UPDATE_DB_RESTORE_TIMEOUT_S={limit}\n'
        + prelude
        + _block()
        + '\nrc=0; _restore_db_snapshot || rc=$?\necho "RC=$rc LANDED=$_DB_RESTORE_LANDED"\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    m = re.search(r"^RC=(\d+) LANDED=(\d)$", proc.stdout, re.M)
    assert m, proc.stdout
    return int(m.group(1)), proc.stdout + f"LANDED={m.group(2)}"


def _old_cp_restore(db: Path) -> None:
    """The removed code, verbatim in effect: cp the snapshot over, drop sidecars."""
    subprocess.run(
        ["bash", "-c", f'cp "{db}.pre-update.peer-restore" "{db}" && rm -f "{db}-wal" "{db}-shm"'],
        check=True,
    )


def _fixture(tmp_path: Path, *, page_size: int | None = None) -> Path:
    """A live WAL database with a 'migrated' column and 5 rows, and its
    pre-update snapshot (DELETE mode, 2 rows, no such column) published the way
    update.sh publishes it."""
    db = tmp_path / "genesis.db"
    snap = Path(f"{db}.pre-update.peer-restore")
    s = sqlite3.connect(snap)
    if page_size:
        s.execute(f"PRAGMA page_size={page_size}")
    s.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    s.executemany("INSERT INTO notes (body) VALUES (?)", [("pre",), ("pre",)])
    s.commit()
    s.execute("PRAGMA journal_mode=DELETE")
    s.close()
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    live.execute("ALTER TABLE notes ADD COLUMN migrated TEXT")
    live.executemany("INSERT INTO notes (body) VALUES (?)", [("new",)] * 5)
    live.commit()
    live.close()
    return db


def _reader(db: Path, mode: str = "idle") -> tuple[subprocess.Popen, int]:
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", READER, str(db), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    line = p.stdout.readline().split()
    assert line[0] == "ready", line
    return p, int(line[1])


def _next(p: subprocess.Popen) -> list[str]:
    p.stdin.write("\n")
    p.stdin.flush()
    return p.stdout.readline().split()


def _finish(p: subprocess.Popen) -> None:
    p.stdin.write("\n")
    p.stdin.flush()
    p.wait(timeout=30)


def _server_write(db: Path) -> str:
    """What genesis-server's init does first: switch to WAL, then write."""
    con = sqlite3.connect(db, timeout=2)
    try:
        mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        con.execute("INSERT INTO notes (body) VALUES ('server')")
        con.commit()
        return f"ok {mode}"
    except sqlite3.OperationalError as exc:
        return f"error {exc}"
    finally:
        con.close()


def _state(db: Path) -> tuple[int, int, str]:
    con = sqlite3.connect(db)
    try:
        return (
            con.execute("SELECT count(*) FROM notes").fetchone()[0],
            len(con.execute("PRAGMA table_info(notes)").fetchall()),
            con.execute("PRAGMA journal_mode").fetchone()[0],
        )
    finally:
        con.close()


# -- the incident shape: idle readers attached ------------------------------


def test_restores_under_an_idle_reader_and_the_server_can_write(tmp_path):
    db = _fixture(tmp_path)
    reader, before = _reader(db)
    assert before == 5
    rc, out = _restore(db)
    assert rc == 0, out
    assert "Restored the pre-update database snapshot through SQLite" in out
    assert _server_write(db) == "ok wal"  # the reader is still attached
    assert _next(reader) == ["then", "3"], "the reader must see the restored rows, not stale pages"
    _finish(reader)
    assert _state(db) == (3, 2, "wal")


def test_control_the_removed_cp_restore_fails_the_same_fixture(tmp_path):
    """The arm that proves the harness can see the incident: with the old code
    the server cannot write and the reader keeps its pre-restore view."""
    db = _fixture(tmp_path)
    reader, _ = _reader(db)
    _old_cp_restore(db)
    assert _server_write(db).startswith("error database is locked")
    assert _next(reader) == ["then", "5"], "the old code left the reader on stale pages"
    _finish(reader)


def test_a_reader_mid_transaction_keeps_its_snapshot_then_sees_the_restore(tmp_path):
    db = _fixture(tmp_path)
    reader, _ = _reader(db, "txn")
    rc, out = _restore(db)
    assert rc == 0, out
    assert "a reader still in a transaction keeps the WAL" in out
    assert _next(reader) == ["then", "5"]
    assert reader.stdout.readline().split() == ["after", "2"]
    _finish(reader)
    assert _server_write(db) == "ok wal"


def test_waits_for_a_writer_holding_the_lock(tmp_path):
    db = _fixture(tmp_path)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import sqlite3,sys,time\nc=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
            "c.execute('BEGIN IMMEDIATE');print('held',flush=True);time.sleep(3);c.execute('COMMIT')",
            str(db),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    rc, out = _restore(db)
    holder.wait(timeout=30)
    assert rc == 0, out
    assert _state(db) == (2, 2, "wal")


# -- refusals: the live database is left exactly as it was ------------------


def test_a_lock_held_past_the_backup_budget_is_not_restored(tmp_path):
    """A bound of 61 s leaves the backup 1 s; the holder keeps the lock for 8."""
    db = _fixture(tmp_path)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import sqlite3,sys,time\nc=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
            "c.execute('BEGIN IMMEDIATE');print('held',flush=True);time.sleep(8);c.execute('COMMIT')",
            str(db),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    rc, out = _restore(db, limit="61")
    holder.wait(timeout=30)
    assert rc == 1, out
    assert "was NOT restored" in out and "LANDED=0" in out, out
    assert _state(db) == (5, 3, "wal")


def test_a_page_size_mismatch_is_refused_before_anything_is_written(tmp_path):
    db = _fixture(tmp_path, page_size=8192)
    rc, out = _restore(db)
    assert rc == 1 and "page_size differs" in out and "LANDED=0" in out, out
    assert _state(db) == (5, 3, "wal")


def test_a_rollback_journal_live_file_under_a_reader_is_refused_fast(tmp_path):
    """The incident's end state: the live file left in DELETE mode under a
    reader's SHARED lock. A backup into it would retry until the bound."""
    db = _fixture(tmp_path)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    reader = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import sqlite3,sys\nr=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
            "r.execute('BEGIN');r.execute('SELECT count(*) FROM notes').fetchone()\n"
            "print('ready',flush=True);sys.stdin.readline()",
            str(db),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert reader.stdout.readline().strip() == "ready"
    rc, out = _restore(db)
    _finish(reader)
    assert rc == 1 and "could not be switched to WAL" in out and "LANDED=0" in out, out
    assert _state(db)[:2] == (5, 3)


def test_a_corrupt_snapshot_is_refused_before_it_overwrites_anything(tmp_path):
    """The backup copies pages as they are, so a damaged snapshot would replace a
    consistent database; it is checked first and the live one is left alone."""
    db = _fixture(tmp_path)
    snap = Path(f"{db}.pre-update.peer-restore")
    s = sqlite3.connect(snap)
    s.execute("PRAGMA journal_mode=DELETE")
    s.executemany("INSERT INTO notes (body) VALUES (?)", [("x" * 500,)] * 200)
    s.commit()
    page = s.execute("PRAGMA page_size").fetchone()[0]
    s.close()
    raw = bytearray(snap.read_bytes())
    raw[page * 3 : page * 3 + 64] = b"\xff" * 64  # inside a b-tree page of notes
    snap.write_bytes(bytes(raw))
    rc, out = _restore(db)
    assert rc == 1, out
    assert "was NOT restored" in out and "LANDED=0" in out, out
    assert _state(db) == (5, 3, "wal")


# -- the bound: what each stopping point reports ---------------------------
# A real stop needs a bound above 60 s, so these replace `timeout` with a shell
# function that prints what the worker had printed and returns timeout's status.


@pytest.mark.parametrize(
    ("printed", "rc", "expect", "landed", "ok"),
    [
        ("", 124, "was NOT restored (exit 124, stopped at the 600s bound)", "0", False),
        ("", 137, "was NOT restored (exit 137, stopped at the 600s bound)", "0", False),
        (
            "COMMITTED",
            124,
            "WAS restored, but its verification did not complete (exit 124",
            "1",
            False,
        ),
        ("COMMITTED\nVERIFIED", 124, "Restored and verified", "1", True),
        (
            "FAILED: OperationalError: unable to open /x/COMMITTED/genesis.db",
            2,
            "was NOT restored (OperationalError: unable to open /x/COMMITTED/genesis.db)",
            "0",
            False,
        ),
    ],
)
def test_each_stopping_point_is_reported_as_what_happened(
    tmp_path, printed, rc, expect, landed, ok
):
    stub = (
        "timeout() { cat >/dev/null; "
        + (f"printf '%s\\n' {' '.join(repr(x) for x in printed.splitlines())}; " if printed else "")
        + f"return {rc}; }}\n"
    )
    status, out = _restore(tmp_path / "genesis.db", prelude=stub)
    assert expect in out, out
    assert f"LANDED={landed}" in out, out
    assert (status == 0) is ok, out


def _summary(landed: str) -> str:
    m = re.search(
        r"# BEGIN rollback-db-summary[^\n]*\n(.*?)        # END rollback-db-summary",
        update_text(),
        re.DOTALL,
    )
    assert m
    script = (
        f"db_ok=false\ncode_action=reset\ncode_kept=false\n_DB_RESTORE_LANDED={landed}\n"
        + m.group(1)
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def test_the_rollback_summary_names_a_restore_that_landed():
    assert "DB restored but NOT verified" in _summary("1")
    assert "DB restore FAILED" in _summary("0")


@pytest.mark.parametrize("kind", ["zero-byte", "no-schema"])
def test_an_empty_snapshot_never_empties_the_live_database(tmp_path, kind):
    """Both pass quick_check; restoring either would leave no tables."""
    db = _fixture(tmp_path)
    snap = Path(f"{db}.pre-update.peer-restore")
    snap.unlink()
    if kind == "zero-byte":
        snap.touch()
    else:
        sqlite3.connect(snap).close()
    rc, out = _restore(db)
    assert rc == 1 and "the snapshot has no schema" in out and "LANDED=0" in out, out
    assert _state(db) == (5, 3, "wal")


def test_error_text_from_the_snapshot_cannot_forge_a_marker(tmp_path):
    """A snapshot with a schema entry named "t\\nCOMMITTED\\nVERIFIED" whose SQL
    is invalid: SQLite's error ("malformed database schema (<name>)") quotes the
    name, newlines and all. Unsanitised, those lines would make bash report a
    verified restore that never happened."""
    db = _fixture(tmp_path)
    snap = Path(f"{db}.pre-update.peer-restore")
    s = sqlite3.connect(snap)
    s.execute('CREATE TABLE "t\nCOMMITTED\nVERIFIED" (id TEXT)')
    s.execute("PRAGMA writable_schema=ON")
    s.execute("UPDATE sqlite_master SET sql = 'CREATE TABLE (' WHERE name LIKE 't%VERIFIED'")
    s.commit()
    s.close()
    probe = sqlite3.connect(snap)
    with pytest.raises(sqlite3.DatabaseError, match="COMMITTED\nVERIFIED"):
        probe.execute("PRAGMA quick_check").fetchone()  # guard: the fixture forges
    probe.close()
    rc, out = _restore(db)
    assert rc == 1, out
    assert "was NOT restored" in out and "LANDED=0" in out, out
    assert _state(db) == (5, 3, "wal")


def test_the_worker_marks_each_point_it_passes(tmp_path):
    """The shipped worker, run directly: the markers the bash side reads must
    appear, in order, on a normal restore (the stub tests above assume them)."""
    m = re.search(r"<<'RESTORE_PY'\n(.*?)\nRESTORE_PY\n", _block(), re.DOTALL)
    assert m
    db = _fixture(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-", f"{db}.pre-update.peer-restore", str(db), "540"],
        input=m.group(1),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.split() == ["COMMITTED", "VERIFIED", "CHECKPOINT", "done"], proc.stdout


def test_an_invalid_bound_restores_nothing(tmp_path):
    db = _fixture(tmp_path)
    for bad in ("0", "60", "abc", "99999999999999999999"):
        rc, out = _restore(db, limit=bad)
        assert rc == 1 and "NOT restored" in out, (bad, out)
    assert _state(db) == (5, 3, "wal")


# -- wiring ------------------------------------------------------------------


def test_the_decision_restores_through_sqlite_and_never_copies():
    text = update_text()
    m = re.search(
        r"# BEGIN rollback-db-decision[^\n]*\n(.*?)# END rollback-db-decision", text, re.DOTALL
    )
    assert m
    decision = m.group(1)
    assert "elif _restore_db_snapshot; then" in decision
    commands = [ln.strip() for ln in decision.splitlines() if not ln.strip().startswith("#")]
    assert not [c for c in commands if re.search(r"(^|&&|\|\||;)\s*(cp|rm|mv)\s", c)], commands


@pytest.mark.parametrize("limit", ["600"])
def test_the_worker_runs_on_system_python_under_the_bound(limit):
    block = _block()
    assert 'timeout --kill-after=5s "$limit" python3 -I -S -' in block
    assert "VENV_DIR" not in block
    assert f"GENESIS_UPDATE_DB_RESTORE_TIMEOUT_S:-{limit}" in block
