"""d0005 — re-verify dispatch proposals false-failed by the old string/size gate.

Flips status='failed' verification rows to 'executed' when the deliverable now
passes AND every file exists at its EXACT resolved path. Fuzzy-only and
genuinely-missing rows stay 'failed'. Idempotent; no-op on a fresh install.
"""

from __future__ import annotations

import json
import sqlite3

import genesis.db.data_migrations.d0005_reverify_false_dispatch_failures as d0005


def _seed(path, rows) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE ego_proposals (id TEXT PRIMARY KEY, status TEXT, "
        "user_response TEXT, expected_outputs TEXT)"
    )
    db.executemany(
        "INSERT INTO ego_proposals (id, status, user_response, expected_outputs) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    db.commit()
    db.close()


def test_flips_exact_pass_leaves_missing_failed(tmp_path, monkeypatch):
    f = tmp_path / "deliverable.md"
    f.write_text("## Summary\nA real, complete deliverable body.\n")
    # A content-miss (advisory-only under the fixed logic) must still flip.
    flip_eo = json.dumps({"files": [str(f)], "required_strings": ["## Not Present Heading"]})
    missing_eo = json.dumps({"files": [str(tmp_path / "never-written.md")]})
    _seed(
        tmp_path / "genesis.db",
        [
            ("p-flip", "failed", "session:x|verification_failed:Missing string", flip_eo),
            ("p-missing", "failed", "session:y|verification_failed:Missing file", missing_eo),
        ],
    )
    monkeypatch.setattr(d0005, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))

    assert d0005.verify() is False
    assert d0005.migrate() == {"flipped": 1, "scanned": 2}
    assert d0005.verify() is True

    db = sqlite3.connect(tmp_path / "genesis.db")
    status = dict(db.execute("SELECT id, status FROM ego_proposals").fetchall())
    ur = db.execute("SELECT user_response FROM ego_proposals WHERE id = 'p-flip'").fetchone()[0]
    db.close()
    assert status["p-flip"] == "executed"
    assert "|completed:" in ur  # reads as a positive outcome for the harvester
    assert status["p-missing"] == "failed"  # genuinely absent → stays failed


def test_fuzzy_only_match_is_not_flipped(tmp_path, monkeypatch):
    """A similarly-named file (exact path absent) must NOT flip — no heuristic
    reliably tells a rename from an unrelated file, so the migration is
    exact-match only."""
    (tmp_path / "report-v2.md").write_text("## Summary\nbody\n")
    eo = json.dumps({"files": [str(tmp_path / "report.md")], "required_strings": ["## Summary"]})
    _seed(tmp_path / "genesis.db", [("p-fuzzy", "failed", "z|verification_failed:x", eo)])
    monkeypatch.setattr(d0005, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))

    assert d0005.migrate()["flipped"] == 0
    db = sqlite3.connect(tmp_path / "genesis.db")
    got = db.execute("SELECT status FROM ego_proposals WHERE id = 'p-fuzzy'").fetchone()
    db.close()
    assert got[0] == "failed"


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    f = tmp_path / "d.md"
    f.write_text("a real deliverable body\n")
    _seed(
        tmp_path / "genesis.db",
        [("p1", "failed", "q|verification_failed:x", json.dumps({"files": [str(f)]}))],
    )
    monkeypatch.setattr(d0005, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))

    assert d0005.migrate()["flipped"] == 1
    assert d0005.migrate()["flipped"] == 0
    assert d0005.verify() is True


def test_fresh_install_is_noop(tmp_path, monkeypatch):
    _seed(tmp_path / "genesis.db", [])
    monkeypatch.setattr(d0005, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))

    assert d0005.verify() is True
    assert d0005.migrate() == {"flipped": 0, "scanned": 0}


def test_flips_many_rows_across_batch_boundaries(tmp_path, monkeypatch):
    # The read-first-then-batched-write restructure must flip correctly when the
    # candidate set spans multiple commit batches. _exact_pass is stubbed (the
    # disk check itself is covered by the real-file tests above) so we can seed
    # far more rows than one batch cheaply.
    rows = [(f"p{i}", "failed", "x|verification_failed:z", '{"files": []}') for i in range(250)]
    _seed(tmp_path / "genesis.db", rows)
    monkeypatch.setattr(d0005, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))
    monkeypatch.setattr(d0005, "_exact_pass", lambda eo: True)

    assert d0005.migrate() == {"flipped": 250, "scanned": 250}
    db = sqlite3.connect(tmp_path / "genesis.db")
    remaining = db.execute("SELECT COUNT(*) FROM ego_proposals WHERE status = 'failed'").fetchone()[
        0
    ]
    db.close()
    assert remaining == 0
    assert d0005.verify() is True
