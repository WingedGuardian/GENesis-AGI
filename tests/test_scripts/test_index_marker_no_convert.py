"""Ordinary queue operations never create-during-update or convert an older queue.

Only the explicit ``convert_queue`` (the strict cutover's path) upgrades. A
deferred enqueue spools the plain request body that schema-2 code writes, so a
rolled-back install still imports it.
"""

import fcntl
import json
import os
import sqlite3
import subprocess
from contextlib import closing, contextmanager

import pytest

from tests.test_scripts.test_index_marker_legs import SCRIPT
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _state(queue):
    return queue.marker_dir().parent


def _files(queue):
    directory = queue.marker_dir()
    if not directory.exists():
        return {}
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


def _schema2(queue, *, version=2, tables=True):
    queue.marker_dir().mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as db:
        if tables:
            db.executescript(queue._SCHEMA)
            db.execute(queue._legacy._RECEIPTS)
            db.execute("INSERT INTO queue_meta VALUES('schema_version',?)", (str(version),))
        db.execute(f"PRAGMA user_version={version}")


def _version(queue):
    # Never read the live queue: the shared fixture roots state at "owned-state".
    assert queue.database_path().parent.parent.name == "owned-state"
    with closing(sqlite3.connect(queue.database_path())) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def _cli(queue, *args):
    return subprocess.run(
        ["python3", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "GENESIS_HOME": str(_state(queue))},
    )


@pytest.mark.parametrize("version", [1, 2])
def test_enqueue_on_older_queue_spools_plain_body_without_converting(queue, tmp_path, version):
    _schema2(queue, version=version)
    database = queue.database_path().read_bytes()
    repo = str(tmp_path / "repo")
    spool = queue.write_marker(repo, "both", "fast")
    assert spool.name.startswith(f".spool-{queue.marker_hash(repo)}-")
    body = json.loads(spool.read_bytes())
    assert set(body) == {"repo_path", "tools", "mode", "requested_at", "attempts"}
    assert body["repo_path"] == queue.canonical_repo(repo) and body["tools"] == "both"
    assert queue.database_path().read_bytes() == database and _version(queue) == version
    assert not (queue.marker_dir() / ".queue-db-init.lock").exists()


def test_every_other_operation_refuses_an_older_queue_without_side_effects(queue, tmp_path):
    _schema2(queue)
    before = _files(queue)
    for call in (
        queue.list_markers,
        queue.list_failed,
        queue.repend_stale_inflight,
        lambda: queue.claim("0" * 16, provider="cbm"),
        lambda: queue.get_repo_state("0" * 16),
    ):
        with pytest.raises(queue.QueueConversionDeferred, match="only via strict cutover"):
            call()
    assert _files(queue) == before


