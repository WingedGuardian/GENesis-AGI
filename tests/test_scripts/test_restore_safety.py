"""Safety tests for ``scripts/restore.sh`` SQLite restore.

``restore.sh`` rehydrates the live SQLite DB — the highest-stakes path in DR.
Three safety properties are guarded here:

* **Quiesce the writer.** Stop ``genesis-server`` before swapping the DB so a
  live WAL connection can't corrupt the restore — and do NOT auto-restart it
  (the operator verifies the restore first).
* **Clear stale WAL/SHM.** ``rm`` must remove ``-wal``/``-shm`` sidecars; a
  leftover WAL would replay onto the restored DB and corrupt it.
* **Integrity-check the result.** Run ``PRAGMA integrity_check`` after ``.read``
  so a corrupt restore is loud, not silent.

Fully sandboxed: ``HOME`` and ``GENESIS_DIR`` point at a tmp dir, so the live
``~/genesis/data/genesis.db`` is never touched. Real ``sqlite3`` is the thing
under test; ``systemctl`` is stubbed (and records its calls).
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_RESTORE = Path(__file__).resolve().parents[2] / "scripts" / "restore.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_systemctl(bind: Path, calls: Path, *, active: bool = True, stop_rc: int = 0) -> None:
    """Configurable systemctl stub: logs every call; `is-active --quiet` exits 0
    iff ``active`` (the gateway uses the exit code, not output); `stop` exits
    ``stop_rc``."""
    active_rc = 0 if active else 3
    _make_stub(
        bind / "systemctl",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in\n'
        f"  *is-active*) exit {active_rc} ;;\n"
        f"  *stop*) exit {stop_rc} ;;\n"
        "esac\n"
        "exit 0\n",
    )


def _write_sqlite3_integrity_intercept(bind: Path) -> None:
    """sqlite3 wrapper that reports a CORRUPT integrity_check but passes
    everything else (incl. `.read`) through to the real sqlite3."""
    real = shutil.which("sqlite3")
    _make_stub(
        bind / "sqlite3",
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in\n'
        '  *integrity_check*) echo "*** in database main ***"; exit 0 ;;\n'
        "esac; done\n"
        f'exec {real} "$@"\n',
    )


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis"
    (gd / "data").mkdir(parents=True)
    (home / ".genesis").mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    calls = tmp_path / "systemctl_calls.log"
    _write_systemctl(bind, calls)  # default: server active, stop succeeds
    return {"home": home, "gd": gd, "bind": bind, "calls": calls, "tmp": tmp_path}


def _seed_live_db(gd: Path) -> Path:
    """A real SQLite DB plus deliberately-stray -wal/-shm sidecars."""
    db = gd / "data" / "genesis.db"
    subprocess.run(
        ["sqlite3", str(db), "CREATE TABLE t(x); INSERT INTO t VALUES(1);"],
        check=True,
        capture_output=True,
    )
    (gd / "data" / "genesis.db-wal").write_bytes(b"STALE-WAL-SHOULD-BE-REMOVED")
    (gd / "data" / "genesis.db-shm").write_bytes(b"STALE-SHM-SHOULD-BE-REMOVED")
    return db


def _seed_backup(tmp_path: Path) -> Path:
    """Backup dir with a plaintext SQL dump (no GPG)."""
    bkp = tmp_path / "backup"
    (bkp / "data").mkdir(parents=True)
    (bkp / "data" / "genesis.sql").write_text("CREATE TABLE t(x);\nINSERT INTO t VALUES(42);\n")
    return bkp


def _run_restore(sandbox):
    bkp = _seed_backup(sandbox["tmp"])
    env = dict(os.environ)
    env["HOME"] = str(sandbox["home"])
    env["GENESIS_DIR"] = str(sandbox["gd"])
    env["QDRANT_URL"] = "http://127.0.0.1:1"  # dead → Qdrant restore skips fast
    env["PATH"] = f"{sandbox['bind']}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(_RESTORE), "--from", str(bkp), "--force"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _calls(sandbox) -> str:
    return sandbox["calls"].read_text() if sandbox["calls"].exists() else ""


def test_restore_stops_server_and_does_not_restart(sandbox):
    _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    calls = _calls(sandbox)
    assert "stop genesis-server" in calls, f"server not stopped before restore:\n{calls}"
    assert "start genesis-server" not in calls and "restart genesis-server" not in calls, (
        f"server must be left stopped (no auto-restart):\n{calls}"
    )


def test_restore_clears_stale_wal_shm(sandbox):
    db = _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not (sandbox["gd"] / "data" / "genesis.db-wal").exists(), "stale -wal not removed"
    assert not (sandbox["gd"] / "data" / "genesis.db-shm").exists(), "stale -shm not removed"
    out = subprocess.run(["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True)
    assert out.stdout.strip() == "42", f"DB not restored from backup dump: {out.stdout!r}"


# NOTE: this test's name must NOT contain the marker word — restore.sh logs the
# (tmp) DB path, and a test name leaking into that path would false-match.
def test_restore_verifies_db_after_restore(sandbox):
    _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # New code emits this exact marker only on a passing PRAGMA integrity_check.
    assert "integrity_check ok" in proc.stdout.lower(), (
        f"integrity_check not run/logged after restore:\n{proc.stdout}"
    )
    status = json.loads((sandbox["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["sqlite_restored"] is True, status


def test_restore_proceeds_and_warns_when_stop_fails(sandbox):
    """If genesis-server can't be stopped, the restore proceeds with a warning —
    and must NOT claim 'left stopped' (the server never stopped)."""
    _write_systemctl(sandbox["bind"], sandbox["calls"], active=True, stop_rc=1)
    db = _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"  # warn → failure → exit 1
    assert "could not stop genesis-server" in proc.stdout
    assert "left stopped" not in proc.stdout, "misleading note after a failed stop"
    assert (
        subprocess.run(
            ["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True
        ).stdout.strip()
        == "42"
    )


def test_restore_skips_stop_when_server_inactive(sandbox):
    """Fresh-box / not-running case: no stop attempted, restore still succeeds."""
    _write_systemctl(sandbox["bind"], sandbox["calls"], active=False)
    db = _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "stop genesis-server" not in _calls(sandbox), "stopped a server that wasn't active"
    assert "left stopped" not in proc.stdout
    assert (
        subprocess.run(
            ["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True
        ).stdout.strip()
        == "42"
    )


def test_restore_warns_on_integrity_failure(sandbox):
    """A restored DB that fails PRAGMA integrity_check must warn loudly, record a
    failure, and exit non-zero — never silently accept a corrupt restore."""
    _write_sqlite3_integrity_intercept(sandbox["bind"])
    _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"
    assert "integrity_check failed" in proc.stdout.lower(), proc.stdout
    status = json.loads((sandbox["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["success"] is False
    assert status["failures"], "integrity failure not recorded in restore_status.json"


# ── Deploy-in-progress marker (watchdog must not revive the server mid-restore) ──


def _write_systemctl_marker_probe(bind: Path, calls: Path) -> None:
    """systemctl stub that, on `stop`, records whether the deploy marker file
    already exists — proving the marker is held BEFORE the server is stopped (the
    window the watchdog would otherwise revive the half-built DB in)."""
    _make_stub(
        bind / "systemctl",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in\n'
        "  *is-active*) exit 0 ;;\n"
        '  *stop*) [ -f "$HOME/.genesis/update_in_progress.pid" ] '
        f'&& echo MARKER_PRESENT_AT_STOP >> "{calls}"; exit 0 ;;\n'
        "esac\n"
        "exit 0\n",
    )


def test_restore_holds_deploy_marker_across_stop(sandbox):
    """While restore.sh holds genesis-server stopped, it must hold the
    ``update_in_progress`` marker env.update_in_progress() honors — in place
    BEFORE the stop and released by the EXIT trap — so the autonomy watchdog
    defers instead of reviving the server into a half-built DB."""
    _write_systemctl_marker_probe(sandbox["bind"], sandbox["calls"])
    _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    calls = _calls(sandbox)
    assert "MARKER_PRESENT_AT_STOP" in calls, f"deploy marker not held at stop time:\n{calls}"
    assert "holding deploy-in-progress marker" in proc.stdout.lower(), proc.stdout
    assert not (sandbox["home"] / ".genesis" / "update_in_progress.pid").exists(), (
        "deploy marker not released by the EXIT trap (would disable the watchdog)"
    )


def test_restore_does_not_clobber_a_live_foreign_deploy_marker(sandbox):
    """If a real update.sh/dashboard deploy already owns the marker, restore must
    NOT overwrite it (and must not remove another deploy's marker in its trap);
    the concurrency is surfaced as a warning (exit 1) but the restore proceeds."""
    marker = sandbox["home"] / ".genesis" / "update_in_progress.pid"
    sleeper = subprocess.Popen(["sleep", "30"])  # a live stand-in "other deploy"
    try:
        marker.write_text(str(sleeper.pid))
        _seed_live_db(sandbox["gd"])
        proc = _run_restore(sandbox)
        assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"  # warn → exit 1
        assert "not overwriting" in proc.stdout.lower(), proc.stdout
        assert marker.exists() and marker.read_text().strip() == str(sleeper.pid), (
            "a live foreign deploy marker was clobbered"
        )
        assert (
            subprocess.run(
                ["sqlite3", str(sandbox["gd"] / "data" / "genesis.db"), "SELECT x FROM t;"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            == "42"
        ), "restore did not proceed"
    finally:
        sleeper.terminate()
        sleeper.wait()
        if marker.exists():
            marker.unlink()


# ── Pre-restore safety copy must be WAL-correct (a valid undo artifact) ──


def _seed_wal_db(gd: Path) -> Path:
    """A clean WAL-mode SQLite DB (the live-writer shape restore quiesces)."""
    import sqlite3

    db = gd / "data" / "genesis.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES(1)")
        conn.commit()
    finally:
        conn.close()
    return db


def test_pre_restore_safety_copy_is_valid_and_taken_via_backup(sandbox):
    """The pre-restore undo copy must be a STRUCTURALLY VALID sqlite db holding
    the pre-restore state — taken via ``sqlite3 .backup`` (WAL-aware) after the
    writer is quiesced, not a torn main-file-only ``cp`` from under a live WAL."""
    _seed_wal_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    copies = [
        c
        for c in (sandbox["gd"] / "data").glob("genesis.db.pre-restore.*")
        if not c.name.endswith(("-wal", "-shm"))
    ]
    assert len(copies) == 1, f"expected exactly one pre-restore copy, got {copies}"
    integ = subprocess.run(
        ["sqlite3", str(copies[0]), "PRAGMA integrity_check;"], capture_output=True, text=True
    ).stdout.strip()
    assert integ == "ok", f"pre-restore copy is not a valid db: {integ!r}"
    val = subprocess.run(
        ["sqlite3", str(copies[0]), "SELECT x FROM t;"], capture_output=True, text=True
    ).stdout.strip()
    assert val == "1", f"pre-restore copy missing the pre-restore state: {val!r}"
    assert "wal-correct" in proc.stdout.lower(), proc.stdout
