"""Tests for the read-only query path (``genesis db query``).

The read-only property is enforced by SQLite itself: a ``mode=ro`` URI,
``PRAGMA query_only``, and an allowlist authorizer. Every refusal case below
asserts the database is byte-for-byte unchanged AND that no new file appeared
next to it, because two of the cases (``VACUUM INTO`` and ``ATTACH ...
mode=rwc``) create a NEW file even on a ``mode=ro`` connection when no
authorizer is installed — measured while building this.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from genesis.db import query_cli
from genesis.db.readonly_query import (
    QueryResult,
    ReadOnlyViolation,
    run_readonly_query,
)

_SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Path:
    # Isolate the quarantine marker from the live install.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    path = tmp_path / "data" / "t.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT)")
    conn.executemany("INSERT INTO t (s) VALUES (?)", [("a",), ("DELETE",), ("c",)])
    conn.commit()
    conn.close()
    return path


def _snapshot(path: Path) -> tuple:
    """Row count, data_version, file bytes, and the directory listing."""
    conn = sqlite3.connect(path)
    try:
        count = conn.execute("SELECT count(*) FROM t").fetchone()[0]
        version = conn.execute("PRAGMA data_version").fetchone()[0]
    finally:
        conn.close()
    return count, version, path.read_bytes(), sorted(os.listdir(path.parent))


# ── reads that must succeed ──────────────────────────────────────────────


class TestReadsSucceed:
    def test_select_with_dml_literal(self, db):
        res = run_readonly_query(db, "SELECT id, s FROM t WHERE s = 'DELETE'")
        assert isinstance(res, QueryResult)
        assert res.columns == ["id", "s"]
        assert res.rows == [(2, "DELETE")]
        assert res.truncated is False

    def test_replace_function(self, db):
        res = run_readonly_query(db, "SELECT replace(s, 'a', 'z') FROM t WHERE id = 1")
        assert res.rows == [("z",)]

    def test_with_recursive(self, db):
        sql = (
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 4) "
            "SELECT n FROM r"
        )
        assert run_readonly_query(db, sql).rows == [(1,), (2,), (3,), (4,)]

    def test_pragma_table_info(self, db):
        res = run_readonly_query(db, "PRAGMA table_info(t)")
        assert [r[1] for r in res.rows] == ["id", "s"]

    def test_pragma_table_valued_function(self, db):
        res = run_readonly_query(db, "SELECT name FROM pragma_table_info('t')")
        assert res.rows == [("id",), ("s",)]

    def test_params(self, db):
        res = run_readonly_query(db, "SELECT id FROM t WHERE s = ?", ("c",))
        assert res.rows == [(3,)]

    def test_path_with_question_mark_and_hash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
        odd = tmp_path / "we?ird#name%20.db"
        conn = sqlite3.connect(odd)
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (7)")
        conn.commit()
        conn.close()
        before = sorted(os.listdir(tmp_path))
        assert run_readonly_query(odd, "SELECT x FROM t").rows == [(7,)]
        assert sorted(os.listdir(tmp_path)) == before

    def test_truncation(self, db):
        res = run_readonly_query(db, "SELECT id FROM t ORDER BY id", max_rows=2)
        assert res.rows == [(1,), (2,)]
        assert res.truncated is True

    def test_exact_limit_is_not_truncated(self, db):
        res = run_readonly_query(db, "SELECT id FROM t", max_rows=3)
        assert len(res.rows) == 3
        assert res.truncated is False

    def test_no_limit(self, db):
        res = run_readonly_query(db, "SELECT id FROM t", max_rows=None)
        assert len(res.rows) == 3
        assert res.truncated is False

    def test_wal_database_with_open_writer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
        path = tmp_path / "w.db"
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t (x)")
        writer.execute("INSERT INTO t VALUES (1)")
        writer.commit()
        try:
            assert run_readonly_query(path, "SELECT count(*) FROM t").rows == [(1,)]
        finally:
            writer.close()


# ── writes and side effects that must be refused ─────────────────────────

_REFUSED = [
    ("INSERT INTO t (s) VALUES ('x')", "SQLITE_INSERT"),
    ("UPDATE t SET s = 'x'", "SQLITE_UPDATE"),
    ("DELETE FROM t", "SQLITE_DELETE"),
    ("DROP TABLE t", None),  # first action is a DELETE on sqlite_master
    ("CREATE TABLE u AS SELECT * FROM t", None),
    ("ALTER TABLE t ADD COLUMN y TEXT", None),
    ("WITH x AS (SELECT 1) DELETE FROM t", "SQLITE_DELETE"),
    ("ATTACH 'file:x?mode=rwc' AS o", "SQLITE_ATTACH"),
    ("VACUUM", "SQLITE_ATTACH"),
    ("VACUUM INTO 'out.db'", "SQLITE_ATTACH"),
    ("PRAGMA user_version = 5", "SQLITE_PRAGMA"),
    ("PRAGMA journal_mode = delete", "SQLITE_PRAGMA"),
    ("PRAGMA query_only = OFF", "SQLITE_PRAGMA"),
    ("BEGIN", "SQLITE_TRANSACTION"),
    ("SAVEPOINT s", "SQLITE_SAVEPOINT"),
    ("ANALYZE", None),
    ("SELECT 1; DELETE FROM t", None),
    ("SELECT load_extension('x')", "SQLITE_FUNCTION"),
    ("SELECT fts3_tokenizer('simple')", "SQLITE_FUNCTION"),
    ("PRAGMA writable_schema", "SQLITE_PRAGMA"),
]


@pytest.mark.parametrize(
    ("sql", "action"),
    _REFUSED,
    ids=[c[0] for c in _REFUSED],
)
def test_refused_and_unchanged(db, monkeypatch, sql, action):
    monkeypatch.chdir(db.parent)  # a relative 'out.db' / 'x' would land here
    before = _snapshot(db)
    with pytest.raises(ReadOnlyViolation) as excinfo:
        run_readonly_query(db, sql)
    if action is not None:
        assert excinfo.value.action == action
    assert "refused: " in str(excinfo.value)
    assert _snapshot(db) == before


def test_multi_statement_is_refused_by_execute(db):
    """``execute`` (never ``executescript``) rejects a second statement."""
    with pytest.raises(ReadOnlyViolation, match="one statement"):
        run_readonly_query(db, "SELECT 1; DELETE FROM t")


def test_ro_uri_is_read_only(tmp_path):
    """The RW-open inventory trusts ``ro_uri(`` as read-only; lock that here."""
    from genesis.db.integrity import ro_uri

    uri = ro_uri(tmp_path / "a?b#c%d.db")
    assert uri.startswith("file:/")
    assert uri.endswith("?mode=ro")
    assert uri.count("?") == 1
    assert "#" not in uri
    assert "%3F" in uri and "%23" in uri and "%25" in uri


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    missing = tmp_path / "nope.db"
    with pytest.raises(sqlite3.OperationalError):
        run_readonly_query(missing, "SELECT 1")
    assert not missing.exists()


def test_quarantined_database_is_refused(db, monkeypatch):
    from genesis.db import integrity

    monkeypatch.setattr(integrity, "database_is_quarantined", lambda _p: True)
    with pytest.raises(integrity.DatabaseIntegrityError):
        run_readonly_query(db, "SELECT 1")


# ── security-review findings (round 1) ─────────────────────────────────

_RUNAWAY = (
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) "
    "SELECT n FROM c ORDER BY n DESC"
)


class TestRuntimeBounds:
    def test_runaway_query_is_aborted_at_deadline(self, db):
        import time

        from genesis.db.readonly_query import QueryTimeout

        start = time.monotonic()
        with pytest.raises(QueryTimeout, match=r"aborted: exceeded 0\.5s"):
            run_readonly_query(db, _RUNAWAY, timeout_s=0.5)
        assert time.monotonic() - start < 10

    def test_timeout_none_disables_the_deadline(self, db):
        res = run_readonly_query(db, "SELECT count(*) FROM t", timeout_s=None)
        assert res.rows == [(3,)]

    def test_length_limit_exists_and_caps_blobs(self, db):
        assert hasattr(sqlite3, "SQLITE_LIMIT_LENGTH")
        for sql in ("SELECT length(zeroblob(1000000000))", "SELECT randomblob(200000000)"):
            with pytest.raises(sqlite3.DataError, match="too big"):
                run_readonly_query(db, sql)

    def test_blob_under_the_cap_still_reads(self, db):
        assert run_readonly_query(db, "SELECT length(zeroblob(1000000))").rows == [(1000000,)]


@pytest.fixture()
def vtab_db(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    path = tmp_path / "v.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE VIRTUAL TABLE ft USING fts5(body)")
    conn.execute("INSERT INTO ft VALUES ('hello x world'), ('other text')")
    conn.execute("CREATE VIRTUAL TABLE rt USING rtree(id, a, b)")
    conn.execute("INSERT INTO rt VALUES (1, 0, 1)")
    conn.commit()
    conn.close()
    return path


class TestVirtualTables:
    def test_fts5_match_reads(self, vtab_db):
        res = run_readonly_query(vtab_db, "SELECT body FROM ft WHERE ft MATCH 'x'")
        assert res.rows == [("hello x world",)]

    def test_fts5_with_bm25_ordering(self, vtab_db):
        res = run_readonly_query(
            vtab_db, "SELECT body FROM ft WHERE ft MATCH 'text OR x' ORDER BY bm25(ft)"
        )
        assert len(res.rows) == 2

    def test_data_version_without_argument_is_allowed(self, db):
        assert len(run_readonly_query(db, "PRAGMA data_version").rows) == 1

    def test_data_version_with_argument_is_refused(self, db):
        with pytest.raises(ReadOnlyViolation, match="SQLITE_PRAGMA"):
            run_readonly_query(db, "PRAGMA data_version = 3")

    def test_rtree_is_refused_without_calling_the_query_a_write(self, vtab_db):
        with pytest.raises(ReadOnlyViolation) as excinfo:
            run_readonly_query(vtab_db, "SELECT * FROM rt")
        msg = str(excinfo.value)
        assert "outside the read-only allowlist" in msg
        assert "not read-only" not in msg


class TestNoStatement:
    @pytest.mark.parametrize("sql", ["", "   ", "-- just a comment", "/* c */"])
    def test_empty_or_comment_only_is_an_error(self, db, sql):
        with pytest.raises(sqlite3.ProgrammingError, match="no statement"):
            run_readonly_query(db, sql)

    def test_pragma_with_no_rows_is_not_mistaken_for_empty(self, db):
        assert run_readonly_query(db, "PRAGMA table_info(no_such_table)").rows == []


def test_wal_sidecars_are_the_only_files_created(tmp_path, monkeypatch):
    """Opening a WAL database ``mode=ro`` may create SQLite's own -wal/-shm
    sidecars when absent; the main file and everything else stay untouched."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    d = tmp_path / "wal"
    d.mkdir()
    path = d / "w.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()  # last close checkpoints and removes the sidecars
    main_bytes = path.read_bytes()
    run_readonly_query(path, "SELECT count(*) FROM t")
    assert path.read_bytes() == main_bytes
    assert set(os.listdir(d)) <= {"w.db", "w.db-wal", "w.db-shm"}


# ── CLI ──────────────────────────────────────────────────────────────────


def _cli(argv: list[str]) -> tuple[int, str, str]:
    import argparse

    parser = argparse.ArgumentParser(prog="genesis")
    sub = parser.add_subparsers(dest="command")
    query_cli.add_parser(sub)
    args = parser.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


class TestCli:
    def test_tsv_default(self, db):
        rc, out, err = _cli(["db", "query", "--db", str(db), "SELECT id, s FROM t ORDER BY id"])
        assert rc == 0
        assert out.splitlines() == ["id\ts", "1\ta", "2\tDELETE", "3\tc"]
        assert err == ""

    def test_json(self, db):
        rc, out, _ = _cli(
            [
                "db",
                "query",
                "--db",
                str(db),
                "--json",
                "SELECT id FROM t WHERE s = ?",
                "--param",
                "c",
            ]
        )
        assert rc == 0
        assert json.loads(out) == [{"id": 3}]

    def test_truncation_line(self, db):
        rc, out, err = _cli(["db", "query", "--db", str(db), "--limit", "2", "SELECT id FROM t"])
        assert rc == 0
        assert len(out.splitlines()) == 3  # header + 2 rows
        assert "2 rows shown, more exist" in err

    def test_refused_exit_4(self, db):
        rc, _, err = _cli(["db", "query", "--db", str(db), "DELETE FROM t"])
        assert rc == 4  # never 2: argparse owns 2 for usage errors
        assert (
            "refused: query touched an operation outside the read-only allowlist "
            "(denied: SQLITE_DELETE)"
        ) in err

    def test_sqlite_error_exit_1(self, db):
        rc, _, err = _cli(["db", "query", "--db", str(db), "SELECT * FROM no_such_table"])
        assert rc == 1
        assert "no such table" in err

    def test_missing_db_exit_1_names_path(self, tmp_path):
        missing = tmp_path / "absent.db"
        rc, _, err = _cli(["db", "query", "--db", str(missing), "SELECT 1"])
        assert rc == 1
        assert f"no database at {missing}" in err
        assert not missing.exists()

    def test_quarantine_exit_3(self, db, monkeypatch):
        from genesis.db import integrity

        monkeypatch.setattr(integrity, "database_is_quarantined", lambda _p: True)
        rc, _, err = _cli(["db", "query", "--db", str(db), "SELECT 1"])
        assert rc == 3
        assert "quarantined" in err

    def test_module_entrypoint(self, db):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(_SRC), *filter(None, [env.get("PYTHONPATH")])])
        env["GENESIS_HOME"] = str(db.parent.parent / "home")
        ok = subprocess.run(
            [
                sys.executable,
                "-m",
                "genesis",
                "db",
                "query",
                "--db",
                str(db),
                "SELECT count(*) AS n FROM t",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert ok.returncode == 0, ok.stderr
        assert ok.stdout.splitlines() == ["n", "3"]
        bad = subprocess.run(
            [
                sys.executable,
                "-m",
                "genesis",
                "db",
                "query",
                "--db",
                str(db),
                "INSERT INTO t (s) VALUES ('x')",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert bad.returncode == 4
        assert "denied: SQLITE_INSERT" in bad.stderr


class TestCliReviewFindings:
    def test_timeout_exit_1_with_message(self, db):
        rc, _, err = _cli(["db", "query", "--db", str(db), "--timeout", "0.5", _RUNAWAY])
        assert rc == 1
        assert "aborted: exceeded 0.5s" in err

    def test_timeout_default_is_60(self):
        import argparse

        parser = argparse.ArgumentParser(prog="genesis")
        query_cli.add_parser(parser.add_subparsers(dest="command"))
        assert parser.parse_args(["db", "query", "SELECT 1"]).timeout == 60

    def test_timeout_zero_disables(self, db):
        rc, out, _ = _cli(["db", "query", "--db", str(db), "--timeout", "0", "SELECT 1 AS x"])
        assert rc == 0
        assert out.splitlines() == ["x", "1"]

    @pytest.mark.parametrize("flag", ["--limit", "--timeout"])
    def test_negative_values_rejected_by_argparse(self, db, flag):
        with pytest.raises(SystemExit) as excinfo, redirect_stderr(io.StringIO()):
            _cli(["db", "query", "--db", str(db), flag, "-1", "SELECT 1"])
        assert excinfo.value.code == 2

    @pytest.mark.parametrize("sql", ["", "-- only a comment"])
    def test_no_statement_exit_1(self, db, sql):
        rc, out, err = _cli(["db", "query", "--db", str(db), sql])
        assert rc == 1
        assert "no statement" in err
        assert out == ""

    def test_tsv_escapes_controls_in_cells_and_headers(self, db):
        sql = (
            "SELECT char(1) || 'a' || char(13) || char(127) || char(133) || char(10) AS \"h\tx\ny\""
        )
        rc, out, _ = _cli(["db", "query", "--db", str(db), sql])
        assert rc == 0
        lines = out.split("\n")
        assert lines[-1] == ""  # exactly one row line plus header, newline-terminated
        assert len(lines) == 3
        assert lines[0] == "h\\tx\\ny"
        assert lines[1] == "\\x01a\\r\\x7f\\x85\\n"
        for line in lines[:2]:
            assert not any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in line)

    def test_json_escapes_del_and_c1(self, db):
        rc, out, _ = _cli(
            ["db", "query", "--db", str(db), "--json", "SELECT char(127) || char(133) AS v"]
        )
        assert rc == 0
        assert not any(0x7F <= ord(ch) <= 0x9F for ch in out)
        assert json.loads(out) == [{"v": "\x7f\x85"}]

    def test_json_non_finite_floats_are_valid_json(self, db):
        rc, out, _ = _cli(
            [
                "db",
                "query",
                "--db",
                str(db),
                "--json",
                "SELECT 9e999 AS a, -9e999 AS b",
            ]
        )
        assert rc == 0

        def _reject(token):
            raise ValueError(token)

        rows = json.loads(out, parse_constant=_reject)
        assert rows == [{"a": "Infinity", "b": "-Infinity"}]


class TestCliArchitectReview:
    def test_bare_db_is_a_usage_error_exit_2(self):
        import argparse

        parser = argparse.ArgumentParser(prog="genesis")
        query_cli.add_parser(parser.add_subparsers(dest="command"))
        with pytest.raises(SystemExit) as excinfo, redirect_stderr(io.StringIO()) as err:
            parser.parse_args(["db"])
        assert excinfo.value.code == 2
        assert "query" in err.getvalue()

    def test_help_prints_the_venv_interpreter(self):
        import argparse

        parser = argparse.ArgumentParser(prog="genesis")
        query_cli.add_parser(parser.add_subparsers(dest="command"))
        out = io.StringIO()
        with pytest.raises(SystemExit), redirect_stdout(out):
            parser.parse_args(["db", "query", "--help"])
        text = " ".join(out.getvalue().split())
        assert '~/genesis/.venv/bin/python -m genesis db query "<sql>"' in text
        assert "exit 4" in text

    def test_db_schema_tool_points_at_the_read_route(self):
        from genesis.mcp.health import db_schema

        doc = " ".join((db_schema.db_schema.__doc__ or "").split())
        if not doc:  # wrapped by the MCP decorator
            doc = " ".join((getattr(db_schema.db_schema, "description", "") or "").split())
        assert '~/genesis/.venv/bin/python -m genesis db query "<sql>"' in doc
        assert "Do not use the sqlite3 shell" in doc