def test_remembered_outcome_never_spools_a_newer_format_onto_an_older_queue(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    context = queue.claim(queue.marker_hash(repo), provider="cbm")["context"]
    queue.database_path().unlink()
    _schema2(queue)
    before = _files(queue)
    with pytest.raises(queue.QueueConversionDeferred):
        queue.remember_outcome(context, "consume")
    assert _files(queue) == before


def test_cli_refusal_is_one_line_exit_2_and_write_spools(queue, tmp_path):
    _schema2(queue)
    listed = _cli(queue, "list")
    assert listed.returncode == 2 and not listed.stdout
    assert listed.stderr.strip() == (
        "index_marker: index queue is schema 2; conversion happens only via strict cutover"
    )
    written = _cli(queue, "write", "--repo", str(tmp_path / "repo"))
    assert written.returncode == 0 and "/.spool-" in written.stdout
    assert _version(queue) == 2


def test_preversioned_queue_with_tables_is_older_but_empty_file_is_fresh(queue, tmp_path):
    _schema2(queue, version=0)
    with pytest.raises(queue.QueueConversionDeferred, match="schema 0"):
        queue.list_markers()
    assert _version(queue) == 0
    queue.database_path().unlink()
    queue.database_path().write_bytes(b"")
    assert queue.list_markers() == []
    assert _version(queue) == queue.SCHEMA_VERSION


def _hold_update_lock(queue, mode):
    locks = _state(queue) / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    holder = open(locks / "update.lock", "a")  # noqa: SIM115 — held for the test
    fcntl.flock(holder, mode | fcntl.LOCK_NB)
    return holder


@pytest.mark.parametrize("signal", ["state-file", "exclusive-lock", "unreadable-lock"])
def test_creation_waits_for_a_running_update(queue, tmp_path, signal):
    holder = None
    _state(queue).mkdir(parents=True)
    if signal == "state-file":
        (_state(queue) / "update_state.json").write_text("{}")
    elif signal == "exclusive-lock":
        holder = _hold_update_lock(queue, fcntl.LOCK_EX)
    else:
        (_state(queue) / "locks").mkdir()
        (_state(queue) / "locks/update.lock").write_text("")
        (_state(queue) / "locks/update.lock").chmod(0)
        if os.access(_state(queue) / "locks/update.lock", os.R_OK):
            pytest.skip("read-denial premise defeated (privileged run)")
    try:
        spool = queue.write_marker(str(tmp_path / "repo"), "both", "fast")
        assert spool.name.startswith(".spool-") and "version" not in json.loads(spool.read_bytes())
        assert not queue.database_path().exists()
        with pytest.raises(queue.QueueConversionDeferred, match="during an update"):
            queue.list_markers()
        assert not queue.database_path().exists()
    finally:
        if holder is not None:
            holder.close()


@pytest.mark.parametrize("signal", ["none", "missing-lock", "shared-lock"])
def test_creation_proceeds_when_no_update_runs(queue, tmp_path, signal):
    holder = None
    if signal == "missing-lock":
        _state(queue).mkdir(parents=True)
    elif signal == "shared-lock":
        holder = _hold_update_lock(queue, fcntl.LOCK_SH)
    try:
        assert queue.write_marker(str(tmp_path / "repo"), "both", "fast") == queue.database_path()
        assert _version(queue) == queue.SCHEMA_VERSION
    finally:
        if holder is not None:
            holder.close()


def test_update_starting_mid_connect_leaves_only_an_absent_reading_empty_file(
    queue, tmp_path, monkeypatch
):
    """Never unlink: another process may be initializing the same new file."""
    probes = iter([False, True])
    gate_globals = queue._conversion_gate.__globals__
    monkeypatch.setitem(gate_globals, "_update_in_progress", lambda: next(probes))
    queue.marker_dir().mkdir(parents=True)
    with pytest.raises(queue.QueueConversionDeferred, match="during an update"):
        queue._connect()
    assert queue.database_path().stat().st_size == 0
    observed = _cli(queue, "automatic-ready")
    assert observed.returncode == 10 and json.loads(observed.stdout)["state"] == "absent"
    monkeypatch.setitem(gate_globals, "_update_in_progress", lambda: False)
    assert queue.write_marker(str(tmp_path / "repo"), "cbm", "fast") == queue.database_path()
    assert _version(queue) == queue.SCHEMA_VERSION


def test_busy_before_the_schema_is_confirmed_spools_the_plain_body(queue, tmp_path):
    """A lock held on an older queue must not yield a spool older code quarantines."""
    _schema2(queue)
    holder = sqlite3.connect(queue.database_path(), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        spool = queue.write_marker(str(tmp_path / "repo"), "both", "fast")
    finally:
        holder.rollback()
        holder.close()
    body = json.loads(spool.read_bytes())
    assert set(body) == {"repo_path", "tools", "mode", "requested_at", "attempts"}
    assert _version(queue) == 2


def test_busy_after_the_schema_is_confirmed_keeps_the_current_envelope(
    queue, tmp_path, monkeypatch
):
    queue.write_marker(str(tmp_path / "first"), "cbm", "fast")

    @contextmanager
    def busy(**_):
        raise TimeoutError("index-request queue busy; request not changed")
        yield

    monkeypatch.setitem(queue.write_marker.__globals__, "_write_db", busy)
    spool = queue.write_marker(str(tmp_path / "second"), "cbm", "fast")
    assert json.loads(spool.read_bytes())["kind"] == "enqueue"


def test_older_queue_created_while_waiting_for_the_init_lock_is_not_converted(
    queue, tmp_path, monkeypatch
):
    """An older writer may create the queue between the first check and the lock."""
    queue.marker_dir().mkdir(parents=True)
    real_flock = fcntl.flock
    created = []

    def flock(fd, operation):
        if not created:
            created.append(True)
            _schema2(queue)
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(queue.QueueConversionDeferred, match="schema 2"):
        queue.list_markers()
    # Restore only flock: monkeypatch.undo() would also drop the fixture's
    # GENESIS_HOME and point the next read at the default (live) queue.
    monkeypatch.setattr(fcntl, "flock", real_flock)
    assert created and _version(queue) == 2


@pytest.mark.parametrize("version", [0, 1, 2])
def test_explicit_conversion_upgrades_and_imports_deferred_spools(queue, tmp_path, version):
    _schema2(queue, version=version)
    repo = str(tmp_path / "repo")
    spool = queue.write_marker(repo, "gitnexus", "full")
    queue.convert_queue()
    assert _version(queue) == queue.SCHEMA_VERSION and not spool.exists()
    rows = queue.list_markers()
    assert [(row["tools"], row["mode"]) for row in rows] == [("gitnexus", "full")]
    assert rows[0]["legacy_unqualified"] == 1
