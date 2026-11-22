"""audit_procedure_secrets: a read-only report with no values in it (#4859)."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

FAKE = "Zq7-fake-Secret!9"
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit_procedure_secrets.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_procedure_secrets_t", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE procedural_memory (id TEXT, activation_tier TEXT, deprecated INT,"
        " principle TEXT, steps TEXT)"
    )
    rows = [
        ("a", "DORMANT", 0, "p", json.dumps([f"sshpass -p '{FAKE}' ssh h"])),
        ("b", "LIBRARY", 0, "clean", json.dumps(["git status"])),
    ]
    con.executemany("INSERT INTO procedural_memory VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def test_the_report_names_ids_and_kinds_never_values(tmp_path):
    db = tmp_path / "g.db"
    _fixture_db(db)
    out = tmp_path / "report.json"
    assert _load().main(["--db", str(db), "--out", str(out)]) == 0
    text = out.read_text()
    report = json.loads(text)
    assert report["procedures"] == 2 and report["with_findings"] == 1
    assert report["findings"][0]["id"] == "a" and "sshpass" in report["findings"][0]["patterns"]
    assert FAKE not in text and "Zq7" not in text
    stored = sqlite3.connect(db).execute("SELECT steps FROM procedural_memory WHERE id='a'")
    assert FAKE in stored.fetchone()[0]  # read-only: the database is untouched


def _db_with(path: Path, rows) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE procedural_memory (id TEXT, activation_tier TEXT, deprecated INT,"
        " principle TEXT, steps TEXT, lesson TEXT)"
    )
    con.executemany("INSERT INTO procedural_memory VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def test_json_fields_are_decoded_before_the_scan(tmp_path):
    """Escaped quotes and credential keys only show once the document is decoded."""
    db = tmp_path / "g.db"
    _db_with(
        db,
        [
            ("q", "DORMANT", 0, "p", json.dumps(['password "Zq7\'fake!9" then']), None),
            ("k", "DORMANT", 0, "p", "[]", "  " + json.dumps({"password": "x7!Secret"})),
            ("c", "DORMANT", 0, "p", json.dumps(["git status"]), json.dumps({"note": "ok"})),
        ],
    )
    out = tmp_path / "r.json"
    assert _load().main(["--db", str(db), "--out", str(out)]) == 0
    text = out.read_text()
    assert sorted(f["id"] for f in json.loads(text)["findings"]) == ["k", "q"]
    assert "Zq7" not in text and "x7!Secret" not in text


def test_a_row_whose_every_field_was_withheld_is_reported_and_exits_2(tmp_path, monkeypatch):
    from genesis.learning.procedural import credential_scrub

    db = tmp_path / "g.db"
    _db_with(db, [("w", "DORMANT", 0, "p", json.dumps(["git status"]), None)])

    def boom(text):
        raise credential_scrub.ScrubFailed("unavailable")

    monkeypatch.setattr(credential_scrub, "_shared_scrub", boom)
    out = tmp_path / "r.json"
    assert _load().main(["--db", str(db), "--out", str(out)]) == 2
    report = json.loads(out.read_text())
    assert report["with_withheld_fields"] == 1
    assert report["findings"][0]["id"] == "w"
    assert report["findings"][0]["withheld_fields"] == ["principle", "steps"]


def _alias(kind: str, db: Path) -> Path:
    if kind == "same":
        return db.parent / "." / db.name
    if kind in ("-wal", "-shm"):
        return db.with_name(db.name + kind)
    alias = db.parent / f"alias-{kind}"
    if kind == "symlink":
        alias.symlink_to(db)
    else:
        os.link(db, alias)
    return alias


@pytest.mark.parametrize("kind", ["same", "symlink", "hardlink", "-wal", "-shm"])
def test_out_naming_the_database_is_refused(tmp_path, kind):
    db = tmp_path / "g.db"
    _fixture_db(db)
    before = db.read_bytes()
    assert _load().main(["--db", str(db), "--out", str(_alias(kind, db))]) == 1
    assert db.read_bytes() == before


@pytest.mark.parametrize("sidecar", ["-wal", "-shm"])
def test_a_sidecar_named_through_a_symlinked_database_is_refused(tmp_path, sidecar):
    """SQLite puts the sidecars beside the file a symlinked --db points to, so
    those are guarded too (#4872 review)."""
    db = tmp_path / "real.db"
    _fixture_db(db)
    alias = tmp_path / "alias.db"
    alias.symlink_to(db)
    target = db.with_name(db.name + sidecar)
    target.write_bytes(b"sidecar")
    assert _load().main(["--db", str(alias), "--out", str(target)]) == 1
    assert target.read_bytes() == b"sidecar"
