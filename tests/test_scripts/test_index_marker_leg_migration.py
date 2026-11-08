"""Migration, provenance and retained identity across queue protocols."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _rows(queue, table):
    assert table in {"executions", "legacy_imports", "repo_state", "failed"}
    with closing(sqlite3.connect(queue.database_path())) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def _artifact(queue, h, suffix, data, *, tick=1):
    directory = queue.marker_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (h + suffix)
    raw = json.dumps(data, sort_keys=True).encode()
    path.write_bytes(raw)
    os.utime(path, ns=(tick, tick))
    return path, raw


def _request(repo, **extra):
    return dict(repo_path=repo, tools="both", mode="fast", requested_at=100, attempts=4, **extra)


def _old_database(queue, tmp_path, version=2, *, factory=sqlite3.Connection):
    """The still-supported legacy DDL; receipts differ between schemas1 and2."""
    queue.marker_dir().mkdir(parents=True)
    db = sqlite3.connect(queue.database_path(), isolation_level=None, factory=factory)
    db.row_factory = sqlite3.Row
    db.executescript(queue._SCHEMA)
    if version == 1:
        db.execute(
            "CREATE TABLE legacy_imports(path TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL, "
            "source_mtime_ns INTEGER NOT NULL, imported_at REAL NOT NULL)"
        )
        db.execute(
            "INSERT INTO legacy_imports VALUES(?,?,?,?)",
            ("old.json", hashlib.sha256(b"lost original").hexdigest(), -11, 12),
        )
    elif version == 2:
        db.execute(queue._legacy._RECEIPTS)
        db.execute(
            "INSERT INTO legacy_imports VALUES(?,?,?,?,?,?,?)",
            (
                b"old.json",
                hashlib.sha256(b"retained").hexdigest(),
                "-11",
                12,
                "quarantined",
                "owned-old-reason",
                b"retained",
            ),
        )
    hashes = []
    for number in range(4):
        repo = str(tmp_path / f"old-{number}")
        h = queue.marker_hash(repo)
        hashes.append((h, repo))
    h, repo = hashes[0]
    db.execute("INSERT INTO pending VALUES(?,?,?,?,?,?)", (h, repo, "both", "full", 100, 4))
    db.execute("INSERT INTO repo_state VALUES(?,?,?)", (h, 23, 29))
    h, repo = hashes[1]
    db.execute(
        "INSERT INTO inflight VALUES(?,?,?,?,?,?,?,?,?)",
        (h, repo, "both", "moderate", 101, 5, "old arbitrary nonce", "consume", 108),
    )
    h, repo = hashes[2]
    db.execute(
        "INSERT INTO failed VALUES(?,?,?,?,?,?,?,?,?)",
        (h, repo, "both", "full", 102, 6, "old terminal debt", b"old failed bytes", 109),
    )
    h, _ = hashes[3]
    db.execute(
        "INSERT INTO failed VALUES(?,?,?,?,?,?,?,?,?)",
        (h, None, None, None, None, None, "unassigned diagnostic", b"unassigned", 110),
    )
    db.execute("INSERT INTO queue_meta VALUES('schema_version',?)", (str(version),))
    db.execute(f"PRAGMA user_version={version}")
    return db, hashes


@pytest.mark.parametrize("version", [0, 1, 2])
def test_legacy_schema_upgrade_preserves_debt_clocks_diagnostics_and_receipts(
    queue, tmp_path, version
):
    db, hashes = _old_database(queue, tmp_path, version)
    db.close()
    with pytest.raises(queue.QueueConversionDeferred):
        queue.list_markers()
    queue.convert_queue()
    rows = queue.list_markers()
    assert len(rows) == 2 and {row["provider"] for row in rows} == {"cbm", "gitnexus"}
    assert all(
        row["attempts"] == 4
        and row["legacy_unqualified"] == 1
        and row["legacy_shared_attempts"] == [4]
        for row in rows
    )
    state = queue.get_repo_state(hashes[0][0])
    assert state["last_full"] == 23 and state["full_backoff"] == 29
    old = queue.get_inflight(hashes[1][0])
    assert old["claim_id"] == "old arbitrary nonce" and old["protocol"] == 1
    assert old["provider"] is None and old["outcome_action"] == "consume"
    assert queue.repend_stale_inflight() == []
    assert queue.get_inflight(hashes[1][0]) == old
    failed = queue.list_failed()
    terminal = [row for row in failed if row["hash"] == hashes[2][0]]
    assert len(terminal) == 2 and {row["provider"] for row in terminal} == {"cbm", "gitnexus"}
    assert all(
        row["attempts"] == 6
        and row["raw_payload"] == b"old failed bytes"
        and json.loads(row["legacy_shared_attempts"]) == [6]
        for row in terminal
    )
    unassigned = queue.get_failed(hashes[3][0])
    assert unassigned["legacy_diagnostic"] == 1 and unassigned["provider"] is None
    assert unassigned["raw_payload"] == b"unassigned"
    history = _rows(queue, "executions")
    assert len(history) == 2 and {row["entry_kind"] for row in history} == {
        "claim",
        "legacy-failure",
    }
    receipts = _rows(queue, "legacy_imports")
    assert len(receipts) == (0 if version == 0 else 1)
    if receipts:
        assert receipts[0]["raw_payload"] == (None if version == 1 else b"retained")
        assert receipts[0]["source_mtime_ns"] == "-11"
    with closing(sqlite3.connect(queue.database_path())) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            db.execute(
                "SELECT value FROM queue_meta WHERE key='automatic_protocol_ready'"
            ).fetchone()[0]
            == "0"
        )
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_every_schema_statement_failure_rolls_back_before_retry(queue, tmp_path):
    class FaultConnection(sqlite3.Connection):
        statement = 0
        fail_at = None

        def execute(self, sql, *args):
            self.statement += 1
            if self.statement == self.fail_at:
                raise RuntimeError("owned schema fault")
            return super().execute(sql, *args)

    db, _ = _old_database(queue, tmp_path, factory=FaultConnection)
    try:
        original = list(db.iterdump())
        with closing(
            sqlite3.connect(":memory:", isolation_level=None, factory=FaultConnection)
        ) as control:
            control.row_factory = sqlite3.Row
            db.backup(control)
            control.statement = 0
            queue._publish_schema(control)
            boundaries = control.statement
        assert boundaries > 20
        for boundary in range(1, boundaries + 1):
            db.statement, db.fail_at = 0, boundary
            with pytest.raises(RuntimeError, match="owned schema fault"):
                queue._publish_schema(db)
            db.fail_at = None
            assert list(db.iterdump()) == original
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert not db.in_transaction
        queue._publish_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        db.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_schema_commit_failure_leaves_only_old_or_complete_state(queue, tmp_path, after_commit):
    class FaultCommit(sqlite3.Connection):
        enabled = False

        def commit(self):
            if self.enabled and not after_commit:
                raise OSError("owned before commit")
            super().commit()
            if self.enabled:
                raise OSError("owned after commit")

    db, _ = _old_database(queue, tmp_path, factory=FaultCommit)
    try:
        original = list(db.iterdump())
        db.enabled = True
        with pytest.raises(OSError, match="owned .* commit"):
            queue._publish_schema(db)
        db.enabled = False
        if after_commit:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4
            assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 2
            assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 2
        else:
            assert list(db.iterdump()) == original
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_effective_debt_and_original_shared_debt_join_independently(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    h = queue.marker_hash(repo)
    with queue._write_db() as db:
        db.execute("UPDATE pending SET attempts=5 WHERE hash=?", (h,))
    for debt in (4, 0, 4):
        data = _request(repo)
        data["attempts"] = debt
        _artifact(queue, h, ".json", data, tick=debt + 20)
        queue.list_markers()
    rows = {row["provider"]: row for row in queue.list_markers()}
    assert rows["cbm"]["attempts"] == 5 and rows["cbm"]["legacy_shared_attempts"] == [0, 4]
    assert rows["gitnexus"]["attempts"] == 4 and rows["gitnexus"]["legacy_shared_attempts"] == [
        0,
        4,
    ]
    assert all(row["legacy_unqualified"] == 1 for row in rows.values())


def test_old_claim_duplicate_and_conflict_keep_first_identity_and_all_raw_receipts(queue, tmp_path):
    repo = str(tmp_path / "repo")
    h = queue.marker_hash(repo)
    data = _request(repo, claim_id="legacy-nonce")
    _, first_raw = _artifact(queue, h, ".inflight.json", data)
    queue.list_markers()
    first = queue.get_inflight(h)
    _, second_raw = _artifact(queue, h, ".inflight.json", {**data, "metadata": "retained"}, tick=2)
    queue.list_markers()
    assert queue.get_inflight(h) == first and len(_rows(queue, "executions")) == 1
    _, conflict_raw = _artifact(queue, h, ".inflight.json", {**data, "mode": "full"}, tick=3)
    queue.list_markers()
    history = _rows(queue, "executions")
    assert queue.get_inflight(h) == first and len(history) == 1
    assert history[0]["legacy_raw"] == first_raw and history[0]["conflict_raw"] == conflict_raw
    assert history[0]["conflict_reason"] == "conflicting legacy claim identity"
    assert {row["raw_payload"] for row in _rows(queue, "legacy_imports")} == {
        first_raw,
        second_raw,
        conflict_raw,
    }


def test_late_old_claim_and_outcomes_never_target_current_protocol(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    h = queue.marker_hash(repo)
    claimed = queue.claim(h, provider="cbm")
    old = _request(repo, claim_id=claimed["claim_id"])
    _artifact(queue, h, ".inflight.json", old)
    queue.list_markers()
    current = queue.get_inflight(h)
    assert current["protocol"] == 2 and current["claim_id"] == claimed["claim_id"]
    for tick, action in enumerate(("consume", "consume", "restore"), 2):
        _artifact(
            queue,
            ".outcome-" + h,
            "",
            dict(version=1, claim_id=claimed["claim_id"], action=action, recorded_at=tick),
            tick=tick,
        )
        queue.list_markers()
    history = {row["protocol"]: row for row in _rows(queue, "executions")}
    assert history[2]["scalar_action"] is None and history[1]["scalar_action"] == "consume"
    assert history[1]["conflict_reason"] == "conflicting legacy durable outcomes"
    assert (
        queue.get_inflight(h) == current
        and queue.apply_remembered_outcome(claimed["context"]) is None
    )
    assert len(_rows(queue, "legacy_imports")) == 4


def test_missing_old_nonce_has_stable_artifact_alias_and_explicit_origin(queue, tmp_path):
    repo = str(tmp_path / "repo")
    h = queue.marker_hash(repo)
    path, raw = _artifact(queue, h, ".inflight.json", _request(repo), tick=5)
    expected = hashlib.sha256(
        json.dumps(
            [h, 1, os.fsencode(path.name).hex(), hashlib.sha256(raw).hexdigest(), "5"],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]
    queue.list_markers()
    _artifact(queue, h, ".inflight.json", _request(repo), tick=5)
    queue.list_markers()
    history = _rows(queue, "executions")
    assert len(history) == 1 and history[0]["nonce_origin"] == "synthetic-artifact"
    assert history[0]["claim_id"] == expected and queue.get_inflight(h)["claim_id"] == expected
    assert len(_rows(queue, "legacy_imports")) == 1


def test_late_legacy_failure_remains_in_history_after_new_failure(queue, tmp_path):
    repo = str(tmp_path / "repo")
    h = queue.marker_hash(repo)
    _, raw = _artifact(queue, h, ".failed.json", _request(repo))
    queue.list_markers()
    queue.write_marker(repo, "cbm", "fast")
    for _ in range(queue.MAX_ATTEMPTS):
        claimed = queue.claim(h, provider="cbm")
        queue.restore(claimed["context"], attempts_inc=True)
    assert queue.get_failed(h, provider="cbm")["reason"] == "attempt budget exhausted"
    history = [row for row in _rows(queue, "executions") if row["entry_kind"] == "legacy-failure"]
    assert len(history) == 1 and history[0]["legacy_raw"] == raw
    assert history[0]["original_tools"] == "both" and history[0]["attempts"] == 4
    assert queue.get_failed(h, provider="gitnexus")["reason"] == "legacy terminal failure"


@pytest.mark.parametrize(
    "embedded",
    [
        {"outcome_action": "consume"},
        {"outcome_recorded_at": 1},
        {"outcome_action": "unknown", "outcome_recorded_at": 1},
        {"outcome_action": {}, "outcome_recorded_at": 1},
        {"outcome_action": "consume", "outcome_recorded_at": True},
        {"outcome_action": "consume", "outcome_recorded_at": "1"},
        {"outcome_action": "consume", "outcome_recorded_at": float("nan")},
    ],
)
def test_malformed_embedded_legacy_outcome_is_artifact_local(queue, tmp_path, embedded):
    repo = str(tmp_path / "old")
    h = queue.marker_hash(repo)
    path, raw = _artifact(queue, h, ".inflight.json", _request(repo, claim_id="old", **embedded))
    sibling = str(tmp_path / "sibling")
    _artifact(queue, queue.marker_hash(sibling), ".json", _request(sibling))
    rows = queue.list_markers()
    assert len(rows) == 2 and all(row["repo_path"] == sibling for row in rows)
    assert queue.get_inflight(h) is None and not path.exists()
    rejected = [
        row for row in _rows(queue, "legacy_imports") if row["disposition"] == "quarantined"
    ]
    assert len(rejected) == 1 and rejected[0]["raw_payload"] == raw
    assert rejected[0]["reason"] == "invalid-legacy-outcome-fields"
