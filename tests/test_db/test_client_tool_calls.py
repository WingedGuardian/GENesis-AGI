"""client_tool_calls and credential_access_log.client: schema on both build paths, CRUD.

A fresh install builds from ``db/schema/_tables.py``; an existing install gets
the table and the column from migration 20261108071709. Both must agree, and the
CRUD must be safe in a stdio subprocess that runs before the migration has.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.crud import client_tool_calls
from genesis.db.schema._tables import INDEXES, TABLES

MIGRATION = "20261108071709_client_tool_calls"


def _migration():
    return importlib.import_module(f"genesis.db.migrations.{MIGRATION}")


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def test_table_ddl_matches_between_build_paths():
    assert _normalize(_migration()._TABLE_DDL) == _normalize(TABLES["client_tool_calls"])


def test_index_ddl_matches_between_build_paths():
    in_tables = [s for s in INDEXES if "idx_client_tool_calls" in s]
    assert [_normalize(s) for s in in_tables] == [_normalize(_migration()._INDEX_DDL)]


def test_credential_access_log_declares_client_on_the_fresh_path():
    assert "client" in _normalize(TABLES["credential_access_log"])


async def _columns(conn, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


@pytest.fixture
async def legacy_conn(tmp_path):
    """The upgrade path's start: no client_tool_calls, credential_access_log without client."""
    conn = await aiosqlite.connect(str(tmp_path / "legacy.db"))
    try:
        await conn.execute(
            "CREATE TABLE credential_access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "unit_id TEXT NOT NULL, accessor_context TEXT, accessed_at TEXT NOT NULL, "
            "query_match_score REAL)"
        )
        await conn.execute(
            "INSERT INTO credential_access_log (unit_id, accessed_at) VALUES ('u1', '2026-01-01')"
        )
        await conn.commit()
        yield conn
    finally:
        await conn.close()


async def test_up_adds_table_index_and_column_idempotently(legacy_conn):
    mig = _migration()
    await mig.up(legacy_conn)
    await mig.up(legacy_conn)  # a retried migration must not explode
    await legacy_conn.commit()
    assert "client" in await _columns(legacy_conn, "credential_access_log")
    assert {"client", "server", "tool", "called_at", "outcome", "result_ref"} <= await _columns(
        legacy_conn, "client_tool_calls"
    )
    cur = await legacy_conn.execute("SELECT client FROM credential_access_log WHERE unit_id = 'u1'")
    assert (await cur.fetchone())[0] is None  # no backfill: existing rows read NULL
    cur = await legacy_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_client_tool_calls_client_time'"
    )
    assert await cur.fetchone() is not None


async def test_up_without_credential_access_log_creates_only_the_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "empty.db")) as conn:
        await _migration().up(conn)
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row[0] for row in await cur.fetchall()}
    assert "client_tool_calls" in names
    assert "credential_access_log" not in names


async def test_fresh_schema_has_both(db):
    assert "client" in await _columns(db, "credential_access_log")
    assert "result_ref" in await _columns(db, "client_tool_calls")


async def _all(db) -> list[tuple]:
    cur = await db.execute(
        "SELECT client, tool, outcome, result_ref FROM client_tool_calls ORDER BY id"
    )
    return [tuple(row) for row in await cur.fetchall()]


async def test_record_and_prune(db):
    calls = (
        ("codex", "t0", "2026-01-01T00:00:00+00:00", "raised", None),
        ("codex", "t1", "2026-06-01T00:00:00+00:00", "returned", "0123abcd0123abcd"),
        ("other", "x", "2026-06-01T00:00:00+00:00", "cancelled", None),
    )
    for client, tool, when, outcome, ref in calls:
        assert await client_tool_calls.record(
            db,
            client=client,
            server="memory",
            tool=tool,
            called_at=when,
            outcome=outcome,
            result_ref=ref,
        )
    assert await _all(db) == [
        ("codex", "t0", "raised", None),
        ("codex", "t1", "returned", "0123abcd0123abcd"),
        ("other", "x", "cancelled", None),
    ]
    deleted = await client_tool_calls.prune(db, older_than_days=30, now="2026-06-15T00:00:00+00:00")
    assert deleted == 1
    assert [row[1] for row in await _all(db)] == ["t1", "x"]


