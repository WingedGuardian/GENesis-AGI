from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import aiosqlite
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
    marker.write_text(
        json.dumps(
            {
                "db_path": str(db.resolve()),
                "st_dev": stat.st_dev,
                "st_ino": stat.st_ino,
            }
        )
    )

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


def test_stale_failed_check_cannot_quarantine_replacement(tmp_path, monkeypatch):
    """A failure result is evidence about one inode, never its replacement."""
    db = tmp_path / "genesis.db"
    db.write_bytes(b"broken")
    checked = integrity._fingerprint(db)

    replacement = tmp_path / "replacement.db"
    _healthy_db(replacement)

    def stale_failure(path):
        os.replace(replacement, path)
        return integrity.IntegrityResult(
            False,
            "failure on the old inode",
            checked_fingerprint=checked,
        )

    monkeypatch.setattr(integrity, "quick_check", stale_failure)
    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.require_healthy_database(db, source="race-test")

    assert not integrity.quarantine_path().exists()
    assert integrity.quick_check is stale_failure


def test_quick_check_retries_inode_churn_without_binding_stale_result(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    _healthy_db(db)
    real_connect = sqlite3.connect

    class ChurningConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql):
            replacement = tmp_path / "next.db"
            with real_connect(replacement) as conn:
                conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
            os.replace(replacement, db)
            return type("Rows", (), {"fetchall": lambda self: [("ok",)]})()

    monkeypatch.setattr(integrity.sqlite3, "connect", lambda *_a, **_kw: ChurningConnection())
    result = integrity.quick_check(db)
    monkeypatch.setattr(integrity.sqlite3, "connect", real_connect)

    assert result.healthy is False
    assert result.checked_fingerprint is None
    assert "identity changed repeatedly" in result.detail


