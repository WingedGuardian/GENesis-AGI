"""Preview preserves crashed-writer WAL data; ordinary recording keeps its contract."""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import aiosqlite
import pytest

from tests.test_scripts.test_pr_verification_closer import _doc_for, _prv, _seed


def _bytes(path):
    return {
        suffix: hashlib.sha256(path.with_name(path.name + suffix).read_bytes()).hexdigest()
        for suffix in ("", "-wal")
    }


@pytest.mark.parametrize("verdict", ["pass-mechanical", "cannot-verify"])
def test_actual_cli_preview_preserves_committed_wal(tmp_path, verdict):
    path = tmp_path / "ledger.db"
    _seed(path)
    producer = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); "
            "c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute(\"UPDATE pr_verifications SET pr_title='WAL fixture'\"); "
            "c.commit(); os._exit(0)",
            str(path),
        ],
        capture_output=True,
        timeout=20,
    )
    assert producer.returncode == 0, producer.stderr
    before = _bytes(path)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_doc_for(verdict)))
    env = dict(os.environ, GENESIS_HOME=str(tmp_path / "private-home"))
    command = [
        sys.executable,
        "-I",
        str(_prv.__file__),
        "close",
        "--pr",
        "7",
        "--db-path",
        str(path),
        "--evidence-file",
        str(evidence),
        "--dry-run",
    ]
    if verdict == "cannot-verify":
        command.extend(["--note", "fixture cannot establish intent"])
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "DRY RUN" in result.stdout and verdict in result.stdout
    assert _bytes(path) == before
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as reader:
        row = reader.execute("SELECT pr_title,status FROM pr_verifications").fetchone()
        assert row == ("WAL fixture", "open")
    assert _bytes(path) == before


@pytest.mark.parametrize("name", ["space name.db", "literal?mode=rw#.db", "percent%é.db"])
def test_preview_encodes_path_and_denies_writes(tmp_path, name):
    path = tmp_path / name
    _seed(path)

    async def check():
        async with _prv._ledger_connection(path, dry_run=True) as db:
            assert (
                await (await db.execute("SELECT count(*) FROM pr_verifications")).fetchone()
            ) == (1,)
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                await db.execute("UPDATE pr_verifications SET status='closed'")

    asyncio.run(check())


def test_missing_preview_never_creates_database(tmp_path):
    path = tmp_path / "missing.db"

    async def check():
        with pytest.raises(sqlite3.OperationalError):
            async with _prv._ledger_connection(path, dry_run=True):
                pytest.fail("missing database opened")

    asyncio.run(check())
    assert not path.exists()


def test_quarantined_preview_refuses_before_open(tmp_path, monkeypatch):
    from genesis.db.integrity import DatabaseIntegrityError, quarantine_database

    path = tmp_path / "ledger.db"
    _seed(path)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "private-home"))
    quarantine_database(path, source="test", detail="disposable preview refusal")

    def forbidden(*args, **kwargs):
        pytest.fail("quarantined database reached the opener")

    monkeypatch.setattr(aiosqlite, "connect", forbidden)

    async def check():
        with pytest.raises(DatabaseIntegrityError):
            async with _prv._ledger_connection(path, dry_run=True):
                pytest.fail("quarantined database opened")

    asyncio.run(check())


def test_removed_after_admission_is_not_recreated(tmp_path, monkeypatch):
    from genesis.db import admission

    path = tmp_path / "ledger.db"
    _seed(path)
    admitted = admission.assert_admitted

    def remove(target):
        admitted(target)
        path.unlink()

    monkeypatch.setattr(admission, "assert_admitted", remove)

    async def check():
        with pytest.raises(sqlite3.OperationalError):
            async with _prv._ledger_connection(path, dry_run=True):
                pytest.fail("removed database opened")

    asyncio.run(check())
    assert not path.exists()


@pytest.mark.parametrize("failure", ["recheck", "body", "cancel"])
def test_preview_closes_on_recheck_body_error_and_cancellation(tmp_path, monkeypatch, failure):
    from genesis.db import admission

    path = tmp_path / "ledger.db"
    _seed(path)
    opened = []
    connect = aiosqlite.connect

    def capture(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        return db

    calls = []
    admitted = admission.assert_admitted

    def recheck(target):
        admitted(target)
        calls.append(target)
        if failure == "recheck" and len(calls) == 2:
            raise RuntimeError("fixture quarantine flip")

    monkeypatch.setattr(aiosqlite, "connect", capture)
    monkeypatch.setattr(admission, "assert_admitted", recheck)

    async def check():
        error = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(error):
            async with _prv._ledger_connection(path, dry_run=True):
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("fixture body error")
        assert len(opened) == 1 and calls == [path, path]
        with pytest.raises(ValueError, match="no active connection"):
            await opened[0].execute("SELECT 1")

    asyncio.run(check())
