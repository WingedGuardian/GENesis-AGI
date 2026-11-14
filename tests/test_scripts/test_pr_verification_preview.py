"""Preview preserves crashed-writer WAL data; ordinary recording keeps its contract."""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading

import aiosqlite
import pytest

from tests.test_scripts.test_pr_verification_closer import MERGE_SHA, _doc_for, _prv, _seed


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
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text(
        "#!"
        + sys.executable
        + "\nimport json\nprint("
        + repr(json.dumps({"mergeCommit": {"oid": MERGE_SHA}, "body": ""}))
        + ")\n"
    )
    gh.chmod(0o700)
    env = dict(
        os.environ,
        GENESIS_HOME=str(tmp_path / "private-home"),
        PATH=str(binary) + os.pathsep + os.environ["PATH"],
    )
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
    assert result.returncode == (0 if verdict == "pass-mechanical" else 4), (
        result.stdout,
        result.stderr,
    )
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


@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize("result_kind", ["done", "failed"])
def test_recheck_preview_preserves_wal_and_recording_still_appends(tmp_path, preview, result_kind):
    from tests.test_scripts.test_pr_verification_rechecks import _recheck

    # Use direct stored fixture data, so this test never needs GitHub.
    path = tmp_path / "genesis.db"
    _seed(path)
    rechecks = [
        dict(
            trigger="2030-01-01",
            recheck="fixture read",
            detail="fixture event",
            results=[],
            done_at=None,
        )
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE pr_verifications SET status='closed', rechecks=?", (json.dumps(rechecks),)
        )
    producer = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute(\"UPDATE pr_verifications SET pr_title='WAL fixture'\"); c.commit(); os._exit(0)",
            str(path),
        ],
        capture_output=True,
        timeout=20,
    )
    assert producer.returncode == 0, producer.stderr
    before = _bytes(path)
    result = dict(repo="owner/repo", result=result_kind, note="owned fixture observation")
    assert _recheck(tmp_path, result, *(["--dry-run"] if preview else [])) == 0
    if preview:
        assert _bytes(path) == before
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        title, status, raw = db.execute(
            "SELECT pr_title,status,rechecks FROM pr_verifications"
        ).fetchone()
    assert (title, status) == ("WAL fixture", "closed")
    assert len(json.loads(raw)[0]["results"]) == (0 if preview else 1)


def test_cancellation_during_open_stops_owned_worker(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    _seed(path)
    started, release = threading.Event(), threading.Event()
    opened = []
    connect = aiosqlite.connect

    def held_open(*args, **kwargs):
        db = connect(*args, **kwargs)
        connector = db._connector

        def held_connector():
            started.set()
            if not release.wait(5):
                raise RuntimeError("fixture opener was not released")
            return connector()

        db._connector = held_connector
        opened.append(db)
        return db

    monkeypatch.setattr(aiosqlite, "connect", held_open)

    async def check():
        async def enter():
            async with _prv._ledger_connection(path, dry_run=True):
                pytest.fail("cancelled open reached the context body")

        task = asyncio.create_task(enter())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for db in opened:
                await asyncio.to_thread(db._thread.join, 5)
        assert len(opened) == 1 and not opened[0]._thread.is_alive()
        with pytest.raises(ValueError, match="no active connection"):
            await opened[0].execute("SELECT 1")

    asyncio.run(check())
