"""d0012 — normalize control chars in existing campaign names.

Heals rows written before the write-boundary strip shipped, so the derived
scheduler job_id / logs / job_health.job_name / reflection note stop being garbled.
Idempotent; never merges two campaigns onto one name; no-op on a clean install.
"""

from __future__ import annotations

import sqlite3

import genesis.db.data_migrations.d0012_normalize_campaign_names as d0012


def _seed(path, rows) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE campaigns (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL)")
    db.executemany("INSERT INTO campaigns (id, name) VALUES (?, ?)", rows)
    db.commit()
    db.close()


def test_normalizes_control_char_name(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed(db_path, [("c1", "weekly-digest"), ("c2", "dirty\nname")])
    monkeypatch.setattr(d0012, "genesis_db_path", lambda: str(db_path))

    assert d0012.verify() is False
    assert d0012.migrate() == {"normalized": 1, "skipped_collision": 0}
    assert d0012.verify() is True

    db = sqlite3.connect(db_path)
    names = {r[0] for r in db.execute("SELECT name FROM campaigns").fetchall()}
    db.close()
    assert names == {"weekly-digest", "dirty name"}  # clean row untouched, dirty healed


def test_skips_collision_never_merges(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    # "collide\n" normalizes to "collide", which already names c1 -> must be skipped.
    _seed(db_path, [("c1", "collide"), ("c2", "collide\n")])
    monkeypatch.setattr(d0012, "genesis_db_path", lambda: str(db_path))

    assert d0012.migrate() == {"normalized": 0, "skipped_collision": 1}
    db = sqlite3.connect(db_path)
    names = {r[0] for r in db.execute("SELECT name FROM campaigns").fetchall()}
    db.close()
    assert "collide" in names and "collide\n" in names  # both survive, never merged
    # verify() is True: the only remaining dirty name is an intentional collision-skip
    assert d0012.verify() is True


def test_noop_on_clean_install(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed(db_path, [("c1", "clean-name")])
    monkeypatch.setattr(d0012, "genesis_db_path", lambda: str(db_path))

    assert d0012.verify() is True
    assert d0012.migrate() == {"normalized": 0, "skipped_collision": 0}


def test_multi_dirty_collide_no_clean_holder(tmp_path, monkeypatch):
    # LOAD-BEARING lock: three dirty rows all normalize to "foo" with NO pre-existing
    # clean holder. Only the in-loop names.discard/add mutation stops the 2nd/3rd
    # UPDATE from hitting UNIQUE(name) -> IntegrityError. A refactor that builds `names`
    # once (no mutation) would pass every OTHER test yet crash here. First row wins,
    # the rest are skipped, nothing is merged, and it stays idempotent.
    db_path = tmp_path / "genesis.db"
    _seed(db_path, [("c1", "foo\n"), ("c2", "foo\r"), ("c3", "foo\t")])
    monkeypatch.setattr(d0012, "genesis_db_path", lambda: str(db_path))

    assert d0012.migrate() == {"normalized": 1, "skipped_collision": 2}
    db = sqlite3.connect(db_path)
    names = {r[0] for r in db.execute("SELECT name FROM campaigns").fetchall()}
    db.close()
    assert "foo" in names and len(names) == 3  # one won; none merged (still 3 rows)
    assert d0012.migrate() == {"normalized": 0, "skipped_collision": 2}  # idempotent
    assert d0012.verify() is True


def test_all_control_name_normalizes_to_empty(tmp_path, monkeypatch):
    # Documented accepted outcome: an all-control-char name -> "" (single-line),
    # consistent with what the write-boundary strip would produce for the same input.
    db_path = tmp_path / "genesis.db"
    _seed(db_path, [("c1", "\n\t\r")])
    monkeypatch.setattr(d0012, "genesis_db_path", lambda: str(db_path))

    assert d0012.migrate() == {"normalized": 1, "skipped_collision": 0}
    db = sqlite3.connect(db_path)
    names = {r[0] for r in db.execute("SELECT name FROM campaigns").fetchall()}
    db.close()
    assert names == {""}
    assert d0012.verify() is True
