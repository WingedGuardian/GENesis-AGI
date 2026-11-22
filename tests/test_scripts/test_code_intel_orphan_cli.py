"""Sealed captured CLI -> canonical scratch locks -> exact dead-owner SQL row."""

import fcntl
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing, nullcontext
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_execution import _reservation


def setup(tmp_path, monkeypatch, profile):
    root, source, snapshot = case(tmp_path, monkeypatch, profile)
    modules = snapshot.assemble()
    queue = modules["queue"]
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
    queue.write_marker(str(root), "both", "fast")
    claimed = queue.claim_native(queue.marker_hash(str(root)), provider="cbm")
    reserve_dead_owner(queue, modules["proof"], claimed)
    return root, source, snapshot, modules, claimed


def reserve_dead_owner(queue, proof, claimed):
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys;sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _, ticks, _ = proof._process_state(Path("/proc") / str(child.pid), child.pid)
        reserved = _reservation(claimed)
        reserved["owner"].update(pid=child.pid, start_ticks=ticks)
        assert queue.reserve_launch(claimed["context"], reserved) == "reserved"
    finally:
        child.communicate(b"x", timeout=20)
    assert child.returncode == 0


@pytest.mark.parametrize(
    "held_reason", ["live", "missing-checkout", "symlinked-checkout", "no-git-directory"]
)
def test_captured_sweep_reconciles_once_without_resending(tmp_path, monkeypatch, held_reason):
    _, _, snapshot, modules, claimed = setup(tmp_path, monkeypatch, "runner")
    queue = modules["queue"]
    live_repo = tmp_path / "live-owner-repo"
    (live_repo / ".git").mkdir(parents=True)
    queue.write_marker(str(live_repo), "gitnexus", "fast")
    live = queue.claim_native(queue.marker_hash(str(live_repo)), provider="gitnexus")
    if held_reason == "live":
        assert queue.reserve_launch(live["context"], _reservation(live)) == "reserved"
    else:
        reserve_dead_owner(queue, modules["proof"], live)
        moved = tmp_path / "preserved-checkout"
        if held_reason == "no-git-directory":
            (live_repo / ".git").rmdir()
        else:
            live_repo.rename(moved)
        if held_reason == "symlinked-checkout":
            live_repo.symlink_to(moved)  # the old path now resolves elsewhere
    live_before = queue.execution_status(live["context"])
    later_repo = tmp_path / "later-orphan-repo"
    (later_repo / ".git").mkdir(parents=True)
    queue.write_marker(str(later_repo), "gitnexus", "fast")
    later = queue.claim_native(queue.marker_hash(str(later_repo)), provider="gitnexus")
    reserve_dead_owner(queue, modules["proof"], later)
    lock = Path(os.environ["GENESIS_HOME"]) / "locks/code-intel-runner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "/bin/bash", "--noprofile", "--norc", "-p", "-c",
        'exec 8>"$1"; /usr/bin/flock -n 8 || exit 97; shift; exec "$@"',
        "--", str(lock), *snapshot.resume_command("queue", "reconcile-orphans"),
    ]
    for _ in range(2):
        with snapshot.sealed() as capsule:
            result = subprocess.run(command, stdin=capsule, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()
        status = queue.execution_status(claimed["context"])
        assert status["control_closure"]["data"]["state"] == "orphaned"
        assert status["launch_permission"] == "cancelled"
        assert status["typed_charged_attempts"] == 0
        assert status["safety_kind"] == "uncertainty"
        assert status["cleanup_proof"] is None and status["terminal"] is None
        assert queue.get_inflight(claimed["hash"]) is None
        assert queue.claim_native(claimed["hash"], provider="cbm") is None
        assert queue.execution_status(live["context"]) == live_before
        assert queue.get_inflight(live["hash"])["claim_id"] == live["claim_id"]
        assert queue.execution_status(later["context"])["control_closure"]["data"]["state"] == "orphaned"
    pending = queue.list_markers()
    assert sorted(row["provider"] for row in pending) == ["cbm", "gitnexus", "gitnexus"]
    assert all(row["attempts"] == 0 for row in pending)


@pytest.mark.parametrize("replacement", ["absent", "empty", "wal", "older"])
def test_owner_sweep_does_not_create_or_upgrade_after_advisory_read(tmp_path, monkeypatch, replacement):
    _, _, snapshot, modules, _ = setup(tmp_path, monkeypatch, "runner")
    queue = modules["queue"]
    supervisor = modules["supervisor"].Supervisor(snapshot, modules, lambda: None)
    # The subprocess tests exercise fd8/fd9. Here pytest may already own fd9;
    # isolate the race specifically between the advisory read and DB open.
    monkeypatch.setattr(modules["owner_admission"], "repository_lock", lambda *_: nullcontext())
    observe = supervisor._observe_schema5
    original = queue.database_path()
    inventories = []

    def replace_after_candidate(operation):
        answer = observe(operation)
        if answer is True:
            original.rename(tmp_path / "preserved-queue.sqlite3")
            if replacement == "empty":
                original.touch()
            if replacement in ("wal", "older"):
                with closing(sqlite3.connect(original)) as db:
                    db.execute("CREATE TABLE sentinel (value)")
                    if replacement == "wal":
                        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
                    db.execute("PRAGMA user_version=4")
                    db.commit()
            inventories.append({p.name: p.read_bytes() for p in original.parent.iterdir()})
        return answer

    monkeypatch.setattr(supervisor, "_observe_schema5", replace_after_candidate)
    with pytest.raises(RuntimeError, match="schema|journal"):
        supervisor.reconcile_orphans()
    assert len(inventories) == 1, "reached the write after a real positive advisory proof"
    assert {p.name: p.read_bytes() for p in original.parent.iterdir()} == inventories[0]


@pytest.mark.parametrize("boundary", ["runner", "publication", "sqlite"])
def test_captured_sweep_classifies_real_contention(tmp_path, monkeypatch, boundary):
    _, _, snapshot, modules, _ = setup(tmp_path, monkeypatch, "runner")
    queue = modules["queue"]
    database = queue.database_path()
    before = database.read_bytes()
    lock = Path(os.environ["GENESIS_HOME"]) / "locks/code-intel-runner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if boundary == "sqlite":
        holder = closing(sqlite3.connect(database))
    else:
        path = lock if boundary == "runner" else queue.marker_dir() / ".queue.lock"
        holder = path.open("a")
    prelude = 'exec 8>"$1"; shift; exec "$@"'
    command = [
        "/bin/bash", "--noprofile", "--norc", "-p", "-c", prelude, "--", str(lock),
        *snapshot.resume_command("queue", "reconcile-orphans"),
    ]
    with holder as owned, snapshot.sealed() as capsule:
        if boundary == "sqlite":
            owned.execute("BEGIN EXCLUSIVE")
        else:
            fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(command, stdin=capsule, capture_output=True, timeout=30)
        assert result.returncode == 75, result.stderr.decode()
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "route", ["captured", "raw", "entry-profile", "missing-global", "occupied-repo", "stale-source"]
)
@pytest.mark.parametrize("sweep", [False, True], ids=["explicit", "automatic"])
def test_explicit_orphan_reconciliation_is_captured_and_never_an_automatic_retry(
    tmp_path, monkeypatch, route, sweep
):
    root, _, snapshot, modules, claimed = setup(
        tmp_path, monkeypatch, "entry" if route == "entry-profile" else "runner"
    )
    queue = modules["queue"]
    before = queue.database_path().read_bytes()
    context = json.dumps(claimed["context"], ensure_ascii=True)
    arguments = ["reconcile-orphans"] if sweep else ["reconcile-native", "--context", context]
    command = snapshot.resume_command("queue", *arguments)
    if route == "stale-source":
        script = root / "scripts/lib/index_marker_cli.py"
        script.write_bytes(script.read_bytes() + b"\n# source drift\n")
    lock = Path(os.environ["GENESIS_HOME"]) / "locks/code-intel-runner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if route == "raw":
        result = subprocess.run(
            [sys.executable, "-I", str(root / "scripts/lib/index_marker.py"), *arguments],
            capture_output=True,
            timeout=30,
        )
    else:
        prelude = (
            "exec 8>&-; "
            if route == "missing-global"
            else ("exec 8>" + shlex.quote(str(lock)) + "; /usr/bin/flock -n 8 || exit 97; ")
        )
        if route == "occupied-repo":
            prelude += "exec 9>" + shlex.quote(str(tmp_path / "foreign-descriptor")) + "; "
        with snapshot.sealed() as capsule:
            result = subprocess.run(
                [
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-p",
                    "-c",
                    prelude + "exec " + shlex.join(command),
                ],
                stdin=capsule,
                capture_output=True,
                timeout=30,
            )
    if route == "captured":
        assert result.returncode == 0, result.stderr.decode()
        observed = queue.execution_status(claimed["context"]) if sweep else json.loads(result.stdout)
        assert observed["control_closure"]["data"]["state"] == "orphaned"
        assert (
            observed["completion_kind"] == "unstarted" and observed["typed_charged_attempts"] == 0
        )
        assert queue.get_inflight(claimed["hash"]) is None
        assert queue.claim_native(claimed["hash"], provider="cbm") is None
        assert {r["provider"] for r in queue.list_markers()} == {"cbm", "gitnexus"}
        assert all(r["attempts"] == 0 for r in queue.list_markers())
        readback = queue.execution_status(claimed["context"])
        assert readback.pop("evidence_pending") is False  # Read-only wrapper adds this advisory.
        if not sweep:
            assert readback == observed
    else:
        assert result.returncode != 0
        assert (
            b"captured supervised reconciliation" in result.stderr
            or b"canonical" in result.stderr
            or b"descriptor" in result.stderr
        )
        assert queue.database_path().read_bytes() == before
    assert not list(root.glob("*.scope"))


