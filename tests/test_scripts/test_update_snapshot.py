"""Exercise the updater's snapshot preflight on isolated SQLite databases."""

from __future__ import annotations

import os
import re
import shlex
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UPDATE = ROOT / "scripts/update.sh"
AUTHORITY = ROOT / "src/genesis/db/crud/peer_restore.py"


def snapshot_block() -> str:
    text = UPDATE.read_text()
    match = re.search(r"# BEGIN db-snapshot[^\n]*\n(.*?)# END db-snapshot", text, re.S)
    assert match
    return match.group(1)


def seed(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE data(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO data VALUES(1, 'original')")


def run_snapshot(
    path: Path, *, authority: str | None = None, seconds: str = "10", harness_timeout: int = 4,
):
    script = (
        "set -euo pipefail\n"
        + snapshot_block()
        + f"\nDB_FILE={shlex.quote(str(path))}\n"
        + "DB_SNAPSHOT_TAKEN=0\n"
        + f"_PEER_RESTORE_PY={shlex.quote(authority if authority is not None else AUTHORITY.read_text())}\n"
        + 'if _take_db_snapshot; then echo "ADMITTED=$DB_SNAPSHOT_TAKEN"; '
        + 'else echo "REFUSED=$DB_SNAPSHOT_TAKEN"; exit 1; fi\n'
    )
    env = dict(os.environ, GENESIS_UPDATE_SNAPSHOT_TIMEOUT_S=seconds)
    proc = subprocess.Popen(
        ["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=harness_timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail("snapshot preflight exceeded its configured deadline")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def test_deadline_covers_authority_preparation(tmp_path):
    path = tmp_path / "live.db"
    seed(path)
    result = run_snapshot(path, authority="import time; time.sleep(30)", seconds="1")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "REFUSED=0" in result.stdout
    assert "ADMITTED=1" not in result.stdout
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM data").fetchone() == ("original",)


def worker():
    """Load exactly the isolated Python body shipped inside the updater."""
    match = re.search(r"<<'SNAPSHOT_PY'\n(.*?)\nSNAPSHOT_PY", UPDATE.read_text(), re.S)
    assert match, "missing updater Python worker"
    namespace = {"__name__": "snapshot_test"}
    exec(compile(match.group(1), str(UPDATE), "exec"), namespace)  # noqa: S102 - shipped worker
    return namespace


def test_pinned_snapshot_converges_despite_a_commit_between_every_step(tmp_path, monkeypatch):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE data(id INTEGER PRIMARY KEY, value BLOB)")
        writer.executemany("INSERT INTO data(value) VALUES(?)", [(b"x" * 4096,)] * 128)
        writer.execute("CREATE TABLE generation(value INTEGER)")
        writer.execute("INSERT INTO generation VALUES(0)")
        writer.commit()
        remaining_pages = []

        class InterleavedConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                def advance(status, remaining, total):
                    remaining_pages.append(remaining)
                    if len(remaining_pages) > 100:
                        raise RuntimeError("backup restarted instead of converging")
                    if remaining:
                        writer.execute("UPDATE generation SET value=value+1")
                        writer.commit()

                # Smaller steps force many interleavings on a small fixture.
                super().backup(target, pages=8, progress=advance)

        connect = sqlite3.connect

        def instrumented_connect(*args, **kwargs):
            return connect(*args, **kwargs, factory=InterleavedConnection)

        functions = worker()
        monkeypatch.setattr(sqlite3, "connect", instrumented_connect)
        target = tmp_path / "snapshot.db"
        functions["copy_snapshot"](source, target)
        assert len(remaining_pages) > 2
        assert all(a > b for a, b in zip(remaining_pages, remaining_pages[1:], strict=False))
        assert remaining_pages[-1] == 0
        with connect(target) as snapshot:
            assert snapshot.execute("SELECT value FROM generation").fetchone() == (0,)
            assert snapshot.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert snapshot.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert writer.execute("SELECT value FROM generation").fetchone()[0] > 0
        # A truncating checkpoint succeeding proves the source read was released.
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["live.db", "quote ' and space ? # Ω.db"])
def test_quiescent_snapshot_is_self_contained_private_and_preserves_live_db(tmp_path, name):
    source = tmp_path / name
    seed(source)
    before = source.read_bytes()
    raw = Path(str(source) + ".pre-update")
    guarded = Path(str(raw) + ".peer-restore")
    for path in (raw, guarded):
        path.write_bytes(b"old snapshot")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(path) + suffix).write_bytes(b"stale sidecar")
    result = run_snapshot(source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ADMITTED=1" in result.stdout
    assert source.read_bytes() == before
    for path in (raw, guarded):
        assert path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT value FROM data").fetchone() == ("original",)
            assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert all(not Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    assert not list(tmp_path.glob(".update-snapshot-*"))


def test_missing_source_skips_without_current_run_authority(tmp_path):
    source = tmp_path / "missing.db"
    result = run_snapshot(source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ADMITTED=0" in result.stdout
    assert not source.exists()
    assert not Path(str(source) + ".pre-update").exists()


def test_corrupt_source_preserves_prior_snapshots(tmp_path):
    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not a database")
    raw = Path(str(source) + ".pre-update")
    guarded = Path(str(raw) + ".peer-restore")
    raw.write_bytes(b"prior raw")
    guarded.write_bytes(b"prior guarded")
    result = run_snapshot(source)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED=0" in result.stdout
    assert raw.read_bytes() == b"prior raw"
    assert guarded.read_bytes() == b"prior guarded"
    assert not list(tmp_path.glob(".update-snapshot-*"))


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf", "1s", "1.5", " 1"])
def test_invalid_deadline_refuses_before_copy(tmp_path, seconds):
    source = tmp_path / "live.db"
    seed(source)
    result = run_snapshot(source, seconds=seconds)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED=0" in result.stdout
    assert "positive seconds required" in result.stderr
    assert not Path(str(source) + ".pre-update").exists()


@pytest.mark.parametrize("authority", ["", "invalid python syntax", "pass", "raise SystemExit(0)", "def reset_peer_authority(path): raise ValueError()"])
def test_authority_failure_never_admits_or_replaces_guarded_snapshot(tmp_path, authority):
    source = tmp_path / "live.db"
    seed(source)
    guarded = Path(str(source) + ".pre-update.peer-restore")
    guarded.write_bytes(b"prior guarded")
    result = run_snapshot(source, authority=authority)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED=0" in result.stdout
    assert "RESTORE AUTHORITY CHECK FAILED" in result.stderr
    assert guarded.read_bytes() == b"prior guarded"


def test_timeout_kills_the_actual_term_resistant_worker(tmp_path):
    source = tmp_path / "live.db"
    seed(source)
    pid_file = tmp_path / "worker.pid"
    authority = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    result = run_snapshot(source, authority=authority, seconds="1", harness_timeout=10)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED=0" in result.stdout
    pid = int(pid_file.read_text())
    # timeout and its worker can be killed together; allow init to reap the
    # already-dead worker before treating PID existence as a surviving process.
    reap_deadline = time.monotonic() + 1
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < reap_deadline, "snapshot worker survived the kill grace"
        time.sleep(0.01)
