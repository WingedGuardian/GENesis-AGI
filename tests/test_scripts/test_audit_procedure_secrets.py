"""audit_procedure_secrets: a read-only report with no values in it (#4859)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

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
