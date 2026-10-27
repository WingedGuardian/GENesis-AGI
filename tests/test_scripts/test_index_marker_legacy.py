"""Poison containment, IO visibility and atomic legacy receipt migration."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MARKER = _ROOT / "scripts/lib/index_marker.py"
_spec = importlib.util.spec_from_file_location("legacy_marker_tests", _MARKER)
im = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(im)


@pytest.fixture(autouse=True)
def owned_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "owned-home"))


def _payload(**overrides):
    return {
        "repo_path": "/tmp",
        "tools": "cbm",
        "mode": "fast",
        "requested_at": 123.0,
        "attempts": 2,
        **overrides,
    }


def _artifact(name, raw):
    directory = im.marker_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(raw)
    return path


def _rows(table):
    db = sqlite3.connect(im.database_path().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        return [
            dict(row)
            for row in db.execute(
                {
                    "legacy_imports": "SELECT * FROM legacy_imports",
                    "pending": "SELECT * FROM pending",
                    "failed": "SELECT * FROM failed",
                    "failed_outcomes": "SELECT * FROM failed_outcomes",
                }[table]
            )
        ]
    finally:
        db.close()


def _cli(*args):
    return subprocess.run(
        ["python3", str(_MARKER), *args],
        text=True,
        capture_output=True,
        timeout=15,
        env=dict(os.environ),
    )


@pytest.mark.parametrize(
    "name,raw",
    [
        ("0" * 16 + ".json", b"{truncated"),
        ("0" * 16 + ".inflight.json", b"[]"),
        ("0" * 16 + ".failed.json", json.dumps(_payload(repo_path="\ud800")).encode()),
        (f".spool-{'0' * 16}-{'a' * 32}.spool", b"[" * 2000 + b"]" * 2000),
        (f".outcome-spool-{'0' * 16}-{'a' * 32}-{'b' * 32}.spool", b"null"),
        (
            ".outcome-" + "0" * 16,
            json.dumps(
                {"version": 1, "claim_id": "\ud800", "action": "consume", "recorded_at": 1}
            ).encode(),
        ),
        (".last-full-" + "0" * 16, b"NaN"),
        (".full-backoff-" + "0" * 16, b"\xff"),
        ("bad-name.json", b"{}"),
    ],
)
def test_artifact_kinds_keep_raw_and_continue_valid_sibling(name, raw):
    bad = _artifact(name, raw)
    good = _artifact(im.marker_hash("/tmp") + ".json", json.dumps(_payload()).encode())

    rows = im.list_markers()

    assert len(rows) == 1 and rows[0]["hash"] == im.marker_hash("/tmp")
    assert rows[0]["attempts"] == 2
    receipts = _rows("legacy_imports")
    rejected = [r for r in receipts if r["disposition"] == "quarantined"]
    assert len(receipts) == 2 and len(rejected) == 1
    assert bytes(rejected[0]["raw_payload"]) == raw
    assert bytes(rejected[0]["source_name_bytes"]) == os.fsencode(name)
    assert rejected[0]["reason"].isascii()
    assert _rows("failed") == [] and _rows("failed_outcomes") == []
    assert not bad.exists() and not good.exists()


def test_poison_cannot_replace_existing_business_failure():
    h = im.marker_hash("/tmp")
    im.write_marker("/tmp", "cbm", "fast")
    for _ in range(im.MAX_ATTEMPTS):
        claim = im.claim(h)
        im.restore(h, claim_id=claim["claim_id"], attempts_inc=True)
    before = im.get_failed(h)
    with im._write_db() as db:
        im._record_failed_outcome(db, h, "owned conflict", b"original")
    before_outcome = _rows("failed_outcomes")
    _artifact(h + ".failed.json", b"{invalid")
    _artifact(".outcome-" + h, b"{invalid")

    assert im.list_markers() == []

    assert im.get_failed(h) == before
    assert _rows("failed_outcomes") == before_outcome
    assert len(_rows("legacy_imports")) == 2


def test_legacy_failed_outcome_is_receipt_only():
    h = im.marker_hash("/tmp")
    raw = b"{historical malformed outcome"
    source = _artifact(".failed-outcome-" + h, raw)
    assert im.list_markers() == []
    assert _rows("failed_outcomes") == []
    receipt = _rows("legacy_imports")[0]
    assert receipt["disposition"] == "quarantined" and receipt["reason"] == "legacy-failed-outcome"
    assert bytes(receipt["raw_payload"]) == raw and not source.exists()


@pytest.mark.parametrize("late_kind", ["sidecar", "spool"])
def test_legacy_failed_outcome_cannot_erase_claim_conflict(late_kind):
    h = im.marker_hash("/tmp")
    im.write_marker("/tmp", "cbm", "fast")
    nonce = im.claim(h)["claim_id"]
    with im._write_db() as db:
        assert im._record_outcome_db(db, h, nonce, "consume", 1, b"first") == "recorded"
        assert im._record_outcome_db(db, h, nonce, "restore", 2, b"conflict") == "conflict"
    before = _rows("failed_outcomes")
    raw = b"{historical malformed outcome"
    _artifact(".failed-outcome-" + h, raw)
    if late_kind == "sidecar":
        _artifact(
            ".outcome-" + h,
            json.dumps(
                {"version": 1, "claim_id": nonce, "action": "consume", "recorded_at": 3}
            ).encode(),
        )
    else:
        im._write_outcome_spool(h, nonce, "consume")
    assert im.list_markers() == []
    assert _rows("failed_outcomes") == before
    assert im.apply_remembered_outcome(h) is None
    receipt = [r for r in _rows("legacy_imports") if r["reason"] == "legacy-failed-outcome"]
    assert len(receipt) == 1 and bytes(receipt[0]["raw_payload"]) == raw
    assert im.repend_stale_inflight() == [h]
    assert im.list_markers()[0]["attempts"] == 1


@pytest.mark.parametrize("name", [b"\xff.json", b"line\nbreak.json"])
def test_lossless_source_identity_resumes_crash_claim(name):
    raw = b"{poison"
    decoded = os.fsdecode(name)
    path = _artifact(decoded + ".migrating-" + "c" * 32, raw)
    assert im.list_markers() == []
    receipt = _rows("legacy_imports")[0]
    assert bytes(receipt["source_name_bytes"]) == name
    assert bytes(receipt["raw_payload"]) == raw
    assert not path.exists()
    status = _cli("import-status")
    assert status.returncode == 0, status.stderr
    record = json.loads(status.stdout)["records"][0]
    assert record["source_name_hex"] == name.hex()
    assert "raw_payload" not in record


def test_savepoint_removes_partial_sql_then_imports_sibling(monkeypatch):
    bad_hash = "0" * 16
    _artifact(bad_hash + ".json", b"{}")
    _artifact(im.marker_hash("/tmp") + ".json", json.dumps(_payload()).encode())
    original = im._import_one

    def partial(db, source, raw):
        if source.name == bad_hash + ".json":
            im._coalesce_pending(db, bad_hash, _payload(repo_path="/tmp/partial"))
            raise im._legacy.LegacyArtifactInvalid("injected-after-insert")
        return original(db, source, raw)

    monkeypatch.setattr(im, "_import_one", partial)
    assert [r["hash"] for r in im.list_markers()] == [im.marker_hash("/tmp")]
    assert len(_rows("pending")) == 1
    assert len(_rows("legacy_imports")) == 2


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("injected SQL IO"),
        RuntimeError("injected programmer failure"),
        MemoryError("injected allocation failure"),
    ],
)
def test_non_content_errors_abort_batch_without_receipt_or_retirement(monkeypatch, error):
    h = im.marker_hash("/tmp")
    source = _artifact(h + ".json", json.dumps(_payload()).encode())

    def broken(db, path, raw):
        im._coalesce_pending(db, h, _payload())
        raise error

    monkeypatch.setattr(im, "_import_one", broken)
    with pytest.raises(type(error), match="injected"):
        im.list_markers()
    assert _rows("pending") == [] and _rows("legacy_imports") == []
    assert len(list(source.parent.glob(source.name + ".migrating-*"))) == 1


@pytest.mark.parametrize("operation", ["read", "stat", "canonicalize"])
def test_filesystem_io_is_visible_and_recoverable(monkeypatch, operation):
    h = im.marker_hash("/tmp")
    source = _artifact(f".spool-{h}-{'d' * 32}.spool", json.dumps(_payload()).encode())
    with monkeypatch.context() as patch:
        if operation == "canonicalize":

            def inaccessible(repo):
                raise PermissionError("injected canonicalization IO")

            patch.setattr(im, "marker_hash", inaccessible)
        else:
            method = "read_bytes" if operation == "read" else "stat"
            original = getattr(Path, method)

            def inaccessible(path, *args, **kwargs):
                if path == source:
                    raise PermissionError("injected artifact IO")
                return original(path, *args, **kwargs)

            patch.setattr(Path, method, inaccessible)
        with pytest.raises(PermissionError, match="injected"):
            im.list_markers()
        assert source.exists() if operation != "stat" else source.read_bytes()
        assert _rows("legacy_imports") == []
    assert im.list_markers()[0]["attempts"] == 2


@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-failed", "commit-ack-lost"])
def test_commit_boundary_never_retires_unrepresented_claim(monkeypatch, after_commit):
    h = im.marker_hash("/tmp")
    source = _artifact(h + ".json", json.dumps(_payload()).encode())
    original = im._write_db

    @contextmanager
    def commit_fault(**kwargs):
        with original(**kwargs) as db:
            yield db
            if not after_commit:
                raise sqlite3.OperationalError("injected before commit")
        if after_commit:
            raise sqlite3.OperationalError("injected lost commit acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(im, "_write_db", commit_fault)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            im.list_markers()
    assert len(list(source.parent.glob(source.name + ".migrating-*"))) == 1
    assert len(_rows("legacy_imports")) == int(after_commit)
    assert im.list_markers()[0]["attempts"] == 2
    assert len(_rows("legacy_imports")) == 1
    assert not list(source.parent.glob(source.name + ".migrating-*"))


@pytest.mark.parametrize("boundary", ["claim-sync", "unlink", "retirement-sync"])
def test_retirement_errors_keep_committed_evidence_and_recover(monkeypatch, boundary):
    h = im.marker_hash("/tmp")
    source = _artifact(h + ".json", json.dumps(_payload()).encode())
    sync = im._fsync_dir
    unlink = Path.unlink
    calls = []

    def failed_sync(directory):
        calls.append(directory)
        if (
            (boundary == "claim-sync" and len(calls) == 1)
            or boundary == "retirement-sync"
            and len(calls) == 2
        ):
            raise OSError("injected directory sync failure")
        return sync(directory)

    def failed_unlink(path, *args, **kwargs):
        if path.parent == source.parent and ".migrating-" in path.name:
            raise PermissionError("injected claim unlink failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(im, "_fsync_dir", failed_sync)
        if boundary == "unlink":
            patch.setattr(Path, "unlink", failed_unlink)
        with pytest.raises(OSError, match="injected"):
            im.list_markers()
    assert len(_rows("legacy_imports")) == (0 if boundary == "claim-sync" else 1)
    assert im.list_markers()[0]["attempts"] == 2
    assert len(_rows("legacy_imports")) == 1


def _schema_one(*, corrupt=False):
    im.marker_dir().mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(im.database_path())
    try:
        db.executescript(im._SCHEMA)
        db.execute(
            "CREATE TABLE legacy_imports(path TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL, "
            "source_mtime_ns INTEGER NOT NULL, imported_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO legacy_imports VALUES(?,?,?,?)",
            (
                "legacy.json",
                hashlib.sha256(b"old").hexdigest(),
                "corrupt" if corrupt else -123,
                45.0,
            ),
        )
        db.execute("INSERT INTO queue_meta VALUES('schema_version','1')")
        db.execute("PRAGMA user_version=1")
        db.commit()
    finally:
        db.close()


def test_schema_upgrade_retains_history_and_old_reader_refuses(monkeypatch):
    _schema_one()
    assert im.list_markers() == []
    receipt = _rows("legacy_imports")[0]
    assert bytes(receipt["source_name_bytes"]) == b"legacy.json"
    assert receipt["source_mtime_ns"] == "-123" and receipt["raw_payload"] is None
    assert receipt["reason"] == "legacy-metadata-only"
    before = im.database_path().read_bytes()
    monkeypatch.setattr(im, "SCHEMA_VERSION", 1)
    with pytest.raises(RuntimeError, match="newer than supported"):
        im.list_markers()
    assert im.database_path().read_bytes() == before


@pytest.mark.parametrize("fault", ["corrupt", "mid-copy"])
def test_failed_upgrade_keeps_original_schema_and_metadata(monkeypatch, fault):
    _schema_one(corrupt=fault == "corrupt")
    original = im._legacy.upgrade_receipts

    def interrupted(db):
        original(db)
        raise RuntimeError("injected interruption after metadata copy")

    if fault == "mid-copy":
        monkeypatch.setattr(im._legacy, "upgrade_receipts", interrupted)
    with pytest.raises(RuntimeError, match="invalid legacy import metadata|injected interruption"):
        im.list_markers()
    with sqlite3.connect(im.database_path()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT value FROM queue_meta WHERE key='schema_version'").fetchone()[0]
            == "1"
        )
        assert db.execute("SELECT path FROM legacy_imports").fetchall() == [("legacy.json",)]
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='legacy_imports_v1'").fetchall()
            == []
        )


def test_same_source_changed_bytes_and_metadata_retain_every_identity():
    h = im.marker_hash("/tmp")
    source = _artifact(h + ".json", json.dumps(_payload(tools="cbm")).encode())
    first_mtime = source.stat().st_mtime_ns
    im.list_markers()
    source = _artifact(h + ".json", json.dumps(_payload(tools="gitnexus")).encode())
    os.utime(source, ns=(first_mtime, first_mtime))
    assert im.list_markers()[0]["tools"] == "both"
    source = _artifact(h + ".json", json.dumps(_payload(tools="gitnexus")).encode())
    os.utime(source, ns=(first_mtime + 1000, first_mtime + 1000))
    im.list_markers()
    assert len(_rows("legacy_imports")) == 3


def test_import_status_absent_old_supported_and_truncated_are_readonly(monkeypatch):
    missing = im.marker_dir()
    assert json.loads(_cli("import-status").stdout)["state"] == "absent"
    assert not missing.exists()
    _schema_one()
    before = im.database_path().read_bytes()
    files = sorted(p.name for p in im.marker_dir().iterdir())
    old = _cli("import-status")
    assert old.returncode == 0 and json.loads(old.stdout) == {
        "state": "legacy-schema",
        "schema_version": 1,
        "receipts_available": False,
    }
    assert im.database_path().read_bytes() == before
    assert sorted(p.name for p in im.marker_dir().iterdir()) == files
    im.list_markers()
    _artifact("bad.json", b"{invalid")
    im.list_markers()
    before = im.database_path().read_bytes()
    files = sorted(p.name for p in im.marker_dir().iterdir())
    for limit in [0, 1, 20]:
        result = _cli("import-status", "--limit", str(limit))
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(result.stdout)
        assert data["total"] == 2 and data["counts"] == {"imported": 1, "quarantined": 1}
        assert data["displayed"] == min(2, limit) and data["truncated"] == (limit < 2)
        assert data["limit"] == limit
    assert im.database_path().read_bytes() == before
    assert sorted(p.name for p in im.marker_dir().iterdir()) == files


@pytest.mark.parametrize("state", ["newer", "busy", "invalid", "wal"])
def test_import_status_refuses_unsupported_or_busy_without_side_effects(state):
    im.list_markers()
    db = sqlite3.connect(im.database_path(), isolation_level=None)
    try:
        if state == "newer":
            db.execute("PRAGMA user_version=99")
        elif state == "wal":
            assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        if state != "busy":
            db.close()
    if state == "invalid":
        im.database_path().write_bytes(b"not sqlite")
    before = im.database_path().read_bytes()
    files = sorted(p.name for p in im.marker_dir().iterdir())
    if state == "busy":
        # Closing ANY fd for this inode cancels this process's POSIX locks.
        # Take the byte snapshot before acquiring the real SQLite blocker.
        db.execute("BEGIN EXCLUSIVE")
    try:
        result = _cli("import-status")
        assert result.returncode == 2
        assert json.loads(result.stdout)["state"] == "error"
        assert im.database_path().read_bytes() == before
        assert sorted(p.name for p in im.marker_dir().iterdir()) == files
    finally:
        if state == "busy":
            db.rollback()
            db.close()


def test_import_status_read_failure_is_explicit_and_never_prepares(monkeypatch, capsys):
    im.list_markers()
    original = Path.open

    def failed_read(path, *args, **kwargs):
        if path == im.database_path():
            raise PermissionError("injected diagnostic read IO")
        return original(path, *args, **kwargs)

    def forbidden_prepare():
        pytest.fail("import-status must not prepare or migrate")

    monkeypatch.setattr(Path, "open", failed_read)
    monkeypatch.setattr(im, "_prepare", forbidden_prepare)
    assert im.main(["import-status"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "error" and "injected diagnostic read IO" in data["error"]


def test_cli_poison_and_valid_request_ownership_roundtrip():
    h = im.marker_hash("/tmp")
    _artifact("0" * 16 + ".json", b"{poison")
    _artifact(h + ".inflight.json", json.dumps(_payload(claim_id="claim-1")).encode())
    _artifact(
        ".outcome-" + h,
        json.dumps(
            {"version": 1, "claim_id": "claim-1", "action": "restore", "recorded_at": 456}
        ).encode(),
    )
    replay = _cli("reconcile-inflight")
    assert replay.returncode == 0, replay.stderr
    listed = _cli("list")
    assert listed.returncode == 0 and listed.stdout.split("\t")[4] == "2"
    claimed = _cli("claim", "--hash", h)
    assert claimed.returncode == 0
    nonce = claimed.stdout.rstrip("\n").split("\t")[-1]
    assert _cli("consume", "--hash", h, "--claim-id", nonce).returncode == 0
    assert _cli("list").stdout == ""
    status = json.loads(_cli("import-status").stdout)
    assert status["counts"] == {"imported": 2, "quarantined": 1}
