"""Recover complete old failures and accept only bindable scalar timestamps."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_index_marker_leg_migration import _old_database, _rows
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


@pytest.mark.parametrize("provider", ["cbm", "gitnexus", "both"])
@pytest.mark.parametrize("raw", [None, b"old opaque bytes"], ids=["metadata-only", "raw-retained"])
def test_complete_old_failure_survives_every_latest_row_replacement(queue, tmp_path, provider, raw):
    db, hashes = _old_database(queue, tmp_path)
    h, repo = hashes[2]
    try:
        db.execute("UPDATE failed SET tools=?,raw_payload=? WHERE hash=?", (provider, raw, h))
        expected = dict(db.execute("SELECT * FROM failed WHERE hash=?", (h,)).fetchone())
    finally:
        db.close()
    queue.convert_queue()
    for leg in ("cbm", "gitnexus") if provider == "both" else (provider,):
        queue.write_marker(repo, leg, "fast")
        for _ in range(queue.MAX_ATTEMPTS):
            queue.restore(queue.claim(h, provider=leg)["context"], attempts_inc=True)
    history = [
        row
        for row in _rows(queue, "executions")
        if row["hash"] == h and row["entry_kind"] == "legacy-failure"
    ]
    assert len(history) == 1 and history[0]["legacy_raw"] == raw
    expected["raw_payload"] = raw.hex() if raw is not None else None
    assert json.loads(history[0]["legacy_failure_snapshot"]) == expected
    assert all(
        row["failed_at"] != expected["failed_at"]
        for row in _rows(queue, "failed")
        if row["hash"] == h
    )


@pytest.mark.parametrize("provider", ["cbm", "gitnexus", "both"])
def test_late_failure_retains_import_time_and_exact_raw_after_duplicate_replay(
    queue, tmp_path, monkeypatch, provider
):
    queue._prepare()
    repo = str(tmp_path / "old")
    h = queue.marker_hash(repo)
    clock = queue._legacy.time
    monkeypatch.setattr(
        queue._legacy,
        "time",
        SimpleNamespace(time=lambda: 109.25, monotonic=clock.monotonic, sleep=clock.sleep),
    )
    path = queue.marker_dir() / (h + ".failed.json")
    raw = json.dumps(
        dict(
            repo_path=repo,
            tools=provider,
            mode="fast",
            requested_at=1,
            attempts=4,
            extra_metadata=float("nan"),
        )
    ).encode()
    path.write_bytes(raw)
    stamp = path.stat().st_mtime_ns
    queue.list_markers()
    original = [row for row in _rows(queue, "executions") if row["entry_kind"] == "legacy-failure"]
    for leg in ("cbm", "gitnexus") if provider == "both" else (provider,):
        queue.write_marker(repo, leg, "fast")
        for _ in range(queue.MAX_ATTEMPTS):
            queue.restore(queue.claim(h, provider=leg)["context"], attempts_inc=True)
    path.write_bytes(raw)
    os.utime(path, ns=(stamp, stamp))
    queue.list_markers()
    history = [row for row in _rows(queue, "executions") if row["entry_kind"] == "legacy-failure"]
    assert history == original and len(history) == 1
    snapshot = json.loads(history[0]["legacy_failure_snapshot"])
    assert snapshot["failed_at"] == 109.25 and snapshot["attempts"] == 4
    assert snapshot["reason"] == "legacy terminal failure" and history[0]["legacy_raw"] == raw
    assert len(_rows(queue, "legacy_imports")) == 1 and not path.exists()
    assert all(row["reason"] == "attempt budget exhausted" for row in queue.list_failed())


@pytest.mark.parametrize("form", ["scalar-v2", "embedded-v1", "sidecar-v1"])
@pytest.mark.parametrize(
    "timestamp,accepted",
    [
        (123, True),
        (-(2**63), True),
        (2**63 - 1, True),
        (2**63, True),
        (10**100, True),
        (1.25, True),
        (-0.5, True),
        (True, False),
        (None, False),
        ("1", False),
        (float("nan"), False),
        (float("inf"), False),
        (10**400, False),
    ],
    ids=[
        "ordinary",
        "int64-min",
        "int64-max",
        "past-int64",
        "finite-large-int",
        "fraction",
        "negative",
        "bool",
        "null",
        "text",
        "nan",
        "infinity",
        "unrepresentable-int",
    ],
)
def test_scalar_timestamp_domains_import_without_poisoning_sibling(
    queue, tmp_path, form, timestamp, accepted
):
    repo = str(tmp_path / "repo")
    h = queue.marker_hash(repo)
    queue._prepare()
    context = None
    if form == "scalar-v2":
        queue.write_marker(repo, "cbm", "fast")
        context = queue.claim(h, provider="cbm")["context"]
        event = dict(version=2, kind="outcome", **context, action="consume", recorded_at=timestamp)
        path = queue.marker_dir() / f".outcome-spool-{h}-{context['claim_id']}-{'a' * 32}.spool"
    else:
        event = dict(
            repo_path=repo, tools="cbm", mode="fast", requested_at=1, attempts=0, claim_id="old"
        )
        path = queue.marker_dir() / (h + ".inflight.json")
        if form == "embedded-v1":
            event.update(outcome_action="consume", outcome_recorded_at=timestamp)
        else:
            path.write_text(json.dumps(event))
            queue.list_markers()
            path = queue.marker_dir() / (".outcome-" + h)
            event = dict(version=1, claim_id="old", action="consume", recorded_at=timestamp)
    raw = json.dumps(event).encode()
    path.write_bytes(raw)
    stamp = path.stat().st_mtime_ns
    sibling = str(tmp_path / "sibling")
    sibling_path = queue.marker_dir() / (queue.marker_hash(sibling) + ".json")
    sibling_path.write_text(
        json.dumps(
            dict(repo_path=sibling, tools="gitnexus", mode="fast", requested_at=1, attempts=0)
        )
    )
    rows = queue.list_markers()
    assert len(rows) == 1 and rows[0]["repo_path"] == sibling
    receipt = [
        row
        for row in _rows(queue, "legacy_imports")
        if row["content_sha256"] == hashlib.sha256(raw).hexdigest()
    ]
    assert len(receipt) == 1 and receipt[0]["raw_payload"] == raw and not path.exists()
    assert receipt[0]["disposition"] == ("imported" if accepted else "quarantined")
    if accepted:
        execution = [row for row in _rows(queue, "executions") if row["hash"] == h][0]
        assert execution["scalar_action"] == "consume" and execution["recorded_at"] == float(
            timestamp
        )
        if context is not None:
            assert queue.apply_remembered_outcome(context) == "consumed"
        before = _rows(queue, "executions")
        path.write_bytes(raw)
        os.utime(path, ns=(stamp, stamp))
        queue.list_markers()
        assert _rows(queue, "executions") == before
        assert len(_rows(queue, "legacy_imports")) == (2 if form != "sidecar-v1" else 3)
    with closing(sqlite3.connect(queue.database_path())) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