def test_indeterminate_check_never_quarantines_current_inode(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    _healthy_db(db)
    monkeypatch.setattr(
        integrity,
        "quick_check",
        lambda _path: integrity.IntegrityResult(False, "identity unstable"),
    )

    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.require_healthy_database(db, source="race-test")

    assert not integrity.quarantine_path().exists()


def test_operational_sqlite_error_is_indeterminate_and_never_quarantines(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    _healthy_db(db)

    def locked(*_args, **_kwargs):
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        exc.sqlite_errorname = "SQLITE_BUSY"
        raise exc

    monkeypatch.setattr(integrity.sqlite3, "connect", locked)
    result = integrity.quick_check(db)

    assert result.healthy is False
    assert result.checked_fingerprint is None
    assert "SQLITE_BUSY" in result.detail
    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.require_healthy_database(db, source="busy-test")
    assert not integrity.quarantine_path().exists()


def test_explicit_extended_corruption_error_remains_quarantinable(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    _healthy_db(db)

    def corrupt(*_args, **_kwargs):
        exc = sqlite3.DatabaseError("database disk image is malformed")
        exc.sqlite_errorcode = sqlite3.SQLITE_CORRUPT | (3 << 8)
        exc.sqlite_errorname = "SQLITE_CORRUPT_INDEX"
        raise exc

    monkeypatch.setattr(integrity.sqlite3, "connect", corrupt)
    with pytest.raises(integrity.DatabaseIntegrityError):
        integrity.require_healthy_database(db, source="corrupt-test")

    assert integrity.database_is_quarantined(db)


def test_quick_check_with_no_result_is_indeterminate(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    _healthy_db(db)

    class EmptyResultConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql):
            return type("Rows", (), {"fetchall": lambda self: []})()

    monkeypatch.setattr(
        integrity.sqlite3, "connect", lambda *_args, **_kwargs: EmptyResultConnection()
    )
    result = integrity.quick_check(db)

    assert result.healthy is False
    assert result.checked_fingerprint is None
    assert result.detail == "quick_check returned no rows"


def test_clear_cannot_unlink_marker_replaced_while_waiting_for_lock(tmp_path, monkeypatch):
    """Clear re-reads under the mutation lock and preserves a newer marker."""
    db = tmp_path / "genesis.db"
    _healthy_db(db)
    marker = integrity.quarantine_path()
    marker.parent.mkdir(parents=True)
    old = {"db_path": str(db.resolve()), "st_dev": 1, "st_ino": 2, "source": "old"}
    newer = {
        **integrity._fingerprint(db),
        "source": "new",
        "detail": "new failure",
    }
    marker.write_text(json.dumps(old))

    original_lock = integrity._quarantine_mutation_lock

    @integrity.contextlib.contextmanager
    def replace_before_lock():
        marker.write_text(json.dumps(newer))
        with original_lock():
            yield

    monkeypatch.setattr(integrity, "_quarantine_mutation_lock", replace_before_lock)
    integrity._clear_quarantine_for(db)

    assert json.loads(marker.read_text())["source"] == "new"


def test_connect_sqlite_rw_refuses_quarantined_database(tmp_path):
    from genesis.db.connection import connect_sqlite_rw

    db = tmp_path / "genesis.db"
    _healthy_db(db)
    integrity.quarantine_database(db, source="test", detail="known bad")

    with pytest.raises(integrity.DatabaseIntegrityError):
        connect_sqlite_rw(db)


@pytest.mark.asyncio
async def test_connect_aiosqlite_rw_preserves_await_and_context_manager(tmp_path):
    from genesis.db.connection import connect_aiosqlite_rw

    db = tmp_path / "genesis.db"
    async with connect_aiosqlite_rw(db) as conn:
        await conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
        await conn.commit()

    assert Path(db).exists()


def test_connect_aiosqlite_rw_refuses_before_returning_connector(tmp_path):
    from genesis.db.connection import connect_aiosqlite_rw

    db = tmp_path / "genesis.db"
    _healthy_db(db)
    integrity.quarantine_database(db, source="test", detail="known bad")

    with pytest.raises(integrity.DatabaseIntegrityError):
        connect_aiosqlite_rw(db)


@pytest.mark.asyncio
async def test_connect_aiosqlite_rw_rechecks_when_delayed_await(tmp_path):
    from genesis.db.connection import connect_aiosqlite_rw

    db = tmp_path / "genesis.db"
    _healthy_db(db)
    pending = connect_aiosqlite_rw(db)
    integrity.quarantine_database(db, source="test", detail="became bad before await")

    with pytest.raises(integrity.DatabaseIntegrityError):
        await pending


@pytest.mark.asyncio
async def test_connect_aiosqlite_rw_rechecks_on_context_entry(tmp_path):
    from genesis.db.connection import connect_aiosqlite_rw

    db = tmp_path / "genesis.db"
    _healthy_db(db)
    pending = connect_aiosqlite_rw(db)
    integrity.quarantine_database(db, source="test", detail="became bad before entry")

    with pytest.raises(integrity.DatabaseIntegrityError):
        async with pending:
            pytest.fail("quarantined database was opened")


@pytest.mark.asyncio
async def test_connect_aiosqlite_rw_closes_if_quarantined_during_open(tmp_path, monkeypatch):
    from genesis.db.connection import connect_aiosqlite_rw

    db = tmp_path / "genesis.db"
    _healthy_db(db)
    real_connect = aiosqlite.connect
    opened: list[aiosqlite.Connection] = []

    class QuarantineAfterOpen:
        def __init__(self, connector):
            self._connector = connector

        def __await__(self):
            async def open_then_quarantine():
                connection = await self._connector
                opened.append(connection)
                integrity.quarantine_database(db, source="test", detail="became bad during open")
                return connection

            return open_then_quarantine().__await__()

    monkeypatch.setattr(
        aiosqlite,
        "connect",
        lambda *args, **kwargs: QuarantineAfterOpen(real_connect(*args, **kwargs)),
    )

    with pytest.raises(integrity.DatabaseIntegrityError):
        await connect_aiosqlite_rw(db)

    assert opened and opened[0]._connection is None


@pytest.mark.asyncio
async def test_connect_aiosqlite_rw_preserves_guard_error_if_cleanup_fails(tmp_path, monkeypatch):
    from genesis.db.connection import connect_aiosqlite_rw

    db = tmp_path / "genesis.db"
    _healthy_db(db)

    class FakeConnection:
        async def close(self):
            raise RuntimeError("close failed")

    class QuarantineDuringOpen:
        def __await__(self):
            async def open_then_quarantine():
                integrity.quarantine_database(db, source="test", detail="became bad during open")
                return FakeConnection()

            return open_then_quarantine().__await__()

    monkeypatch.setattr(aiosqlite, "connect", lambda *_args, **_kwargs: QuarantineDuringOpen())

    with pytest.raises(integrity.DatabaseIntegrityError) as raised:
        await connect_aiosqlite_rw(db)

    assert any("close failed" in note for note in raised.value.__notes__)
