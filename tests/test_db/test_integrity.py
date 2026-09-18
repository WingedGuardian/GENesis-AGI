from __future__ import annotations

import json
import os
import sqlite3

import pytest

from genesis.db import integrity


@pytest.fixture(autouse=True)
def isolated_genesis_home(tmp_path, monkeypatch):
    home = tmp_path / "genesis-home"
    monkeypatch.setattr(integrity, "genesis_home", lambda: home)
    return home


def _healthy_db(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES ('ok')")


def test_healthy_same_inode_remains_quarantined(tmp_path):
    db = tmp_path / "healthy.db"
    _healthy_db(db)
    marker = integrity.quarantine_path()
    marker.parent.mkdir(parents=True)
    stat = db.stat()
    marker.write_text(json.dumps({
        "db_path": str(db.resolve()), "st_dev": stat.st_dev, "st_ino": stat.st_ino,
    }))

    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.require_healthy_database(db, source="test")
    assert marker.exists()


def test_corrupt_database_is_quarantined_and_refused(tmp_path):
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a sqlite database")

    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.require_healthy_database(db, source="test")

    assert integrity.database_is_quarantined(db)
    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.assert_not_quarantined(db)
    marker = json.loads(integrity.quarantine_path().read_text())
    assert marker["source"] == "test"
    assert marker["st_ino"] == db.stat().st_ino


@pytest.mark.asyncio
async def test_existing_serialized_connection_stops_after_quarantine(tmp_path):
    from genesis.db.connection import get_db

    db_path = tmp_path / "live.db"
    db = await get_db(db_path)
    try:
        await db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        await db.commit()
        integrity.quarantine_database(
            db_path, source="test", detail="detected after connection opened"
        )

        with pytest.raises(integrity.DatabaseIntegrityError):
            await db.execute("INSERT INTO sample VALUES (1)")
        with pytest.raises(integrity.DatabaseIntegrityError):
            await db.commit()
    finally:
        await db.close()


def test_atomic_replacement_makes_old_quarantine_stale(tmp_path):
    db = tmp_path / "genesis.db"
    db.write_bytes(b"broken")
    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.require_healthy_database(db, source="test")
    old_inode = db.stat().st_ino

    replacement = tmp_path / "replacement.db"
    _healthy_db(replacement)
    os.replace(replacement, db)

    assert db.stat().st_ino != old_inode
    assert not integrity.database_is_quarantined(db)
    integrity.require_healthy_database(db, source="startup")
    assert not integrity.quarantine_path().exists()


def test_missing_database_named_by_marker_stays_quarantined(tmp_path):
    db = tmp_path / "missing.db"
    marker = integrity.quarantine_path()
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"db_path": str(db.resolve()), "st_dev": 1, "st_ino": 2}))

    assert integrity.database_is_quarantined(db)


def test_malformed_marker_fails_closed(tmp_path, caplog):
    db = tmp_path / "healthy.db"
    _healthy_db(db)
    marker = integrity.quarantine_path()
    marker.parent.mkdir(parents=True)
    marker.write_text("not-json")

    assert integrity.database_is_quarantined(db)
    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.assert_not_quarantined(db)
    assert "unreadable" in caplog.text
