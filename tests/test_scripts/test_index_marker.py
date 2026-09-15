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
    with sqlite3.connect(im.database_path()) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_newer_schema_fails_loud_instead_of_downgrading(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.marker_dir().mkdir(parents=True)
    with sqlite3.connect(im.database_path()) as db:
        db.execute(f"PRAGMA user_version={im.SCHEMA_VERSION + 1}")
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
    time.sleep(0.01)
    im.write_marker("/tmp/.", "gitnexus", "full")
    rows = im.list_markers()
    assert len(rows) == 1
    assert rows[0]["repo_path"] == "/tmp"
    assert rows[0]["tools"] == "both"
    assert rows[0]["mode"] == "full"
    assert rows[0]["requested_at"] == first["requested_at"]
    assert rows[0]["attempts"] == 0
    assert rows[0]["age_s"] >= 0


def test_concurrent_thread_writers_do_not_lose_widening(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    requests = [("cbm", "full"), ("gitnexus", "fast")] * 8
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(im.write_marker, "/tmp", tool, mode) for tool, mode in requests]
        for future in futures:
            future.result(timeout=10)
    row = im.list_markers()[0]
    assert (row["tools"], row["mode"]) == ("both", "full")


def test_database_write_lock_times_out_without_mutation(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "fast")
    before = im.list_markers()[0]
    blocker = sqlite3.connect(im.database_path(), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(TimeoutError):
            im.write_marker("/tmp", "gitnexus", "full")
    finally:
        blocker.rollback()
        blocker.close()
    assert im.list_markers()[0] == before


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
    im.claim(h)
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
    with sqlite3.connect(im.database_path()) as db:
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
    claimed = im.claim(h)
    assert claimed and claimed["claim_id"]
    assert im.get_inflight(h)["tools"] == "cbm"
    assert im.list_markers() == []
    im.write_marker("/tmp", "gitnexus", "fast")
    assert im.claim(h) is None
    im.consume(h)
    assert im.get_inflight(h) is None
    assert im.list_markers()[0]["tools"] == "gitnexus"


def test_restore_atomically_coalesces_pending_and_inflight(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "full")
    h = im.marker_hash("/tmp")
    im.claim(h)
    im.write_marker("/tmp", "gitnexus", "fast")
    assert im.restore(h) == "pending"
    assert im.get_inflight(h) is None
    row = im.list_markers()[0]
    assert (row["tools"], row["mode"], row["attempts"]) == ("both", "full", 0)
    with sqlite3.connect(im.database_path()) as db:
        assert db.execute("SELECT count(*) FROM pending WHERE hash=?", (h,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM inflight WHERE hash=?", (h,)).fetchone()[0] == 0


def test_deferrals_never_burn_failure_budget(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "both", "fast")
    h = im.marker_hash("/tmp")
    for _ in range(im.MAX_ATTEMPTS * 2):
        assert im.claim(h)
        assert im.restore(h) == "pending"
    assert im.list_markers()[0]["attempts"] == 0
    assert im.get_failed(h) is None


def test_genuine_failures_euthanize_at_exact_budget(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "both", "fast")
    h = im.marker_hash("/tmp")
    states = []
    for _ in range(im.MAX_ATTEMPTS):
        assert im.claim(h)
        states.append(im.restore(h, attempts_inc=True))
    assert states == ["pending"] * (im.MAX_ATTEMPTS - 1) + ["failed"]
    assert im.list_markers() == [] and im.get_inflight(h) is None
    assert im.get_failed(h)["attempts"] == im.MAX_ATTEMPTS


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
    im.claim(h)
    im.remember_outcome(h, action)
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


def test_unknown_runner_deaths_exhaust_budget(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    im.write_marker("/tmp", "cbm", "full")
    h = im.marker_hash("/tmp")
    for _ in range(im.MAX_ATTEMPTS):
        assert im.claim(h)
        im.repend_stale_inflight()
    assert im.list_markers() == []
    assert im.get_failed(h)["attempts"] == im.MAX_ATTEMPTS


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
    im.claim(h)
    im.consume(h)
    time.sleep(0.001)
    _legacy(path, payload)
    assert len(im.list_markers()) == 1
    assert not path.exists()
    assert im.list_markers()[0]["attempts"] == 0


def test_crash_left_legacy_file_is_not_imported_twice(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.json"
    payload = _queue_payload()
    _legacy(path, payload)
    original_mtime = path.stat().st_mtime_ns
    assert im.list_markers()
    im.claim(h)
    im.consume(h)
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
    im.list_markers()
    assert im.list_markers()[0]["tools"] == "gitnexus"
    assert im.get_inflight(h)["claim_id"] == "claim-1"
    assert im.restore(h) == "pending"
    assert (im.list_markers()[0]["tools"], im.list_markers()[0]["mode"]) == ("both", "full")


def test_deployed_legacy_inflight_without_claim_id_is_retried(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.inflight.json"
    _legacy(path, _queue_payload(mode="full", attempts=2))
    assert im.repend_stale_inflight() == [h]
    row = im.list_markers()[0]
    assert row["mode"] == "full" and row["attempts"] == 3
    assert im.get_failed(h) is None
    assert not path.exists()


def test_legacy_remembered_outcome_attaches_after_inflight(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    _legacy(im.marker_dir() / f"{h}.inflight.json", _queue_payload(claim_id="claim-1"))
    _legacy(
        im.outcome_path(h),
        {"version": 1, "claim_id": "claim-1", "action": "consume", "recorded_at": 456.0},
    )
    assert im.repend_stale_inflight() == []
    assert im.get_inflight(h) is None and im.list_markers() == []


@pytest.mark.parametrize("payload", ["{truncated", "[]", "{}", '{"requested_at":NaN}'])
def test_malformed_legacy_inflight_is_quarantined(tmp_path, monkeypatch, payload):
    _home(tmp_path, monkeypatch)
    h = im.marker_hash("/tmp")
    path = im.marker_dir() / f"{h}.inflight.json"
    _legacy(path, payload)
    assert im.repend_stale_inflight() == []
    failed = im.get_failed(h)
    assert failed["reason"] == "malformed legacy inflight marker"
    assert bytes(failed["raw_payload"]).decode() == payload
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
    fields = cli("list").stdout.strip().split("\t")
    assert fields[1:5] == ["/tmp", "both", "full", "0"]
    h = cli("hash", "--repo", "/tmp").stdout.strip()
    assert cli("claim", "--hash", h).returncode == 0
    assert cli("list").stdout == ""
    assert cli("remember-outcome", "--hash", h, "--action", "consume").returncode == 0
    assert cli("apply-outcome", "--hash", h).stdout.strip() == "consumed"
    assert cli("claim", "--hash", h, check=False).returncode == 1
