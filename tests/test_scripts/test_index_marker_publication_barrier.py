"""Real source processes share one publication boundary before queue decisions."""

import fcntl
import json
import os
import select
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts.test_index_marker_legs import SCRIPT
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _line(process):
    ready, _, _ = select.select([process.stdout], [], [], 20)
    assert ready, "actual source child did not reach its instrumented publication boundary"
    value = process.stdout.readline().strip()
    assert value, process.stderr.read() if process.poll() is not None else "child still running"
    return value


def _driver(tmp_path):
    path = tmp_path / "actual_queue_driver.py"
    path.write_text(
        "import fcntl,json,runpy,sys\n"
        "module=runpy.run_path(sys.argv[1])\n"
        "actual=fcntl.flock\n"
        "def observed(fd,operation):\n"
        " if operation & fcntl.LOCK_EX: print('EXCLUSIVE_ATTEMPT',flush=True)\n"
        " elif operation & fcntl.LOCK_SH: print('SHARED_ATTEMPT',flush=True)\n"
        " return actual(fd,operation)\n"
        "fcntl.flock=observed\n"
        "command=sys.argv[2]\n"
        "if command=='claim':\n"
        " result=module['claim'](sys.argv[3],provider='cbm')\n"
        " print('RESULT:'+json.dumps(result),flush=True)\n"
        "else:\n"
        " result=module['write_marker'](sys.argv[3],'cbm','full')\n"
        " print('RESULT:'+str(result),flush=True)\n"
    )
    return path


def test_actual_claim_waits_for_existing_publication_lock_before_deciding(queue, tmp_path):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "cbm", "fast")
    lock = (queue.marker_dir() / ".queue.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_SH)
    process = subprocess.Popen(
        [sys.executable, "-I", str(_driver(tmp_path)), str(SCRIPT), "claim", queue.marker_hash(repo)],
        env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        first = _line(process)
        assert first == "EXCLUSIVE_ATTEMPT", "claim bypassed the common publication boundary: " + first
    finally:
        lock.close()
        output, error = process.communicate(timeout=20)
    assert process.returncode == 0, error
    rows = [line for line in output.splitlines() if line.startswith("RESULT:")]
    assert len(rows) == 1 and json.loads(rows[0][7:])["provider"] == "cbm"


def test_busy_enqueue_enters_shared_barrier_before_fsync_fallback(queue, tmp_path):
    queue._prepare()
    blocker = sqlite3.connect(queue.database_path(), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    process = subprocess.Popen(
        [sys.executable, "-I", str(_driver(tmp_path)), str(SCRIPT), "write", str(tmp_path / "repo")],
        env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        first = _line(process)
        assert first == "SHARED_ATTEMPT", "busy enqueue published outside the barrier: " + first
        output, error = process.communicate(timeout=20)
        assert process.returncode == 0, error
        path = Path(next(line[7:] for line in output.splitlines() if line.startswith("RESULT:")))
        assert path.suffix == ".spool" and json.loads(path.read_bytes())["request"]["mode"] == "full"
    finally:
        blocker.rollback()
        blocker.close()
        if process.poll() is None:
            process.communicate(timeout=20)


@pytest.mark.parametrize("version", [2, 3])
def test_deferred_plain_request_holds_shared_barrier_through_sync(
    queue, tmp_path, monkeypatch, version
):
    from tests.test_scripts.test_index_marker_no_convert import _older_queue

    _older_queue(queue, version)
    gate = queue.marker_dir() / ".queue.lock"
    # Instrument control: this distinct open can acquire the unlocked file.
    with gate.open("a") as control:
        fcntl.flock(control, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(control, fcntl.LOCK_UN)
    sync = queue._fsync_dir
    observed = []

    def checked_sync(directory):
        with gate.open("a") as contender, pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed.append(directory)
        sync(directory)

    monkeypatch.setitem(queue.write_marker.__globals__, "_fsync_dir", checked_sync)
    spool = queue.write_marker(str(tmp_path / "repo"), "both", "full")
    assert observed == [queue.marker_dir()]
    assert set(json.loads(spool.read_bytes())) == {
        "repo_path", "tools", "mode", "requested_at", "attempts"
    }
