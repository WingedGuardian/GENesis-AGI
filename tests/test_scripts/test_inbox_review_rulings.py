"""Read-only exact-tag history enumeration never adjudicates owner authority."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.db.schema._tables import FTS5_DDL, TABLES
from tests.conftest import private_module


@pytest.fixture
def reader():
    path = Path(__file__).resolve().parents[2] / "scripts/inbox_review_rulings.py"
    assert path.is_file(), "the explicit review workflow needs its read-only ruling reader"
    return private_module("inbox_review_rulings_fixture", path)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "history ?#%.db"
    with sqlite3.connect(path) as db:
        db.executescript(FTS5_DDL)
        db.execute(TABLES["memory_metadata"])
        db.execute("ALTER TABLE memory_metadata ADD COLUMN superseded_by TEXT")
        db.execute("ALTER TABLE memory_metadata ADD COLUMN future_field BLOB")
    return path


def insert(path, name, tags="inbox_disposition", metadata=None):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO memory_fts VALUES(?,?,?,?,?)",
                   (name, "Full disposition\nwith its original context", "owner", tags, "semantic_memory"))
        if metadata is not None:
            db.execute("INSERT INTO memory_metadata(memory_id,created_at) VALUES(?,?)",
                       (name, "2026-01-01T00:00:00Z"))
            for column, value in metadata.items():
                assert column in {"deprecated", "expires_at", "durability", "invalid_at",
                                  "superseded_by", "confidence", "future_field"}
                db.execute(f'UPDATE memory_metadata SET "{column}"=? WHERE memory_id=?', (value, name))


def invoke(reader, database, capsys):
    before = database.read_bytes()
    assert reader.main(["--db", str(database)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert database.read_bytes() == before
    return json.loads(captured.out)


@pytest.mark.parametrize("separator", [" ", "\t", "\n", "\r", "\u00a0", "\u2003"],
                         ids=["space", "tab", "newline", "return", "nbsp", "em-space"])
def test_exact_tags_include_all_whitespace_and_exclude_near_matches(reader, database, capsys, separator):
    insert(database, "match", "before" + separator + "inbox_disposition" + separator + "after")
    for name, tags in [("prefix", "x_inbox_disposition"), ("suffix", "inbox_disposition_old"),
                       ("phrase", "inbox disposition"), ("case", "INBOX_DISPOSITION")]:
        insert(database, name, tags)
    result = invoke(reader, database, capsys)
    assert result["scope"] == "retained_exact_tagged_fts_only"
    assert result["complete"] is True
    assert [r["memory_id"] for r in result["records"]] == ["match"]


@pytest.mark.parametrize("metadata", [None, {}, {"deprecated": 1},
    {"durability": "temporary", "expires_at": "2000-01-01T00:00:00Z"},
    {"expires_at": "malformed", "invalid_at": "malformed"},
    {"superseded_by": "replacement"}, {"future_field": b"\x00\xff"},
    {"confidence": float("inf")}],
    ids=["missing", "ordinary", "deprecated", "expired", "malformed", "superseded", "blob", "infinite"])
def test_history_and_all_metadata_are_retained_without_authority(reader, database, capsys, metadata):
    insert(database, "history", metadata=metadata)
    result = invoke(reader, database, capsys)
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["content"] == "Full disposition\nwith its original context"
    assert record["source_type"] == "owner"
    assert record["tags"] == "inbox_disposition"
    assert record["collection"] == "semantic_memory"
    assert record["authority"] == "not_adjudicated"
    if metadata is None:
        assert record["metadata"] is None
    else:
        assert "future_field" in record["metadata"]
        for key, value in metadata.items():
            if isinstance(value, bytes):
                assert record["metadata"][key] == {"type": "blob", "hex": "00ff"}
            elif key == "confidence":
                assert record["metadata"][key]["type"] == "nonfinite-real"
            else:
                assert record["metadata"][key] == value


def test_duplicate_fts_rows_are_not_silently_deduplicated(reader, database, capsys):
    insert(database, "same")
    insert(database, "same")
    records = invoke(reader, database, capsys)["records"]
    assert len(records) == 2
    assert len({r["fts_rowid"] for r in records}) == 2


def test_empty_success_is_scoped_and_distinct_from_failure(reader, database, capsys):
    assert invoke(reader, database, capsys)["records"] == []


@pytest.mark.parametrize("failure", ["missing", "schema", "not-sqlite", "metadata-schema"])
def test_unavailable_or_invalid_database_is_not_empty_success(reader, tmp_path, capsys, failure):
    path = tmp_path / "bad.db"
    if failure == "not-sqlite":
        path.write_bytes(b"not a database")
    elif failure != "missing":
        with sqlite3.connect(path) as db:
            if failure == "metadata-schema":
                db.executescript(FTS5_DDL)
                db.execute("CREATE TABLE memory_metadata(wrong_column TEXT)")
    before = path.read_bytes() if path.exists() else None
    assert reader.main(["--db", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert (path.read_bytes() if path.exists() else None) == before


def test_default_database_uses_existing_environment_override(reader, database):
    insert(database, "default")
    # The suite replaces env.genesis_db_path with an isolated fixed function.
    # A fresh CLI exercises the real resolver, pointing only at this fixture.
    run = subprocess.run([sys.executable, str(Path(reader.__file__))],
                         env={**os.environ, "GENESIS_DB_PATH": str(database)},
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["records"][0]["memory_id"] == "default"


def test_reader_keeps_snapshot_open_and_query_only(reader, database):
    insert(database, "one")
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
    with reader.connect_readonly(database) as db:
        assert len(reader.read_rulings(db)["records"]) == 1
        assert db.in_transaction
        insert(database, "later")
        assert db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 1
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM memory_fts")


def test_nontext_tags_are_visible_as_incomplete_history(reader, database, capsys):
    insert(database, "ambiguous", b"inbox_disposition")
    result = invoke(reader, database, capsys)
    assert result["records"] == []
    assert result["complete"] is False
    assert len(result["nontext_tag_rows"]) == 1


def test_null_tags_have_no_exact_token(reader, database, capsys):
    insert(database, "untagged", None)
    assert invoke(reader, database, capsys)["records"] == []


def test_actual_cli_returns_full_fixture_without_writes(reader, database):
    insert(database, "fixture", metadata={"deprecated": 1})
    before = database.read_bytes()
    run = subprocess.run([sys.executable, str(Path(reader.__file__)), "--db", str(database)],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["complete"] is True
    assert result["records"][0]["metadata"]["deprecated"] == 1
    assert result["records"][0]["content"] == "Full disposition\nwith its original context"
    assert database.read_bytes() == before
