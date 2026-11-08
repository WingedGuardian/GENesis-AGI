"""Explicit scratch conversion never upgrades the live/default queue implicitly."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_execution import _reservation
from tests.test_scripts.test_index_marker_legs import SCRIPT
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _upgrade(queue, db):
    helper = getattr(queue, "_recovery", None)
    assert helper is not None, "schema5 needs its transaction-only recovery helper"
    helper.upgrade(db, queue._legs, queue._native)


def _claim(queue, tmp_path):
    """Reconstruct existing schema4 history; new public authority must refuse4."""
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "fast")
    with closing(queue._connect()) as db, queue._transaction(db):
        return queue._native.claim(db, queue.marker_hash(repo), "cbm", queue._legs)


def _reserve(queue, claimed, data):
    with closing(queue._connect()) as db, queue._transaction(db):
        return queue._native.reserve(db, claimed["context"], data, queue._legs)


def _snapshot(db):
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: [tuple(r) for r in db.execute('SELECT * FROM "' + name + '"')] for name in names}


@pytest.mark.parametrize("name", ["x;DROP TABLE executions", 'a"b', "a b", "2column", "a-b"])
def test_rebuild_identifier_rejects_sql_syntax(queue, name):
    assert queue._native._identifier("terminal_sha256") == '"terminal_sha256"'
    with pytest.raises(RuntimeError):
        queue._native._identifier(name)


def test_explicit_upgrade_preserves_original_rows_blobs_fk_and_high_water(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    _reserve(queue, claimed, _reservation(claimed))
    with closing(queue._connect()) as db:
        old = _snapshot(db)
        old_execution = dict(db.execute("SELECT * FROM executions").fetchone())
        db.execute("UPDATE sqlite_sequence SET seq=4096 WHERE name='executions'")
        with queue._transaction(db):
            _upgrade(queue, db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT value FROM queue_meta WHERE key='schema_version'").fetchone()[0] == "5"
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='executions'").fetchone()[0] == 4096
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for name in old:
            if name in ("executions", "sqlite_sequence", "queue_meta"):
                continue
            assert _snapshot(db)[name] == old[name]
        row = dict(db.execute("SELECT * FROM executions").fetchone())
        assert {name: row[name] for name in old_execution} == old_execution
        assert row["control_closure"] is None
        state = {r[1] for r in db.execute("PRAGMA table_info(provider_state)")}
        assert {"hash", "repo_path", "provider", "hazard_revision", "accepted_sequence",
                "accepted_terminal_sha256", "accepted_control_sha256", "accepted_source_identity",
                "accepted_evidence_reference", "grant"} <= state
        assert db.execute("SELECT * FROM provider_state").fetchall() == []


def test_upgrade_failure_after_rebuild_rolls_back_every_original_byte(queue, tmp_path):
    _claim(queue, tmp_path)
    with closing(queue._connect()) as db:
        old, ddl = _snapshot(db), db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()

        def deny_index(action, name, *unused):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX and name == "executions_safety" else sqlite3.SQLITE_OK

        db.set_authorizer(deny_index)
        with pytest.raises(sqlite3.DatabaseError), queue._transaction(db):
            _upgrade(queue, db)
        db.set_authorizer(None)
        assert _snapshot(db) == old
        assert db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == ddl
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_conversion_requires_explicit_transaction_and_exact_supported_source(queue):
    queue._prepare()
    with closing(queue._connect()) as db:
        with pytest.raises(RuntimeError, match="transaction"):
            _upgrade(queue, db)
        db.execute("PRAGMA user_version=6")
        with pytest.raises(RuntimeError, match="schema"), queue._transaction(db):
            _upgrade(queue, db)


def test_existing_enqueue_keeps_default_schema4_and_readiness_false(queue, tmp_path):
    queue.write_marker(str(tmp_path / "repo"), "both", "full")
    with closing(queue._connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    helper = getattr(queue, "_recovery", None)
    assert helper is not None, "explicit schema5 support must not silently change default init"
    assert queue.SCHEMA_VERSION == 4 and helper.SCHEMA_VERSION == 5


def test_legacy_observation_has_no_fabricated_claim_or_new_inflight(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    with closing(queue._connect()) as db:
        with queue._transaction(db):
            _upgrade(queue, db)
        row = dict(db.execute("SELECT * FROM executions").fetchone())
        fields = ("hash", "repo_path", "provider", "protocol", "entry_kind", "claim_id", "original_tools",
                  "mode", "requested_at", "attempts", "legacy_unqualified", "legacy_shared_attempts",
                  "nonce_origin", "legacy_raw")
        row.update(protocol=1, entry_kind="legacy-observation", claim_id=None,
                   nonce_origin="artifact", legacy_raw=b"{original pending bytes}\x00")
        cursor = db.execute("INSERT INTO executions(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")",
                            tuple(row[name] for name in fields))
        assert cursor.lastrowid > claimed["context"]["execution_seq"]
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 1
        row["protocol"] = 3
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO executions(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")",
                       tuple(row[name] for name in fields))


def test_converted_queue_reopens_for_compatible_enqueue_without_downgrading(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    with closing(queue._connect()) as db, queue._transaction(db):
        _upgrade(queue, db)
    queue.write_marker(repo, "gitnexus", "full")
    rows = queue.list_markers()
    assert {r["provider"] for r in rows} == {"cbm", "gitnexus"}
    with closing(queue._connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT value FROM queue_meta WHERE key='schema_version'").fetchone()[0] == "5"


@pytest.mark.parametrize("version", [4, 5])
@pytest.mark.parametrize("tools", ["cbm", "gitnexus", "both"])
def test_busy_supported_enqueue_stays_modern_without_legacy_holds(
    queue, tmp_path, monkeypatch, version, tools
):
    queue._prepare()
    if version == 5:
        with closing(queue._connect()) as db, queue._transaction(db):
            _upgrade(queue, db)
    repo = str(tmp_path / "busy modern request 雪")
    api = queue.write_marker.__globals__
    monkeypatch.setitem(api, "LOCK_WAIT_S", 0.1)
    monkeypatch.setitem(api, "OUTCOME_LOCK_WAIT_S", 0.1)
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        try:
            spool = queue.write_marker(repo, tools, "fast")
        finally:
            holder.rollback()
    envelope = json.loads(spool.read_bytes())
    rows = queue.list_markers()
    with closing(queue._connect()) as db:
        observations = db.execute("SELECT count(*) FROM executions WHERE entry_kind='legacy-observation'").fetchone()[0] if version == 5 else 0
        states = db.execute("SELECT count(*) FROM provider_state").fetchone()[0] if version == 5 else 0
    assert observations == states == 0 and not any(row["legacy_unqualified"] for row in rows), (
        "busy modern enqueue became legacy", envelope.get("kind"), observations, states)
    assert envelope["kind"] == "enqueue"
    assert {row["provider"] for row in rows} == ({"cbm", "gitnexus"} if tools == "both" else {tools})
    assert not spool.exists()
    if version == 5:
        assert queue.claim_native(queue.marker_hash(repo), provider=rows[0]["provider"]) is not None


@pytest.mark.parametrize("command", ["import-status", "automatic-ready"])
def test_actual_extracted_cli_reads_schema5_without_repair_or_sidecars(queue, command):
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        _upgrade(queue, db)
    before = {p.name: p.read_bytes() for p in queue.marker_dir().iterdir() if p.is_file()}
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), command],
                            capture_output=True, timeout=20, env=dict(os.environ))
    assert result.returncode == (10 if command == "automatic-ready" else 0), result.stderr
    observed = json.loads(result.stdout)
    assert observed["schema_version"] == 5
    if command == "automatic-ready":
        assert observed["automatic_protocol_ready"] == 0
    else:
        assert observed["state"] == "available"
    assert {p.name: p.read_bytes() for p in queue.marker_dir().iterdir() if p.is_file()} == before


def test_busy_future_header_does_not_gain_supported_outcome_publication(queue, tmp_path, monkeypatch):
    repo = str(tmp_path / "future")
    queue.write_marker(repo, "cbm", "fast")
    claimed = queue.claim(queue.marker_hash(repo), provider="cbm")
    with closing(queue._connect()) as db:
        db.execute("PRAGMA user_version=6")
    before = {p.name: p.read_bytes() for p in queue.marker_dir().iterdir() if p.is_file()}
    monkeypatch.setitem(queue.remember_outcome.__globals__, "LOCK_WAIT_S", 0.1)
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises((TimeoutError, sqlite3.OperationalError)):
                queue.remember_outcome(claimed["context"], "consume")
        finally:
            holder.rollback()
    assert {p.name: p.read_bytes() for p in queue.marker_dir().iterdir() if p.is_file()} == before


@pytest.mark.parametrize("missing", ["control_closure", "accepted_control_sha256"])
def test_partial_schema5_refuses_without_fallback_or_repair(queue, missing):
    queue._prepare()
    with closing(queue._connect()) as db:
        with queue._transaction(db):
            _upgrade(queue, db)
        table = "executions" if missing == "control_closure" else "provider_state"
        db.execute(f"ALTER TABLE {table} DROP COLUMN {missing}")
    old = queue.database_path().read_bytes()
    with pytest.raises(RuntimeError, match="incomplete"):
        queue._connect()
    assert queue.database_path().read_bytes() == old
    assert not list(queue.marker_dir().glob("*.spool"))


@pytest.mark.parametrize("operation", ["claim", "reserve", "register", "begin"])
def test_new_native_authority_refuses_incomplete_schema4(queue, tmp_path, operation):
    from tests.test_scripts.test_index_marker_execution import _intent, _registration

    claimed = _claim(queue, tmp_path)
    context = claimed["context"]
    if operation in ("register", "begin"):
        _reserve(queue, claimed, _reservation(claimed))
    if operation == "begin":
        with closing(queue._connect()) as db, queue._transaction(db):
            queue._native.register(db, context, _registration(claimed), queue._legs, queue._proof)
    actions = {"claim": lambda: queue.claim_native(context["hash"], provider="gitnexus"),
               "reserve": lambda: queue.reserve_launch(context, _reservation(claimed)),
               "register": lambda: queue.register_scope(context, _registration(claimed)),
               "begin": lambda: queue.begin_native(context, _intent(queue, claimed))}
    with pytest.raises(RuntimeError, match="schema5"):
        actions[operation]()
    with closing(queue._connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert not list(queue.marker_dir().glob("*.spool"))


@pytest.mark.parametrize("populated", [False, True])
def test_every_schema5_statement_failure_rolls_back_original_logical_database(queue, tmp_path, populated):
    _claim(queue, tmp_path)
    with closing(queue._connect()) as db:
        if not populated:
            db.execute("DELETE FROM inflight")
            db.execute("DELETE FROM executions")
        db.execute("UPDATE sqlite_sequence SET seq=4096 WHERE name='executions'")
        original = list(db.iterdump())

        class Boundary:
            def __init__(self, fail):
                self.fail, self.count = fail, 0

            def __getattr__(self, name):
                return getattr(db, name)

            def execute(self, sql, *args):
                self.count += 1
                if self.count == self.fail:
                    raise OSError("injected schema5 statement boundary")
                return db.execute(sql, *args)

        failed = 1
        while True:
            observed = Boundary(failed)
            try:
                with queue._transaction(observed):
                    _upgrade(queue, observed)
            except OSError:
                assert list(db.iterdump()) == original
                assert db.execute("PRAGMA user_version").fetchone()[0] == 4
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                failed += 1
                continue
            assert observed.count < failed and failed > 20
            assert db.execute("PRAGMA user_version").fetchone()[0] == 5
            assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='executions'").fetchone()[0] == 4096
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            break


@pytest.mark.parametrize("after_commit", [False, True])
def test_schema5_commit_failure_leaves_only_old_or_complete_state(queue, tmp_path, after_commit):
    _claim(queue, tmp_path)
    with closing(queue._connect()) as db:
        original = list(db.iterdump())

        class CommitFailure:
            def __getattr__(self, name):
                return getattr(db, name)

            def commit(self):
                if after_commit:
                    db.commit()
                raise OSError("injected schema5 commit acknowledgement")

        observed = CommitFailure()
        with pytest.raises(OSError, match="acknowledgement"), queue._transaction(observed):
            _upgrade(queue, observed)
        assert db.execute("PRAGMA user_version").fetchone()[0] == (5 if after_commit else 4)
        if after_commit:
            queue._recovery.validate_schema(db, queue._native)
        else:
            assert list(db.iterdump()) == original
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