async def test_outcome_outside_the_set_is_refused(db):
    with pytest.raises(sqlite3.IntegrityError):
        await client_tool_calls.record(
            db,
            client="codex",
            server="memory",
            tool="t",
            called_at="2026-01-01T00:00:00+00:00",
            outcome="success",
            result_ref=None,
        )


async def test_prune_refuses_a_zero_window(db):
    with pytest.raises(ValueError):
        await client_tool_calls.prune(db, older_than_days=0)


async def test_record_and_prune_are_noops_before_the_migration(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "pre.db")) as conn:
        assert (
            await client_tool_calls.record(
                conn,
                client="codex",
                server="memory",
                tool="t",
                called_at="2026-01-01T00:00:00+00:00",
                outcome="returned",
                result_ref=None,
            )
            is False
        )
        assert await client_tool_calls.prune(conn, older_than_days=1) == 0
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        assert await cur.fetchall() == []  # never creates the table itself


_REPO = Path(__file__).resolve().parents[2]


def test_disk_hygiene_runs_the_prune():
    script = (_REPO / "scripts" / "disk_hygiene.sh").read_text()
    assert '"$REPO_DIR/scripts/prune_client_tool_calls.py"' in script


def test_prune_script_deletes_only_old_rows(tmp_path):
    path = tmp_path / "genesis.db"
    with sqlite3.connect(path) as conn:
        conn.execute(TABLES["client_tool_calls"])
        conn.executemany(
            "INSERT INTO client_tool_calls (client, server, tool, called_at, outcome) "
            "VALUES ('codex', 'memory', ?, ?, 'returned')",
            [("old", "2000-01-01T00:00:00+00:00"), ("new", "2999-01-01T00:00:00+00:00")],
        )
    env = {**os.environ, "GENESIS_DB_PATH": str(path)}
    run = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "prune_client_tool_calls.py"), "--days", "30"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "deleted 1 row" in run.stdout, (run.stdout, run.stderr)
    with sqlite3.connect(path) as conn:
        assert [r[0] for r in conn.execute("SELECT tool FROM client_tool_calls")] == ["new"]


def _run_prune(path, *args):
    env = {**os.environ, "GENESIS_DB_PATH": str(path)}
    return subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "prune_client_tool_calls.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def test_prune_script_never_creates_a_missing_database(tmp_path):
    path = tmp_path / "absent" / "genesis.db"
    run = _run_prune(path, "--days", "30")
    assert run.returncode == 0, run.stderr
    assert not path.exists() and not path.parent.exists()


def test_prune_script_exits_nonzero_on_failure(tmp_path):
    path = tmp_path / "genesis.db"
    path.write_text("this is not a sqlite database")
    run = _run_prune(path, "--days", "30")
    assert run.returncode == 1
    assert "client_tool_calls prune error" in run.stderr


async def test_the_table_check_is_per_connection(db, tmp_path):
    # A migrated connection first, then an unmigrated one in the same process:
    # the second must skip, not raise.
    assert await client_tool_calls.record(
        db,
        client="codex",
        server="memory",
        tool="t",
        called_at="2026-01-01T00:00:00+00:00",
        outcome="returned",
        result_ref=None,
    )
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as bare:
        assert (
            await client_tool_calls.record(
                bare,
                client="codex",
                server="memory",
                tool="t",
                called_at="2026-01-01T00:00:00+00:00",
                outcome="returned",
                result_ref=None,
            )
            is False
        )
        assert await client_tool_calls.prune(bare, older_than_days=1) == 0
