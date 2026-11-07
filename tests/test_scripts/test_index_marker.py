"""Behavioral, durability, concurrency, and migration tests for the index queue."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MARKER_PY = _ROOT / "scripts/lib/index_marker.py"
_spec = importlib.util.spec_from_file_location("index_marker", _MARKER_PY)
im = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(im)


def _home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))


def _legacy(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def _queue_payload(**overrides) -> dict:
    return {
        "repo_path": "/tmp",
        "tools": "cbm",
        "mode": "fast",
        "requested_at": 123.0,
        "attempts": 0,
        **overrides,
    }


def _import_receipts():
    db = sqlite3.connect(im.database_path().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        return [dict(row) for row in db.execute("SELECT * FROM legacy_imports")]
    finally:
        db.close()


@pytest.mark.parametrize("poison_hash", ["0" * 16, "f" * 16], ids=["poison-first", "poison-last"])
def test_poison_enqueue_import_retains_evidence_and_valid_sibling(
    tmp_path, monkeypatch, poison_hash
):
    _home(tmp_path, monkeypatch)
    good_repo = "/tmp/legacy-valid"
    good_hash = im.marker_hash(good_repo)
    assert "0" * 16 < good_hash < "f" * 16
    poison = im.marker_dir() / f".spool-{poison_hash}-{'a' * 32}.spool"
    valid = im.marker_dir() / f".spool-{good_hash}-{'b' * 32}.spool"
    raw = json.dumps(_queue_payload(repo_path="\ud800")).encode()
    poison.parent.mkdir(parents=True)
    poison.write_bytes(raw)
    _legacy(valid, _queue_payload(repo_path=good_repo, tools="gitnexus"))

    rows = im.list_markers()

    assert [(row["hash"], row["tools"]) for row in rows] == [(good_hash, "gitnexus")]
    receipts = _import_receipts()
    bad = [r for r in receipts if r["content_sha256"] == hashlib.sha256(raw).hexdigest()]
    assert len(bad) == 1 and bad[0]["disposition"] == "quarantined"
    assert bytes(bad[0]["raw_payload"]) == raw
    assert im.get_failed(poison_hash) is None
    assert not poison.exists() and not valid.exists()


def test_valid_legacy_import_preserves_oversized_metadata_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp/legacy-metadata")
    path = im.marker_dir() / f"{h}.json"
    _legacy(path, _queue_payload(repo_path="/tmp/legacy-metadata"))
    original_stat = Path.stat
    oversized = 2**70

    def large_metadata(candidate, *args, **kwargs):
        value = original_stat(candidate, *args, **kwargs)
        if candidate.parent == im.marker_dir() and candidate.name.startswith(
            path.name + ".migrating-"
        ):
            return SimpleNamespace(st_mode=value.st_mode, st_mtime_ns=oversized)
        return value

    monkeypatch.setattr(Path, "stat", large_metadata)

    assert im.list_markers()[0]["hash"] == h
    receipts = _import_receipts()
    assert len(receipts) == 1 and receipts[0]["source_mtime_ns"] == str(oversized)
    assert receipts[0]["disposition"] == "imported"


def test_legacy_read_io_failure_stays_visible_and_recoverable(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp/legacy-io")
    path = im.marker_dir() / f"{h}.json"
    _legacy(path, _queue_payload(repo_path="/tmp/legacy-io", attempts=2))
    original_read = Path.read_bytes

    def unreadable(candidate):
        if candidate.parent == im.marker_dir() and candidate.name.startswith(
            path.name + ".migrating-"
        ):
            raise PermissionError("injected owned legacy read failure")
        return original_read(candidate)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(PermissionError, match="injected owned legacy read failure"):
        im.list_markers()
    assert len(list(im.marker_dir().glob(path.name + ".migrating-*"))) == 1
    assert _import_receipts() == []
    monkeypatch.setattr(Path, "read_bytes", original_read)
    rows = im.list_markers()
    assert len(rows) == 1 and rows[0]["attempts"] == 2
    assert im.get_failed(h) is None


@pytest.mark.parametrize("path", ["/tmp/example-repo", "/tmp", str(_ROOT)])
def test_hash_matches_entrypoint_algorithm(path):
    canonical = os.path.realpath(path)
    assert im.marker_hash(path) == hashlib.sha1(canonical.encode()).hexdigest()[:16]


def test_hash_matches_live_bash_sha1sum():
    canonical = os.path.realpath("/tmp")
    result = subprocess.run(
        ["bash", "-c", "printf '%s' \"$1\" | sha1sum | cut -c1-16", "_", canonical],
        text=True,
        capture_output=True,
        check=True,
    )
    assert im.marker_hash("/tmp") == result.stdout.strip()


def test_sqlite_queue_uses_full_sync_without_wal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    with closing(sqlite3.connect(im.database_path())) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_newer_schema_fails_loud_instead_of_downgrading(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.marker_dir().mkdir(parents=True)
    with closing(sqlite3.connect(im.database_path())) as db:
        # Default startup remains4 while explicit schema5 is supported.
        future = max(im.SCHEMA_VERSION, im._recovery.SCHEMA_VERSION) + 1
        db.execute(f"PRAGMA user_version={future}")
        db.commit()
    with pytest.raises(RuntimeError, match="newer than supported"):
        im.list_markers()


@pytest.mark.parametrize("tools", ["bad", "CBM", ""])
def test_write_rejects_invalid_tools(tmp_path, monkeypatch, tools):
    _home(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        im.write_marker("/tmp", tools, "fast")


@pytest.mark.parametrize("mode", ["bad", "FAST", ""])
def test_write_rejects_invalid_modes(tmp_path, monkeypatch, mode):
    _home(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        im.write_marker("/tmp", "cbm", mode)


def test_write_list_and_coalesce(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert im.write_marker("/tmp", "cbm", "fast") == im.database_path()
    first = im.list_markers()[0]
    im.write_marker("/tmp/.", "gitnexus", "full")
    rows = {row["provider"]: row for row in im.list_markers()}
    assert set(rows) == {"cbm", "gitnexus"}
    assert rows["cbm"]["requested_at"] == first["requested_at"]
    assert rows["cbm"]["mode"] == "fast" and rows["gitnexus"]["mode"] == "full"
    assert all(
        row["repo_path"] == "/tmp" and row["attempts"] == 0 and row["age_s"] >= 0
        for row in rows.values()
    )


def test_concurrent_thread_writers_do_not_lose_widening(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    requests = [("cbm", "full"), ("gitnexus", "fast")] * 8
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(im.write_marker, "/tmp", tool, mode) for tool, mode in requests]
        for future in futures:
            future.result(timeout=10)
    rows = {row["provider"]: row for row in im.list_markers()}
    assert {provider: row["mode"] for provider, row in rows.items()} == {
        "cbm": "full",
        "gitnexus": "fast",
    }


def test_database_write_lock_durably_spools_without_losing_widening(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    queue_dir = tmp_path / ".genesis" / "index-requests"
    blocker = sqlite3.connect(queue_dir / "queue.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        spool = im.write_marker("/tmp", "gitnexus", "full")
        assert spool.exists()
        assert spool.suffix == ".spool"
    finally:
        blocker.rollback()
        blocker.close()
    rows = {row["provider"]: row for row in im.list_markers()}
    assert {provider: row["mode"] for provider, row in rows.items()} == {
        "cbm": "fast",
        "gitnexus": "full",
    }
    assert not spool.exists()


def test_failed_transaction_rolls_back_entire_coalesce(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    before = im.list_markers()[0]
    original = im._coalesce_pending

    def fail_after_write(db, h, data):
        original(db, h, data)
        raise RuntimeError("injected after SQL mutation")

    monkeypatch.setattr(im, "_coalesce_pending", fail_after_write)
    with pytest.raises(RuntimeError):
        im.write_marker("/tmp", "gitnexus", "full")
    monkeypatch.setattr(im, "_coalesce_pending", original)
    assert im.list_markers()[0] == before


def test_process_death_with_uncommitted_write_recovers_cleanly(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    h = im.marker_hash("/tmp")
    im.claim(h, provider="cbm")
    code = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute('PRAGMA synchronous=FULL')
db.execute('BEGIN IMMEDIATE')
db.execute("DELETE FROM inflight")
os._exit(9)
"""
    result = subprocess.run(["python3", "-c", code, str(im.database_path())])
    assert result.returncode == 9
    assert im.get_inflight(h) is not None
    with closing(sqlite3.connect(im.database_path())) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_corrupt_database_fails_loud_instead_of_reporting_empty(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.marker_dir().mkdir(parents=True)
    im.database_path().write_bytes(b"not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        im.list_markers()


def test_claim_consume_and_concurrent_request(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "full")
    h = im.marker_hash("/tmp")
    claimed = im.claim(h, provider="cbm")
    assert claimed and claimed["claim_id"]
    assert im.get_inflight(h)["tools"] == "cbm"
    assert im.list_markers() == []
    im.write_marker("/tmp", "gitnexus", "fast")
    assert im.claim(h, provider="cbm") is None
    im.consume(claimed["context"])
    assert im.get_inflight(h) is None
    assert im.list_markers()[0]["tools"] == "gitnexus"


def test_restore_atomically_coalesces_pending_and_inflight(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "full")
    h = im.marker_hash("/tmp")
    context = im.claim(h, provider="cbm")["context"]
    im.write_marker("/tmp", "gitnexus", "fast")
    assert im.restore(context) == "pending"
    assert im.get_inflight(h) is None
    rows = {row["provider"]: row for row in im.list_markers()}
    assert {provider: (row["mode"], row["attempts"]) for provider, row in rows.items()} == {
        "cbm": ("full", 0),
        "gitnexus": ("fast", 0),
    }
    with closing(sqlite3.connect(im.database_path())) as db:
        assert db.execute("SELECT count(*) FROM pending WHERE hash=?", (h,)).fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM inflight WHERE hash=?", (h,)).fetchone()[0] == 0


def test_deferrals_never_burn_failure_budget(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "both", "fast")
    h = im.marker_hash("/tmp")
    for _ in range(im.MAX_ATTEMPTS * 2):
        context = im.claim(h, provider="cbm")["context"]
        assert im.restore(context) == "pending"
    assert im.list_markers()[0]["attempts"] == 0
    assert im.get_failed(h) is None


def test_genuine_failures_euthanize_at_exact_budget(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "both", "fast")
    h = im.marker_hash("/tmp")
    states = []
    for _ in range(im.MAX_ATTEMPTS):
        context = im.claim(h, provider="cbm")["context"]
        states.append(im.restore(context, attempts_inc=True))
    assert states == ["pending"] * (im.MAX_ATTEMPTS - 1) + ["failed"]
    assert [row["provider"] for row in im.list_markers()] == ["gitnexus"]
    assert (
        im.get_inflight(h) is None
        and im.get_failed(h, provider="cbm")["attempts"] == im.MAX_ATTEMPTS
    )


@pytest.mark.parametrize(
    ("action", "pending", "attempts", "backoff", "last_full"),
    [
        ("consume", False, None, False, False),
        ("consume_full", False, None, False, True),
        ("restore", True, 0, False, False),
        ("restore_backoff", True, 0, True, False),
        ("restore_failure", True, 1, False, False),
    ],
)
def test_remembered_outcomes_reconcile_exactly(
    tmp_path, monkeypatch, action, pending, attempts, backoff, last_full
):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "full")
    h = im.marker_hash("/tmp")
    context = im.claim(h, provider="cbm")["context"]
    im.remember_outcome(context, action)
    recorded = im.get_inflight(h)["outcome_recorded_at"]
    repended = im.repend_stale_inflight()
    assert bool(im.list_markers()) is pending
    assert (h in repended) is pending
    if pending:
        assert im.list_markers()[0]["attempts"] == attempts
    state = im.get_repo_state(h)
    assert (state["full_backoff"] is not None) is backoff
    assert (state["last_full"] is not None) is last_full
    if backoff:
        assert state["full_backoff"] == recorded
    if last_full:
        assert state["last_full"] == recorded
    assert im.get_inflight(h) is None


def test_busy_terminal_outcome_is_durably_spooled_and_replayed(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    h = im.marker_hash("/tmp")
    context = im.claim(h, provider="cbm")["context"]
    monkeypatch.setattr(im, "OUTCOME_LOCK_WAIT_S", 0.01)
    blocker = sqlite3.connect(im.database_path(), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        spool = im.remember_outcome(context, "consume")
        assert spool.exists() and spool.name.startswith(f".outcome-spool-{h}-")
    finally:
        blocker.rollback()
        blocker.close()

    assert im.repend_stale_inflight() == []
    assert im.get_inflight(h) is None
    assert im.list_markers() == []
    assert not spool.exists()


def test_stale_outcome_spool_cannot_apply_to_a_newer_claim(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    h = im.marker_hash("/tmp")
    old = im.claim(h, provider="cbm")["context"]
    im.consume(old)
    im.write_marker("/tmp", "cbm", "full")
    current = im.claim(h, provider="cbm")["context"]
    event = im._legs.scalar_event(old, "consume", 123, im.marker_hash)
    spool = im._write_outcome_spool(old, event)
    im.list_markers()  # Imports the late duplicate against retained old history.
    assert im.get_inflight(h)["execution_seq"] == current["execution_seq"]
    assert im.apply_remembered_outcome(current) is None
    assert im.apply_remembered_outcome(old) == "consumed"
    assert not spool.exists() and _import_receipts()[0]["disposition"] == "imported"
    assert im.repend_stale_inflight() == [h]  # Protocol2 never had a native-intent path.
    assert im.list_markers()[0]["attempts"] == 0


def test_conflicting_outcome_spools_preserve_claim_for_reconciliation(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    h = im.marker_hash("/tmp")
    context = im.claim(h, provider="cbm")["context"]
    first = im._write_outcome_spool(
        context, im._legs.scalar_event(context, "consume", 1, im.marker_hash)
    )
    second = im._write_outcome_spool(
        context, im._legs.scalar_event(context, "restore", 2, im.marker_hash)
    )
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        im.repend_stale_inflight()
    assert im.list_markers() == [] and im.get_inflight(h)["claim_id"] == context["claim_id"]
    with closing(sqlite3.connect(im.database_path())) as db:
        reason, evidence = db.execute(
            "SELECT conflict_reason,conflict_raw FROM executions"
        ).fetchone()
    assert reason == "conflicting durable outcomes" and bytes(evidence)
    assert not first.exists() and not second.exists()
    assert len(_import_receipts()) == 2 and all(row["raw_payload"] for row in _import_receipts())


def test_conflicting_direct_outcomes_preserve_claim_for_reconciliation(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    h = im.marker_hash("/tmp")
    context = im.claim(h, provider="cbm")["context"]
    im.remember_outcome(context, "consume")
    with pytest.raises(RuntimeError, match="conflicting durable queue outcomes"):
        im.remember_outcome(context, "restore")
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        im.repend_stale_inflight()
    assert im.get_inflight(h)["attempts"] == 0 and im.list_markers() == []


def test_protocol2_claim_without_native_intent_restores_without_failure_charge(
    tmp_path, monkeypatch
):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "full")
    h = im.marker_hash("/tmp")
    for _ in range(im.MAX_ATTEMPTS * 2):
        assert im.claim(h, provider="cbm")
        assert im.repend_stale_inflight() == [h]
    assert im.list_markers()[0]["attempts"] == 0 and im.get_failed(h) is None


def test_exhausted_inflight_never_terminalizes_new_pending_work(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    h = im.marker_hash("/tmp")
    for _ in range(im.MAX_ATTEMPTS - 1):
        context = im.claim(h, provider="cbm")["context"]
        im.restore(context, attempts_inc=True)
    context = im.claim(h, provider="cbm")["context"]
    im.write_marker("/tmp", "gitnexus", "full")
    assert im.restore(context, attempts_inc=True) == "failed"
    pending = im.list_markers()[0]
    assert (pending["tools"], pending["mode"], pending["attempts"]) == ("gitnexus", "full", 0)
    failed = im.get_failed(h, provider="cbm")
    assert (failed["tools"], failed["mode"], failed["attempts"]) == ("cbm", "fast", im.MAX_ATTEMPTS)


def test_full_escalation_and_backoff_clock(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    assert im.should_escalate_full(h)
    im.stamp_full(h)
    assert not im.should_escalate_full(h)
    im.stamp_full(h, timestamp=time.time() - im.FULL_INTERVAL_S - 1)
    assert im.should_escalate_full(h)
    im.mark_full_backoff(h)
    assert not im.should_escalate_full(h)
    im.mark_full_backoff(h, timestamp=time.time() - im.FULL_BACKOFF_S - 1)
    assert im.should_escalate_full(h)
    im.stamp_full(h)
    assert im.get_repo_state(h)["full_backoff"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "123"])
def test_full_timestamps_reject_non_finite_or_non_numeric_values(tmp_path, monkeypatch, value):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    with pytest.raises(ValueError):
        im.stamp_full(h, timestamp=value)
    with pytest.raises(ValueError):
        im.mark_full_backoff(h, timestamp=value)


def test_legacy_pending_is_imported_idempotently(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.json"
    _legacy(path, _queue_payload(attempts=2))
    assert im.list_markers()[0]["attempts"] == 2
    assert not path.exists()
    assert im.list_markers()[0]["attempts"] == 2


def test_identical_legacy_request_recreated_later_is_not_deduped_away(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.json"
    payload = _queue_payload()
    _legacy(path, payload)
    assert im.list_markers()
    context = im.claim(h, provider="cbm")["context"]
    im.consume(context)
    time.sleep(0.001)
    _legacy(path, payload)
    assert len(im.list_markers()) == 1
    assert not path.exists()
    assert im.list_markers()[0]["attempts"] == 0


def test_migration_never_unlinks_a_concurrently_replaced_legacy_marker(tmp_path, monkeypatch):
    """A pre-SQLite writer may replace the pathname while migration is active."""
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.json"
    _legacy(path, _queue_payload(tools="cbm", mode="fast"))
    original_import = im._import_one
    replaced = False

    def replace_during_import(db, source_path, raw, artifact_identity=None):
        nonlocal replaced
        result = original_import(db, source_path, raw, artifact_identity)
        if source_path.name == path.name and not replaced:
            replaced = True
            _legacy(path, _queue_payload(tools="gitnexus", mode="full"))
        return result

    monkeypatch.setattr(im, "_import_one", replace_during_import)
    first = im.list_markers()[0]
    assert (first["tools"], first["mode"]) == ("cbm", "fast")
    assert path.exists(), "the replacement written after the claim must survive"
    second = {row["provider"]: row for row in im.list_markers()}
    assert {provider: row["mode"] for provider, row in second.items()} == {
        "cbm": "fast",
        "gitnexus": "full",
    }
    assert not path.exists()


def test_crash_left_migration_claim_is_recovered(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    claim = im.marker_dir() / f"{h}.json.migrating-{'a' * 32}"
    _legacy(claim, _queue_payload(tools="gitnexus", mode="full"))

    row = im.list_markers()[0]

    assert (row["tools"], row["mode"]) == ("gitnexus", "full")
    assert not claim.exists()


def test_unreadable_legacy_claim_remains_visible_until_access_is_repaired(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.json"
    _legacy(path, _queue_payload())
    path.chmod(0)

    try:
        with pytest.raises(PermissionError):
            im.list_markers()
        claims = list(im.marker_dir().glob(f"{h}.json.migrating-*"))
        assert len(claims) == 1 and _import_receipts() == []
        claims[0].chmod(0o600)
        assert im.list_markers()[0]["tools"] == "cbm"
        assert im.get_failed(h) is None
    finally:
        for candidate in [path, *im.marker_dir().glob(f"{h}.json.migrating-*")]:
            if candidate.exists():
                candidate.chmod(0o600)


def test_temporary_spool_files_are_never_discovered_as_legacy_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    claim_id = "a" * 32
    outcome_tmp = im.marker_dir() / f".outcome-spool-{h}-{claim_id}-partial.tmp"
    enqueue_tmp = im.marker_dir() / f".spool-{h}-partial.tmp"
    _legacy(outcome_tmp, "partial")
    _legacy(enqueue_tmp, "partial")

    assert outcome_tmp not in im._legacy_files()
    assert enqueue_tmp not in im._legacy_files()


def test_malformed_enqueue_spool_is_quarantined(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    spool = im.marker_dir() / f".spool-{h}-{'b' * 32}.spool"
    _legacy(spool, "{truncated")

    assert im.list_markers() == []
    receipt = _import_receipts()[0]
    assert receipt["reason"] == "invalid-json" and receipt["disposition"] == "quarantined"
    assert bytes(receipt["raw_payload"]) == b"{truncated"
    assert im.get_failed(h) is None
    assert not spool.exists()


def test_crash_left_legacy_file_is_not_imported_twice(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.json"
    payload = _queue_payload()
    _legacy(path, payload)
    original_mtime = path.stat().st_mtime_ns
    assert im.list_markers()
    context = im.claim(h, provider="cbm")["context"]
    im.consume(context)
    # Recreate the exact inode metadata shape that survives commit-before-unlink.
    _legacy(path, payload)
    os.utime(path, ns=(original_mtime, original_mtime))
    assert im.list_markers() == []
    assert not path.exists()


def test_legacy_pending_and_inflight_both_survive_migration(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    _legacy(im.marker_dir() / f"{h}.json", _queue_payload(tools="gitnexus"))
    _legacy(im.marker_dir() / f"{h}.inflight.json", _queue_payload(mode="full", claim_id="claim-1"))
    assert im.list_markers()[0]["tools"] == "gitnexus"
    old = im.get_inflight(h)
    assert old["claim_id"] == "claim-1" and old["protocol"] == 1
    assert im.repend_stale_inflight() == [] and im.get_inflight(h) == old
    assert im.claim(h, provider="gitnexus") is None


def test_deployed_legacy_inflight_without_claim_id_is_preserved_unqualified(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.inflight.json"
    _legacy(path, _queue_payload(mode="full", attempts=2))
    assert im.repend_stale_inflight() == [] and im.list_markers() == []
    row = im.get_inflight(h)
    assert row["mode"] == "full" and row["attempts"] == 2 and row["protocol"] == 1
    assert row["legacy_unqualified"] == 1 and len(row["claim_id"]) == 32
    assert im.get_failed(h) is None and not path.exists()


def test_legacy_remembered_outcome_attaches_after_inflight(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    _legacy(im.marker_dir() / f"{h}.inflight.json", _queue_payload(claim_id="claim-1"))
    _legacy(
        im.outcome_path(h),
        {"version": 1, "claim_id": "claim-1", "action": "consume", "recorded_at": 456.0},
    )
    assert im.repend_stale_inflight() == [] and im.list_markers() == []
    row = im.get_inflight(h)
    assert (
        row["protocol"] == 1
        and row["outcome_action"] == "consume"
        and row["outcome_recorded_at"] == 456
    )


@pytest.mark.parametrize("payload", ["{truncated", "[]", "{}", '{"requested_at":NaN}'])
def test_malformed_legacy_inflight_is_quarantined(tmp_path, monkeypatch, payload):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.inflight.json"
    _legacy(path, payload)
    assert im.repend_stale_inflight() == []
    receipt = _import_receipts()[0]
    assert receipt["disposition"] == "quarantined"
    assert bytes(receipt["raw_payload"]).decode() == payload
    assert im.get_failed(h) is None
    assert not path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        _queue_payload(attempts=10**100),
        _queue_payload(attempts=2**63 - 1),
        _queue_payload(requested_at=10**1000),
    ],
)
def test_legacy_numbers_outside_sqlite_range_are_quarantined(tmp_path, monkeypatch, payload):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.json"
    _legacy(path, payload)

    assert im.list_markers() == []
    assert _import_receipts()[0]["reason"] == "invalid-queue-fields"
    assert im.get_failed(h) is None
    assert not path.exists()


def test_legacy_full_timestamps_migrate(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    _legacy(im.last_full_path(h), "123.0\n")
    _legacy(im.full_backoff_path(h), "456.0\n")
    state = im.get_repo_state(h)
    assert state["last_full"] == 123.0 and state["full_backoff"] == 456.0
    assert not im.last_full_path(h).exists() and not im.full_backoff_path(h).exists()


def test_cli_roundtrip_and_cross_process_coalesce(tmp_path):
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}

    def cli(*args, check=True):
        return subprocess.run(
            ["python3", str(_MARKER_PY), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=check,
        )

    requests = [("cbm", "full"), ("gitnexus", "fast")] * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(cli, "write", "--repo", "/tmp", "--tools", tool, "--mode", mode)
            for tool, mode in requests
        ]
        for future in futures:
            future.result(timeout=20)
    rows = [json.loads(line) for line in cli("list-legs").stdout.splitlines()]
    assert {(row["provider"], row["mode"], row["attempts"]) for row in rows} == {
        ("cbm", "full", 0),
        ("gitnexus", "fast", 0),
    }
    h = cli("hash", "--repo", "/tmp").stdout.strip()
    for provider in ("cbm", "gitnexus"):
        context = json.loads(cli("claim", "--hash", h, "--provider", provider).stdout)["context"]
        encoded = json.dumps(context)
        assert cli("remember-outcome", "--context", encoded, "--action", "consume").returncode == 0
        assert cli("apply-outcome", "--context", encoded).stdout.strip() == "consumed"
        assert cli("claim", "--hash", h, "--provider", provider, check=False).returncode == 1
    assert cli("list").stdout == ""


def test_cli_enqueue_spools_across_process_boundary_when_database_is_busy(tmp_path):
    """Exercise the exact CLI path used by the detached post-commit hook."""
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}

    def cli(*args):
        return subprocess.run(
            ["python3", str(_MARKER_PY), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )

    cli("write", "--repo", "/tmp", "--tools", "cbm", "--mode", "fast")
    blocker = sqlite3.connect(tmp_path / ".genesis" / "index-requests" / "queue.sqlite3")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        result = cli("write", "--repo", "/tmp", "--tools", "gitnexus", "--mode", "full")
        spool = Path(result.stdout.strip())
        assert spool.exists() and spool.suffix == ".spool"
    finally:
        blocker.rollback()
        blocker.close()

    rows = [json.loads(line) for line in cli("list-legs").stdout.splitlines()]
    assert {(row["provider"], row["mode"]) for row in rows} == {
        ("cbm", "fast"),
        ("gitnexus", "full"),
    }
    assert not spool.exists()


def test_cli_terminal_outcome_spools_across_process_boundary_when_database_is_busy(
    tmp_path,
):
    """Replay claim stdout -> CLI outcome -> durable spool -> reconciliation."""
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}

    def cli(*args):
        return subprocess.run(
            ["python3", str(_MARKER_PY), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )

    cli("write", "--repo", "/tmp", "--tools", "cbm", "--mode", "fast")
    h = cli("hash", "--repo", "/tmp").stdout.strip()
    context = json.loads(cli("claim", "--hash", h, "--provider", "cbm").stdout)["context"]
    claim_id = context["claim_id"]
    queue_dir = tmp_path / ".genesis" / "index-requests"
    blocker = sqlite3.connect(queue_dir / "queue.sqlite3", isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        result = cli(
            "remember-outcome",
            "--context",
            json.dumps(context),
            "--action",
            "consume",
        )
        spool = (
            Path(result.stdout.strip())
            if result.stdout.strip()
            else next(queue_dir.glob(f".outcome-spool-{h}-{claim_id}-*.spool"))
        )
        assert spool.exists()
    finally:
        blocker.rollback()
        blocker.close()

    cli("reconcile-inflight")
    assert cli("list").stdout == ""
    assert not spool.exists()