@pytest.mark.parametrize("version", [None, "empty-file", 0, 1, 2, 3, 4, 5, 6])
def test_orphan_cli_refusal_never_initializes_an_unsupported_schema(tmp_path, monkeypatch, version):
    root, _, snapshot = case(tmp_path, monkeypatch, "runner")
    modules = snapshot.assemble()
    queue = modules["queue"]
    queue.marker_dir().mkdir(parents=True)
    if version == "empty-file":
        queue.database_path().write_bytes(b"")
    elif version == 4:
        with closing(queue._connect()):
            pass  # A genuine fresh schema4 is still not native authority.
    elif version is not None:
        with closing(sqlite3.connect(queue.database_path())) as db:
            for statement in queue._SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO queue_meta VALUES('schema_version',?)", (str(version),))
            db.execute(f"PRAGMA user_version={version}")
            db.commit()
    before = queue.database_path().read_bytes() if version is not None else None
    # Fresh schema4 setup already creates its initialization lock. Refusal must
    # preserve that artifact as well as avoid creating one for the other cases.
    files_before = {
        p.name: (p.stat().st_dev, p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes())
        for p in queue.marker_dir().iterdir()
    }
    context = dict(
        hash=queue.marker_hash(str(root)),
        repo_path=str(root),
        provider="cbm",
        protocol=3,
        execution_seq=1,
        claim_id="b" * 32,
    )
    lock = Path(os.environ["GENESIS_HOME"]) / "locks/code-intel-runner.lock"
    lock.parent.mkdir()
    command = [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-p",
        "-c",
        'exec 8>"$1"; /usr/bin/flock -n 8 || exit 97; shift; exec "$@"',
        "--",
        str(lock),
        *snapshot.resume_command("queue", "reconcile-native", "--context", json.dumps(context)),
    ]
    with snapshot.sealed() as capsule:
        result = subprocess.run(command, stdin=capsule, capture_output=True, timeout=30)
    assert result.returncode != 0
    assert b"schema" in result.stderr or b"no such table" in result.stderr
    if before is None:
        assert not queue.database_path().exists()
    else:
        assert queue.database_path().read_bytes() == before
    assert {
        p.name: (p.stat().st_dev, p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes())
        for p in queue.marker_dir().iterdir()
    } == files_before
