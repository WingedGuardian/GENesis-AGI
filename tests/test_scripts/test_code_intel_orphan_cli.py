"""Sealed captured CLI -> canonical scratch locks -> exact dead-owner SQL row."""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing
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
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys;sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _, ticks, _ = modules["proof"]._process_state(Path("/proc") / str(child.pid), child.pid)
        reserved = _reservation(claimed)
        reserved["owner"].update(pid=child.pid, start_ticks=ticks)
        assert queue.reserve_launch(claimed["context"], reserved) == "reserved"
    finally:
        child.communicate(b"x", timeout=20)
    assert child.returncode == 0
    return root, source, snapshot, modules, claimed


@pytest.mark.parametrize(
    "route", ["captured", "raw", "entry-profile", "missing-global", "occupied-repo"]
)
def test_explicit_orphan_reconciliation_is_captured_and_never_an_automatic_retry(
    tmp_path, monkeypatch, route
):
    root, _, snapshot, modules, claimed = setup(
        tmp_path, monkeypatch, "entry" if route == "entry-profile" else "runner"
    )
    queue = modules["queue"]
    before = queue.database_path().read_bytes()
    context = json.dumps(claimed["context"], ensure_ascii=True)
    arguments = ["reconcile-native", "--context", context]
    command = snapshot.resume_command("queue", *arguments)
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
        observed = json.loads(result.stdout)
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
