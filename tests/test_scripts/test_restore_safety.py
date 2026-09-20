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
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_RESTORE = Path(__file__).resolve().parents[2] / "scripts" / "restore.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_systemctl(
    bind: Path,
    calls: Path,
    *,
    active: bool = True,
    stop_rc: int = 0,
    probe_marker_on_stop: bool = False,
) -> None:
    """Configurable systemctl stub: logs every call; `is-active --quiet` exits 0
    iff ``active`` (the gateway uses the exit code, not output); `stop` exits
    ``stop_rc``."""
    state_file = calls.with_suffix(".state")
    state_file.write_text("active" if active else "inactive")
    marker_probe = ""
    if probe_marker_on_stop:
        marker_probe = (
            f' [ -f "$HOME/.genesis/update_in_progress.pid" ] && '
            f'echo MARKER_PRESENT_AT_STOP >> "{calls}";'
        )
    _make_stub(
        bind / "systemctl",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in\n'
        f'  *is-active*) grep -qx active "{state_file}"; exit $? ;;\n'
        f"  *stop*){marker_probe} [ {stop_rc} -eq 0 ] && "
        f'echo inactive > "{state_file}"; exit {stop_rc} ;;\n'
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
    (bkp / "data").mkdir(parents=True, exist_ok=True)
    (bkp / "data" / "genesis.sql").write_text("CREATE TABLE t(x);\nINSERT INTO t VALUES(42);\n")
    return bkp


def _run_restore(sandbox, *extra_args: str, scan_mode: str = "none"):
    """Run restore.sh hermetically.

    ``scan_mode`` is passed explicitly so no test inherits the HOST's privilege
    environment. The live-handle guard borrows authority (uid 0, else
    ``sudo -n``); a suite that lets ``auto`` resolve means the guard's behaviour
    under test depends on whether the machine running it happens to have
    passwordless sudo. Measured: with a failing ``sudo`` on PATH, 11 of 19 tests
    failed and one passed *vacuously* (it refused before reaching the swap, so
    the bytes it asserted on were trivially unchanged). Tests that care about
    the guard pass ``plain``/``sudo`` and drive that branch directly; the rest
    use ``none``, which states that the guard is out of scope for them rather
    than leaving it to resolve by accident.
    """
    bkp = _seed_backup(sandbox["tmp"])
    env = dict(os.environ)
    env["HOME"] = str(sandbox["home"])
    env["GENESIS_DIR"] = str(sandbox["gd"])
    env["QDRANT_URL"] = "http://127.0.0.1:1"  # dead → Qdrant restore skips fast
    env["PATH"] = f"{sandbox['bind']}:{env['PATH']}"
    env["GENESIS_RESTORE_HOLDER_SCAN"] = scan_mode
    # An inherited off-site backend makes the suite attempt a real network pull
    # and hang past the timeout, so the code under test is never reached.
    env["GENESIS_BACKUP_TIER2_BACKEND"] = "none"
    return subprocess.run(
        ["bash", str(_RESTORE), "--from", str(bkp), "--force", *extra_args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _write_sudo_failing_then_ok(bind: Path, calls: Path, fail_times: int) -> None:
    """A `sudo` whose FIND fails the first ``fail_times`` calls, then succeeds.

    ``sudo`` is stubbed rather than ``find`` because sudo uses a secure PATH: a
    PATH stub for find is bypassed and the guard silently succeeds, which is how
    an earlier version of the incomplete-inspection test passed for entirely the
    wrong reason. The stub still answers a bare ``true`` probe (exit 0) even
    though the script no longer sends one — sudo authorization is
    command-specific, so the scan itself is now the probe; the handling is kept
    inert so this helper stays valid against older script revisions.
    """
    _make_stub(
        bind / "sudo",
        "#!/usr/bin/env bash\n"
        'args=("$@")\n'
        '[ "${args[0]:-}" = "-n" ] && args=("${args[@]:1}")\n'
        '[ "${args[0]:-}" = "true" ] && exit 0\n'
        f'n=$(cat "{calls}" 2>/dev/null || echo 0)\n'
        f'if [ "$n" -lt {fail_times} ]; then echo $((n+1)) > "{calls}"; exit 1; fi\n'
        # The success path forces rc 0. It deliberately does NOT exec the args:
        # the stub runs unprivileged, and an unprivileged find over /proc exits
        # non-zero on unreadable entries, so every attempt would fail by
        # construction and the proceed path could never be exercised.
        #
        # This models "a scan that returns clean", NOT "a privileged scan": it
        # says nothing about visibility, because an unprivileged scan on this box
        # cannot produce rc 0 with empty output, so the stub's success state is
        # not one the real unprivileged path reaches. The tests using this stub
        # bind the retry bound and the rc capture; they do not test visibility.
        '"${args[@]}" >/dev/null 2>&1 || true\n'
        "exit 0\n",
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


def _capture_replayable_wal(tmp_path: Path) -> bytes:
    """Bytes of a REAL, uncheckpointed WAL — one that actually replays.

    ``_seed_live_db`` writes literal ASCII into the sidecars, and ASCII is not a
    WAL, so it cannot replay: a test using it exercises file EXISTENCE only, never
    the replay that the clearing exists to prevent. This captures a genuine WAL
    by opening a WAL-mode database at a different path, committing, and reading
    ``-wal`` while the connection is still open.
    """
    src = tmp_path / "foreign.db"
    for suf in ("", "-wal", "-shm"):
        Path(str(src) + suf).unlink(missing_ok=True)
    conn = sqlite3.connect(str(src))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t(x) VALUES(?)", [(1,), (999,)])
    conn.commit()
    try:
        return Path(str(src) + "-wal").read_bytes()
    finally:
        conn.close()


def test_stale_sidecars_cleared_when_main_db_is_absent(sandbox):
    """REGRESSION 2026-09-19: sidecar clearing must NOT be conditional on the
    main database existing.

    A stale WAL surviving the swap REPLAYS onto the restored database, replacing
    its pages with the old database's — and does so SILENTLY, because the result
    is self-consistent: the post-install integrity check passes and the restore
    reports success. The base script cleared the sidecars unconditionally; a
    revision of this fix nested that clearing inside ``[ -f "$DB_FILE" ]``, which
    skips it whenever that test is false: the main file absent, a dangling
    symlink, a symlink to a directory, or a directory at that path. (``test -f``
    FOLLOWS symlinks — it is true for a symlink to a regular file — so the
    accurate statement is "not a regular file", which is what the script's own
    comment says.)

    Measured on that revision: rc=0, "Restore complete", and a live database
    holding the FOREIGN database's rows instead of the backup's.

    ``_seed_live_db`` always creates the main database, so no other test can
    express this input — the gap was structurally invisible to the suite.

    Two assertions, doing different jobs. The EXISTENCE assertion is the one that
    bites for the ordering bug: a surviving `-wal` is the failure. The CONTENT
    assertion is a second net for the outcome — file existence alone cannot
    distinguish "cleared" from "replayed" — and to be meaningful it needs a WAL
    that can actually replay, which is why the fixture captures a real one. (The
    suite's other sidecar fixture writes literal ASCII, which is not a WAL and
    cannot replay, so a test built on it can only exercise existence.)
    """
    db = _seed_live_db(sandbox["gd"])
    data = sandbox["gd"] / "data"
    real_wal = _capture_replayable_wal(sandbox["tmp"])

    # NEGATIVE CONTROL: prove THIS wal really replays, so the content assertion
    # below cannot be vacuous. The old ASCII fixture could not replay at all, so
    # a test built on it exercised file existence and nothing else.
    control = sandbox["tmp"] / "control.db"
    subprocess.run(
        ["sqlite3", str(control), "CREATE TABLE t(x); INSERT INTO t VALUES(42);"],
        check=True,
        capture_output=True,
    )
    Path(str(control) + "-wal").write_bytes(real_wal)
    ctl = subprocess.run(
        ["sqlite3", str(control), "SELECT x FROM t;"], capture_output=True, text=True
    )
    assert "999" in ctl.stdout, (
        f"the control WAL does not replay ({ctl.stdout!r}) — this test would be "
        "vacuous, so fix the fixture before trusting a pass below"
    )

    db.unlink()  # the precondition: no main database, sidecars present
    (data / "genesis.db-wal").write_bytes(real_wal)

    proc = _run_restore(sandbox)

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not (data / "genesis.db-wal").exists(), (
        "a stale -wal survived the swap with no main DB present — it replays onto "
        "the restored database"
    )
    assert not (data / "genesis.db-shm").exists(), "a stale -shm survived the swap"
    out = subprocess.run(["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True)
    assert out.stdout.strip() == "42", (
        f"the restored database is not the backup's content: {out.stdout!r} — a stale "
        "WAL has replayed over it"
    )


def test_database_only_restore_skips_every_non_database_payload(sandbox):
    db = _seed_live_db(sandbox["gd"])
    backup = _seed_backup(sandbox["tmp"])
    (backup / "memory").mkdir()
    (backup / "memory" / "must-not-restore.txt").write_text("unrelated")

    proc = _run_restore(sandbox, "--database-only")

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "--- Qdrant ---" not in proc.stdout
    assert not (sandbox["home"] / ".claude" / "projects").exists()
    restored = subprocess.run(
        ["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True
    ).stdout.strip()
    assert restored == "42"


def test_database_only_restore_keeps_newer_guard_when_not_quarantined(sandbox):
    db = _seed_live_db(sandbox["gd"])
    backup = _seed_backup(sandbox["tmp"])
    sql = backup / "data" / "genesis.sql"
    os.utime(sql, (1_000, 1_000))
    os.utime(db, (2_000, 2_000))
    env = dict(os.environ)
    env.update(
        HOME=str(sandbox["home"]),
        GENESIS_DIR=str(sandbox["gd"]),
        QDRANT_URL="http://127.0.0.1:1",
        PATH=f"{sandbox['bind']}:{os.environ['PATH']}",
    )

    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(backup), "--database-only"],
        env=env,
        capture_output=True,
        text=True,
        input="y\n",
    )

    assert proc.returncode != 0
    assert "destination is newer than backup" in proc.stdout
    value = subprocess.run(
        ["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert value == "1"


def test_database_only_restore_aborts_if_quarantine_state_cannot_be_checked(sandbox):
    db = _seed_live_db(sandbox["gd"])
    backup = _seed_backup(sandbox["tmp"])
    real_python = sys.executable
    _make_stub(
        sandbox["bind"] / "python3",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-" ]; then echo "integrity import failed" >&2; exit 7; fi\n'
        f'exec "{real_python}" "$@"\n',
    )
    env = dict(os.environ)
    env.update(
        HOME=str(sandbox["home"]),
        GENESIS_DIR=str(sandbox["gd"]),
        QDRANT_URL="http://127.0.0.1:1",
        PATH=f"{sandbox['bind']}:{os.environ['PATH']}",
    )

    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(backup), "--database-only"],
        env=env,
        capture_output=True,
        text=True,
        input="y\n",
    )

    assert proc.returncode == 1
    assert "could not determine live database quarantine state" in proc.stdout
    value = subprocess.run(
        ["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert value == "1"


# NOTE: this test's name must NOT contain the marker word — restore.sh logs the
# (tmp) DB path, and a test name leaking into that path would false-match.
def test_restore_verifies_db_after_restore(sandbox):
    _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # New code emits this exact marker only on a passing PRAGMA integrity_check.
    assert "passed integrity, foreign-key, and schema checks" in proc.stdout.lower(), (
        f"integrity_check not run/logged after restore:\n{proc.stdout}"
    )
    status = json.loads((sandbox["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["sqlite_restored"] is True, status


def test_restore_refuses_when_stop_fails(sandbox):
    """A live writer that cannot be stopped leaves the live DB untouched."""
    _write_systemctl(sandbox["bind"], sandbox["calls"], active=True, stop_rc=1)
    db = _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"  # warn → failure → exit 1
    assert "could not confirm genesis-server stopped" in proc.stdout
    assert "left stopped" not in proc.stdout, "misleading note after a failed stop"
    assert (
        subprocess.run(
            ["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True
        ).stdout.strip()
        == "1"
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
    db = _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"
    assert "integrity_check failed" in proc.stdout.lower(), proc.stdout
    status = json.loads((sandbox["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["success"] is False
    assert status["failures"], "integrity failure not recorded in restore_status.json"
    live = subprocess.run(
        ["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True
    ).stdout.strip()
    assert live == "1", "failed staged validation modified the live DB"


def test_failed_final_check_quarantines_installed_inode(sandbox):
    """A post-swap failure must leave a fence bound to the replacement inode."""
    real_python = sys.executable
    _make_stub(
        sandbox["bind"] / "python3",
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"-m genesis.db.integrity check"*"--source restore-complete"*)\n'
        '    printf "not a sqlite database" > "$GENESIS_DIR/data/genesis.db" ;;\n'
        "esac\n"
        f'exec "{real_python}" "$@"\n',
    )
    _seed_live_db(sandbox["gd"])

    proc = _run_restore(sandbox)

    assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"
    assert "installed database failed final verification" in proc.stdout
    marker = json.loads((sandbox["home"] / ".genesis" / "db_quarantine.json").read_text())
    installed = (sandbox["gd"] / "data" / "genesis.db").stat()
    assert marker["st_dev"] == installed.st_dev
    assert marker["st_ino"] == installed.st_ino


def test_indeterminate_final_check_fences_installed_inode(sandbox):
    """Operational uncertainty fences restart without claiming corruption."""
    real_python = sys.executable
    _make_stub(
        sandbox["bind"] / "python3",
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"-m genesis.db.integrity check"*"--source restore-complete"*)\n'
        '    echo "quick_check could not complete (SQLITE_BUSY): database is locked" >&2\n'
        "    exit 1 ;;\n"
        "esac\n"
        f'exec "{real_python}" "$@"\n',
    )
    _seed_live_db(sandbox["gd"])

    proc = _run_restore(sandbox)

    assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"
    marker = json.loads((sandbox["home"] / ".genesis" / "db_quarantine.json").read_text())
    installed_path = sandbox["gd"] / "data" / "genesis.db"
    installed = installed_path.stat()
    assert marker["source"] == "restore-final-verification-incomplete"
    assert "SQLITE_BUSY" in marker["detail"]
    assert marker["st_dev"] == installed.st_dev
    assert marker["st_ino"] == installed.st_ino
    value = subprocess.run(
        ["sqlite3", str(installed_path), "SELECT x FROM t;"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert value == "42"


# ── Deploy-in-progress marker (watchdog must not revive the server mid-restore) ──


def test_restore_holds_deploy_marker_across_stop(sandbox):
    """While restore.sh holds genesis-server stopped, it must hold the
    ``update_in_progress`` marker env.update_in_progress() honors — in place
    BEFORE the stop and released by the EXIT trap — so the autonomy watchdog
    defers instead of reviving the server into a half-built DB."""
    _write_systemctl(sandbox["bind"], sandbox["calls"], probe_marker_on_stop=True)
    _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    calls = _calls(sandbox)
    assert "MARKER_PRESENT_AT_STOP" in calls, f"deploy marker not held at stop time:\n{calls}"
    assert "holding deploy-in-progress marker" in proc.stdout.lower(), proc.stdout
    assert not (sandbox["home"] / ".genesis" / "update_in_progress.pid").exists(), (
        "deploy marker not released by the EXIT trap (would disable the watchdog)"
    )


def _write_sqlite3_marker_probe(bind: Path, calls: Path) -> None:
    """sqlite3 wrapper that, on the dump `.read`, records whether the deploy
    marker exists AT THAT MOMENT — proving the marker is held during the
    multi-minute DB rebuild — then execs the real sqlite3."""
    real = shutil.which("sqlite3")
    _make_stub(
        bind / "sqlite3",
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in\n'
        '  .read*) if [ -f "$HOME/.genesis/update_in_progress.pid" ]; then\n'
        f'            echo MARKER_PRESENT_DURING_READ >> "{calls}"\n'
        "          else\n"
        f'            echo MARKER_ABSENT_DURING_READ >> "{calls}"\n'
        "          fi ;;\n"
        "esac; done\n"
        f'exec {real} "$@"\n',
    )


def test_restore_holds_marker_when_server_already_inactive(sandbox):
    """The watchdog revives an INACTIVE unit — so the marker must be held during
    the DB rebuild even when genesis-server is already stopped at restore start
    (operator pre-stop, or a crash): the highest-risk, longest window."""
    _write_systemctl(sandbox["bind"], sandbox["calls"], active=False)
    _write_sqlite3_marker_probe(sandbox["bind"], sandbox["calls"])
    _seed_live_db(sandbox["gd"])
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    calls = _calls(sandbox)
    assert "stop genesis-server" not in calls, "stopped a server that wasn't active"
    # Candidate construction is intentionally online and isolated. The marker
    # is acquired only for the short validated swap window.
    assert "MARKER_ABSENT_DURING_READ" in calls, calls
    assert "holding deploy-in-progress marker" in proc.stdout.lower(), proc.stdout
    assert not (sandbox["home"] / ".genesis" / "update_in_progress.pid").exists(), (
        "deploy marker not released by the EXIT trap"
    )


def test_restore_does_not_clobber_a_live_foreign_deploy_marker(sandbox):
    """If a real update.sh/dashboard deploy already owns the marker, restore must
    NOT overwrite it (and must not remove another deploy's marker in its trap);
    the concurrency is fatal and the live DB remains untouched."""
    marker = sandbox["home"] / ".genesis" / "update_in_progress.pid"
    sleeper = subprocess.Popen(["sleep", "30"])  # a live stand-in "other deploy"
    try:
        marker.write_text(str(sleeper.pid))
        _seed_live_db(sandbox["gd"])
        proc = _run_restore(sandbox)
        assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"  # warn → exit 1
        assert "refusing concurrent" in proc.stdout.lower(), proc.stdout
        assert marker.exists() and marker.read_text().strip() == str(sleeper.pid), (
            "a live foreign deploy marker was clobbered"
        )
        assert (
            subprocess.run(
                ["sqlite3", str(sandbox["gd"] / "data" / "genesis.db"), "SELECT x FROM t;"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            == "1"
        ), "restore modified the DB despite a foreign deploy"
    finally:
        sleeper.terminate()
        sleeper.wait()
        if marker.exists():
            marker.unlink()


# ── Pre-restore safety copy must be WAL-correct (a valid undo artifact) ──


def _seed_wal_db(gd: Path) -> Path:
    """A clean WAL-mode SQLite DB (the live-writer shape restore quiesces)."""

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


def test_pre_restore_safety_copy_preserves_raw_database(sandbox):
    """The pre-restore DB is retained byte-for-byte for rollback/forensics."""
    live_db = _seed_wal_db(sandbox["gd"])
    before = live_db.read_bytes()
    proc = _run_restore(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    copies = [
        c
        for c in (sandbox["gd"] / "data").glob("genesis.db.pre-restore.*")
        if not c.name.endswith(("-wal", "-shm"))
    ]
    assert len(copies) == 1, f"expected exactly one pre-restore copy, got {copies}"
    assert copies[0].read_bytes() == before
    integ = subprocess.run(
        ["sqlite3", str(copies[0]), "PRAGMA integrity_check;"], capture_output=True, text=True
    ).stdout.strip()
    assert integ == "ok", f"pre-restore copy is not a valid db: {integ!r}"
    val = subprocess.run(
        ["sqlite3", str(copies[0]), "SELECT x FROM t;"], capture_output=True, text=True
    ).stdout.strip()
    assert val == "1", f"pre-restore copy missing the pre-restore state: {val!r}"
    assert "preserved raw pre-restore" in proc.stdout.lower(), proc.stdout


def test_the_audit_store_is_restored_after_secrets_so_its_path_can_be_configured():
    """A custom audit store is configured in secrets.env, which this script restores.

    The destination is resolved by asking the writer's own resolver, which reads the
    ENVIRONMENT — and `restore.sh` never loads `secrets.env`, so on the disaster this
    backup exists for (secrets.env gone) the resolver saw nothing and fell back to the
    default directory. The records landed there while the restored config sent every
    writer to the custom one, leaving the recovered audit trail orphaned in a
    directory nothing reads (Codex P2, PR #1609).

    The fix is ORDER, not a lookup: the section now runs after "Secrets", because
    before that point there is no config to consult. Both halves are pinned here —
    the ordering, and that the config is actually loaded before the resolve.

    A STRUCTURAL check, and said plainly: driving it end-to-end needs a full backup
    payload plus gpg for one branch. What it pins is the invariant that broke.
    """
    body = _RESTORE.read_text()
    secrets_at = body.index('log "--- Secrets ---"')
    resolve_at = body.index('_AUDIT_DST="$(python3')
    assert resolve_at > secrets_at, (
        "the audit-store destination is resolved BEFORE secrets are restored, so a "
        "configured store cannot be seen and records go to the default directory"
    )
    # ...and the config is actually loaded, not merely available on disk.
    window = body[secrets_at:resolve_at]
    assert "load_secrets_file" in window, (
        "secrets are restored but never loaded, so the resolver still reads an "
        "environment that does not carry GENESIS_MERGE_OVERRIDE_DIR"
    )


# ── Live-handle guard, and swap-failure recovery ─────────────────────
#
# Both regression tests below DRIVE the real script in the sandbox rather than
# re-implementing its logic: an inline copy of the code under test grades itself,
# and a hand-written intermediate hides exactly the seam where these defects live.


def _wait_for_fd_holder(pid: int, db: Path, *, timeout: float = 10.0) -> None:
    """Guard-the-guard: assert the fixture really holds an fd on *db*.

    Without this the test could pass — or fail — for a reason other than the
    guard, e.g. the holder dying before the scan or the fd not being established.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for entry in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    if entry.resolve() == db.resolve():
                        return
                except OSError:
                    continue
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"fixture never established an fd on {db} (pid {pid})")


def test_open_handle_blocks_the_swap(sandbox):
    """REGRESSION 2026-09-19: the live-handle guard could not fire at all.

    ``find ... | grep -q .`` under ``set -o pipefail`` yields FIND's exit status,
    so any unreadable ``/proc`` entry (permission-denied is routine) discards
    grep's success and the ``if`` is false. Measured with a guaranteed match —
    ``-lname /proc/self/exe`` — the guard still went silent, i.e. it was a no-op
    on any real box, and the restore would have swapped the database out from
    under a live writer. That is the precondition of the 2026-09-18 corruption.
    """
    db = _seed_live_db(sandbox["gd"])
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 3<"$1"; sleep 60', "_", str(db)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_fd_holder(holder.pid, db)

        # `plain` is sufficient and deliberate: the holder is this uid's own
        # child, so an unprivileged scan sees it — which is exactly the claim
        # being tested. Driving the mode explicitly keeps the assertion
        # independent of the host's sudoers.
        proc = _run_restore(sandbox, scan_mode="plain")

        combined = proc.stdout + proc.stderr
        assert "open process handles" in combined, (
            "restore proceeded with a live handle on the database — the guard "
            f"did not fire (rc={proc.returncode})\n{combined}"
        )
        assert proc.returncode != 0, "a refused restore must not exit 0"
    finally:
        holder.kill()
        holder.wait()


def test_failed_swap_leaves_the_original_trio_intact(sandbox):
    """REGRESSION 2026-09-19: sidecars were removed BEFORE the rename.

    ``restore.sh`` deleted ``-wal``/``-shm`` and only then moved the staged
    candidate into place, so an ``mv`` that failed left the original main file
    present but both sidecars gone — the live database de-fanged, with the
    pre-restore copies on disk but nothing that restored from them. The die
    message claimed the live DB "remains available" without mentioning them.
    """
    db = _seed_live_db(sandbox["gd"])
    data = sandbox["gd"] / "data"
    wal, shm = data / "genesis.db-wal", data / "genesis.db-shm"
    before = {"db": db.read_bytes(), "wal": wal.read_bytes(), "shm": shm.read_bytes()}

    # Fail `mv` only when the live database is a destination, so the rest of the
    # script still behaves normally and we isolate the swap.
    _make_stub(
        sandbox["bind"] / "mv",
        "#!/usr/bin/env bash\n"
        f'for a in "$@"; do [ "$a" = "{db}" ] && exit 1; done\n'
        'exec /bin/mv "$@"\n',
    )

    proc = _run_restore(sandbox)

    assert proc.returncode != 0, "an injected swap failure must make the restore die"
    assert db.read_bytes() == before["db"], "live database changed despite the failed swap"
    assert wal.read_bytes() == before["wal"], (
        "WAL sidecar was removed and not restored — the live trio is broken"
    )
    assert shm.read_bytes() == before["shm"], (
        "SHM sidecar was removed and not restored — the live trio is broken"
    )


def test_elevated_scan_finds_a_holder_through_the_privileged_shell(sandbox):
    """The elevated scan must glob INSIDE the privileged command.

    ``sudo find /proc/[0-9]*/fd ...`` expands the bracket in the CALLER, before
    sudo starts. Under procfs ``hidepid=2`` the hidden PID directories are then
    absent from find's operands, find succeeds over the visible subset, and the
    guard passes while an unseen holder exists — a fail-open in the exact
    direction this guard exists to prevent.

    ``sudo`` is a pass-through stub here, so ``sudo -n sh -c 'find ...'`` really
    executes and the assertion is about the scan finding the holder, not about
    the spelling of the command.
    """
    db = _seed_live_db(sandbox["gd"])
    argv_log = sandbox["tmp"] / "sudo.argv"
    _make_stub(
        sandbox["bind"] / "sudo",
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{argv_log}"\n'
        'args=("$@")\n'
        '[ "${args[0]:-}" = "-n" ] && args=("${args[@]:1}")\n'
        '[ "${args[0]:-}" = "true" ] && exit 0\n'
        'exec "${args[@]}"\n',
    )
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 3<"$1"; sleep 60', "_", str(db)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_fd_holder(holder.pid, db)
        proc = _run_restore(sandbox, scan_mode="sudo")
        combined = proc.stdout + proc.stderr
        assert "open process handles" in combined, (
            "the elevated scan did not find a real holder — the privileged "
            f"command form is not inspecting /proc correctly\n{combined}"
        )
        assert proc.returncode != 0, "a refused restore must not exit 0"
    finally:
        holder.kill()
        holder.wait()

    # The functional assertion above is a SMOKE test, not a binding one: on a
    # host without procfs `hidepid` the unprivileged expansion sees every PID, so
    # the correct form and the caller-expanded one both find the holder and it
    # passes either way.
    #
    # Bind the PROPERTY, not the spelling. An earlier version of this test
    # asserted `"sudo -n find /proc/" not in src` — a denylist of one spelling,
    # which MEASURED green against `sudo -n /usr/bin/find /proc/[0-9]*/fd ...`,
    # the identical fail-open re-spelled. What matters is that no
    # ALREADY-EXPANDED PID path reaches the privileged layer: observe the argv
    # sudo actually receives, which binds on every host.
    argv = argv_log.read_text() if argv_log.exists() else ""
    expanded = re.findall(r"/proc/\d+/fd", argv)
    assert not expanded, (
        f"a PID-expanded /proc operand reached the privileged layer "
        f"({expanded[:3]}) — the glob expanded in the unprivileged caller, so "
        "hidepid-hidden holders would be omitted and the guard would fail open"
    )


def test_dangling_sidecar_symlinks_are_cleared(sandbox):
    """REGRESSION 2026-09-19: `-f` is FALSE for a dangling symlink.

    The base cleared the sidecars with an unconditional ``rm -f``, which removes
    a dangling symlink. A ``[ -f ... ] || continue`` guard skips it instead,
    leaving the pathname in place. That is not inert: SQLite cannot create the
    sidecar through a dangling target, so the restored service fails to open for
    writes while the read-only final integrity check still passes.
    """
    _seed_live_db(sandbox["gd"])
    data = sandbox["gd"] / "data"
    for suf in ("-wal", "-shm"):
        p = data / f"genesis.db{suf}"
        p.unlink()
        p.symlink_to("/nonexistent/target")
    assert not (data / "genesis.db-wal").exists(), "fixture is not dangling"
    assert (data / "genesis.db-wal").is_symlink(), "fixture is not a symlink"

    proc = _run_restore(sandbox)

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{combined}"
    for suf in ("-wal", "-shm"):
        p = data / f"genesis.db{suf}"
        assert not p.is_symlink() and not p.exists(), (
            f"a dangling genesis.db{suf} symlink survived the swap — SQLite "
            f"cannot create the sidecar through a dangling target\n{combined}"
        )


def test_failed_second_move_back_restores_the_first_sidecar(sandbox):
    """REGRESSION 2026-09-19: a failure in the MOVE-ASIDE phase must roll back.

    The sidecars are moved aside in a loop (wal, then shm). If the second move
    fails, `die` exits — and the rollback written for the rename-failure path is
    never reached, because the rename has not been attempted yet. The live main
    database is then left present with its WAL already moved away: the de-fanged
    state this whole block exists to prevent, and the SAME CLASS as the original
    defect, reintroduced in a different phase.

    The assertion is on all three files' bytes, not on their existence: the point
    is that the live trio is exactly as it was.
    """
    db = _seed_live_db(sandbox["gd"])
    data = sandbox["gd"] / "data"
    wal, shm = data / "genesis.db-wal", data / "genesis.db-shm"
    before = {"db": db.read_bytes(), "wal": wal.read_bytes(), "shm": shm.read_bytes()}

    # Fail the SECOND move only — i.e. the shm, by source path. The wal has
    # already been moved aside by the time this fires, which is what makes the
    # rollback necessary.
    _make_stub(
        sandbox["bind"] / "mv",
        f'#!/usr/bin/env bash\n[ "$1" = "{shm}" ] && exit 1\nexec /bin/mv "$@"\n',
    )

    proc = _run_restore(sandbox)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"a failed move-aside must die\n{combined}"
    # Pin the FAILURE SITE, not only the outcome. Measured: injecting a `die`
    # before the move-aside loop leaves every other assertion here GREEN, because
    # nothing was touched and the trio is trivially intact — the same vacuity
    # this file's `_run_restore` docstring warns about.
    assert "could not move the live shm aside" in combined, (
        f"the run did not fail in the move-aside phase\n{combined}"
    )
    assert not list(data.glob("genesis.db.pre-restore.*-wal")), (
        "the WAL was left aside instead of moved back"
    )
    assert db.exists(), "the live main database was lost"
    assert wal.exists(), (
        f"the WAL was moved aside and NOT moved back — the live database is de-fanged\n{combined}"
    )
    assert shm.exists(), "the SHM was lost"
    assert db.read_bytes() == before["db"], "live database bytes changed"
    assert wal.read_bytes() == before["wal"], "WAL bytes changed"
    assert shm.read_bytes() == before["shm"], "SHM bytes changed"


def test_failed_swap_with_no_sidecars_reports_the_pre_restore_copy(sandbox):
    """REGRESSION 2026-09-19: the swap-failure message must branch on what was
    RECORDED, not on the negation of the other flag pair.

    ``!(MOVED_WAL || MOVED_SHM)`` does not imply there was no pre-restore copy. A
    copy taken while NO sidecars were present leaves both flags false, and that is
    the ordinary clean-shutdown shape — a cleanly closed WAL database keeps no
    ``-wal``/``-shm``, which is exactly what quiescing the server produces. The
    message then claimed "there was no pre-restore copy, so the live -wal/-shm
    were REMOVED" — all three claims false, on a path an ordinary run reaches.

    Harm is bounded to diagnosis (rc=1, quarantine retained, actions correct),
    which is why this guards a message and not a data outcome.
    """
    db = _seed_live_db(sandbox["gd"])
    data = sandbox["gd"] / "data"
    (data / "genesis.db-wal").unlink()  # the clean-shutdown shape
    (data / "genesis.db-shm").unlink()

    _make_stub(
        sandbox["bind"] / "mv",
        "#!/usr/bin/env bash\n"
        f'for a in "$@"; do [ "$a" = "{db}" ] && exit 1; done\n'
        'exec /bin/mv "$@"\n',
    )

    proc = _run_restore(sandbox)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"an injected swap failure must die\n{combined}"
    assert "no pre-restore copy" not in combined, (
        f"claimed there was no pre-restore copy when one was taken\n{combined}"
    )
    assert "were REMOVED" not in combined, (
        f"claimed sidecars were removed when none existed\n{combined}"
    )
    assert "pre-restore copy at" in combined, (
        f"the message did not name the pre-restore copy it actually took\n{combined}"
    )


def test_incomplete_proc_inspection_refuses(sandbox):
    """REGRESSION 2026-09-19: an unreadable /proc must REFUSE, not warn.

    The guard's job is "no process holds this database". When it cannot inspect
    every process it has established nothing, so proceeding on a warning would
    substitute an unsupported completeness claim for the check — the same defect
    class as the pipeline bug it replaces. This drives the real script with a
    `find` that reports failure, which is what an unreadable /proc entry
    produces (routine: other-uid processes are unreadable from this uid).
    """
    _seed_live_db(sandbox["gd"])
    # Force the genuinely-unavailable-authority state: `sudo -n true` must fail
    # so the guard cannot borrow visibility into other-uid /proc entries.
    #
    # Do NOT stub `find` for this: sudo runs with a secure PATH, so a PATH stub
    # is bypassed and the guard silently succeeds — which is how an earlier
    # version of this test passed for entirely the wrong reason.
    _make_stub(sandbox["bind"] / "sudo", "#!/usr/bin/env bash\nexit 1\n")

    proc = _run_restore(sandbox, scan_mode="sudo")

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"an incomplete inspection must refuse\n{combined}"
    assert "inspect" in combined.lower(), (
        f"refusal did not say the inspection was incomplete\n{combined}"
    )
    # "Refused" must mean the swap did NOT happen. Without these two assertions a
    # one-word `die` -> `warn` edit keeps the suite GREEN: warn() appends to
    # _FAILURES so the run still exits non-zero and prints the same "inspect"
    # text, but the script then PERFORMS THE SWAP under an unknown holder — the
    # exact corruption class this guard exists to prevent. Measured: rc=1,
    # "restored and verified" present, live DB replaced by the backup's 42.
    assert "restored and verified" not in combined, (
        f"the swap ran despite an inconclusive scan\n{combined}"
    )
    live = subprocess.run(
        ["sqlite3", str(sandbox["gd"] / "data" / "genesis.db"), "SELECT x FROM t;"],
        capture_output=True,
        text=True,
    )
    assert live.stdout.strip() == "1", (
        f"the live database was replaced despite an inconclusive scan: {live.stdout!r}"
    )


def test_holder_scan_retries_then_proceeds(sandbox):
    """REGRESSION 2026-09-19: the bounded retry loop and the status capture.

    Measured before this test existed: setting ``_HOLDER_ATTEMPTS=1``, or
    deleting both ``|| _HOLDER_RC=$?`` captures, left the suite GREEN — so the
    headline of the (a) fix could be reverted without a single test noticing.
    A scan that fails transiently (a pid vanishes between glob expansion and
    traversal) must be retried until a clean run is obtained, and the restore
    must then proceed.
    """
    _seed_live_db(sandbox["gd"])
    counter = sandbox["tmp"] / "sudo-count"
    _write_sudo_failing_then_ok(sandbox["bind"], counter, fail_times=2)

    proc = _run_restore(sandbox, scan_mode="sudo")

    combined = proc.stdout + proc.stderr
    # Exactly `fail_times` failed attempts: the counter is written on each
    # failing call, so `exists()` alone would also be satisfied by a single
    # attempt that never retried — which is the mutation this test exists to
    # catch. Equality is what proves the retry actually happened.
    attempts = counter.read_text().strip() if counter.exists() else ""
    assert attempts == "2", (
        f"expected exactly 2 failed scan attempts before the clean one, got {attempts!r}"
    )
    assert proc.returncode == 0, (
        "a scan that succeeds on a later attempt must let the restore proceed; "
        f"the retry loop did not (rc={proc.returncode})\n{combined}"
    )


def test_holder_scan_exhausts_and_refuses(sandbox):
    """The other half of the pair: when EVERY attempt fails, refuse.

    Paired with the retry test so neither can pass by the guard always refusing
    or always proceeding — the failure mode that makes a suite green while the
    mechanism it names is inert.
    """
    _seed_live_db(sandbox["gd"])
    counter = sandbox["tmp"] / "sudo-count"
    _write_sudo_failing_then_ok(sandbox["bind"], counter, fail_times=99)

    proc = _run_restore(sandbox, scan_mode="sudo")

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"an exhausted scan must refuse\n{combined}"
    assert "inspect" in combined.lower(), (
        f"refusal did not say the inspection was incomplete\n{combined}"
    )
    # Same pair as the incomplete-inspection test: an exhausted scan must leave
    # the live database untouched, not merely exit non-zero.
    assert "restored and verified" not in combined, (
        f"the swap ran despite an exhausted scan\n{combined}"
    )
    live = subprocess.run(
        ["sqlite3", str(sandbox["gd"] / "data" / "genesis.db"), "SELECT x FROM t;"],
        capture_output=True,
        text=True,
    )
    assert live.stdout.strip() == "1", (
        f"the live database was replaced despite an exhausted scan: {live.stdout!r}"
    )


def test_command_specific_sudo_grant_is_accepted(sandbox):
    """Sudo authorization is COMMAND-specific — a `true` probe must not gate.

    A least-privilege sudoers rule granting exactly the privileged scan command
    fails a ``sudo -n true`` capability probe, so probing with ``true`` refused
    a restore that held precisely the authority it needed. The scan itself is
    the probe. The stub models that grant: ``true`` is NOT authorized (exit 1),
    everything else — i.e. the scan — succeeds with a clean empty result.
    """
    _seed_live_db(sandbox["gd"])
    argv_log = sandbox["tmp"] / "sudo.argv"
    _make_stub(
        sandbox["bind"] / "sudo",
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{argv_log}"\n'
        'args=("$@")\n'
        '[ "${args[0]:-}" = "-n" ] && args=("${args[@]:1}")\n'
        '[ "${args[0]:-}" = "true" ] && exit 1\n'
        "exit 0\n",
    )

    proc = _run_restore(sandbox, scan_mode="sudo")

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "a sudoers rule granting exactly the scan command was refused — a "
        f"`true` capability probe is still gating the scan\n{combined}"
    )
    # Bind the property, not just the outcome: no bare `true` probe may reach
    # sudo at all, or a least-privilege grant fails before the scan is tried.
    argv = argv_log.read_text() if argv_log.exists() else ""
    assert "\ntrue\n" not in f"\n{argv}", f"a `sudo -n true` capability probe reached sudo:\n{argv}"


def test_holder_match_ignores_pre_restore_copies(sandbox):
    """A holder of a .pre-restore.<ts> safety copy must NOT refuse the restore.

    The prefix pattern ``"$DB_FILE*"`` also matched this script's own
    ``genesis.db.pre-restore.<epoch>`` artifacts, so a forensic tool holding a
    safety copy open read as a live-database holder — refusing a restore that
    copy cannot affect. Matching is now exact (main/-wal/-shm, live and
    deleted forms).
    """
    _seed_live_db(sandbox["gd"])
    copy = sandbox["gd"] / "data" / "genesis.db.pre-restore.1700000000"
    copy.write_bytes(b"forensic safety copy")
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 3<"$1"; sleep 60', "_", str(copy)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_fd_holder(holder.pid, copy)
        proc = _run_restore(sandbox, scan_mode="plain")
        combined = proc.stdout + proc.stderr
        # The binding assertion is the REASON, not the verdict: on a host with
        # unreadable other-uid /proc entries the unprivileged scan may still
        # refuse as INCONCLUSIVE, but it must never claim this safety-copy
        # handle is a live-database holder.
        assert "open process handles" not in combined, (
            "a handle on a .pre-restore safety copy was treated as a live-"
            f"database holder — the match pattern is still a prefix\n{combined}"
        )
    finally:
        holder.kill()
        holder.wait()


def test_deleted_but_open_database_still_refuses(sandbox):
    """An unlinked-but-open database handle is still a holder.

    /proc fd links to an unlinked target read ``<path> (deleted)`` — the exact
    shape of the 2026-09-18 incident (a server holding deleted sidecars). The
    exact-match patterns must therefore include the `` (deleted)`` forms, or
    tightening the match would have traded a false refusal for a fail-open.
    """
    db = _seed_live_db(sandbox["gd"])
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 3<"$1"; sleep 60', "_", str(db)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_fd_holder(holder.pid, db)
        db.unlink()  # AFTER the fd is established: the link text gains " (deleted)"
        proc = _run_restore(sandbox, scan_mode="plain")
        combined = proc.stdout + proc.stderr
        assert "open process handles" in combined, (
            "an unlinked-but-open database handle was not treated as a holder — "
            f"the (deleted) form is not matched (rc={proc.returncode})\n{combined}"
        )
        assert proc.returncode != 0, "a refused restore must not exit 0"
    finally:
        holder.kill()
        holder.wait()


def test_symlinked_genesis_dir_still_sees_the_holder(sandbox):
    """A symlinked component in DB_FILE must not blind the scan.

    /proc fd links carry the RESOLVED target, while ``-lname`` matches the
    pattern text literally — so a DB_FILE reached through a symlink never
    matched, and the guard went blind exactly where paths are least canonical.
    The scan now resolves DB_FILE first.
    """
    db = _seed_live_db(sandbox["gd"])
    gd_link = sandbox["tmp"] / "gd-link"
    gd_link.symlink_to(sandbox["gd"])
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 3<"$1"; sleep 60', "_", str(db)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_fd_holder(holder.pid, db)
        linked = dict(sandbox)
        linked["gd"] = gd_link
        proc = _run_restore(linked, scan_mode="plain")
        combined = proc.stdout + proc.stderr
        assert "open process handles" in combined, (
            "a live holder was missed because GENESIS_DIR is a symlink — the "
            f"scan matched the unresolved path (rc={proc.returncode})\n{combined}"
        )
        assert proc.returncode != 0, "a refused restore must not exit 0"
    finally:
        holder.kill()
        holder.wait()


def _write_proc_mounts_redirect(bind: Path, fake_mounts: Path) -> None:
    """A `grep` that reads *fake_mounts* wherever the script names /proc/mounts.

    Delegates every other invocation to the real grep unchanged, so the many
    unrelated grep calls in restore.sh behave identically.
    """
    _make_stub(
        bind / "grep",
        "#!/usr/bin/env bash\n"
        "args=()\n"
        'for a in "$@"; do\n'
        f'    [ "$a" = "/proc/mounts" ] && a="{fake_mounts}"\n'
        '    args+=("$a")\n'
        "done\n"
        'exec /usr/bin/grep "${args[@]}"\n',
    )


def _write_find_proc_recorder(bind: Path, log: Path) -> None:
    """A `find` that records /proc scans (returning a clean empty result) and
    delegates every other invocation to the real find."""
    _make_stub(
        bind / "find",
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        f'/proc/*) printf "%s\\n" "$@" >> "{log}"; exit 0 ;;\n'
        "esac\n"
        'exec /usr/bin/find "$@"\n',
    )


def test_hidepid_any_enabled_value_stops_the_unprivileged_scan(sandbox):
    """`plain` must refuse under ANY enabled hidepid value, not just 1/2.

    ``hidepid=[12]`` missed ``hidepid=4`` and the symbolic spellings
    (``noaccess``, ``invisible``, ``ptraceable``): under those the unprivileged
    glob silently omits hidden PID directories and find exits 0 over the
    visible subset — a clean-looking pass with a holder it cannot see. Same
    closed-set-of-values mistake scripts/check_cc_running_versions.sh already
    corrected. The control run (hidepid=off) proves the scan DOES run when
    visibility is not restricted — so this cannot pass by the guard refusing
    everything.
    """
    _seed_live_db(sandbox["gd"])
    fake_mounts = sandbox["tmp"] / "mounts"
    find_log = sandbox["tmp"] / "find.argv"
    _write_proc_mounts_redirect(sandbox["bind"], fake_mounts)
    _write_find_proc_recorder(sandbox["bind"], find_log)

    fake_mounts.write_text("proc /proc proc rw,nosuid,nodev,noexec,relatime,hidepid=noaccess 0 0\n")
    proc = _run_restore(sandbox, scan_mode="plain")
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"plain scan proceeded under hidepid=noaccess\n{combined}"
    assert not find_log.exists(), (
        "the unprivileged scan RAN under an enabled hidepid value — it cannot "
        f"see every PID there and must refuse without scanning:\n"
        f"{find_log.read_text() if find_log.exists() else ''}"
    )

    # Control: with hidepid explicitly off the scan must actually run —
    # otherwise the assertion above would also pass with a guard that simply
    # refuses every plain scan.
    fake_mounts.write_text("proc /proc proc rw,nosuid,nodev,noexec,relatime,hidepid=off 0 0\n")
    proc = _run_restore(sandbox, scan_mode="plain")
    assert find_log.exists() and "/proc/" in find_log.read_text(), (
        "control failed: the plain scan did not run with hidepid=off — the "
        "detection is refusing unconditionally, or the stubs are not wired"
    )


def test_journal_holder_still_refuses(sandbox):
    """A holder of the rollback journal is a database holder.

    SQLite's sidecar family is {-journal, -wal, -shm} — this script's own stage
    cleanup enumerates all three. The old prefix pattern matched `-journal`
    holders; the exact-match set must keep them, or tightening the match trades
    a false refusal for a shrunk refusal surface.
    """
    db = _seed_live_db(sandbox["gd"])
    journal = sandbox["gd"] / "data" / "genesis.db-journal"
    journal.write_bytes(b"hot journal")
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 3<"$1"; sleep 60', "_", str(journal)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_fd_holder(holder.pid, journal)
        proc = _run_restore(sandbox, scan_mode="plain")
        combined = proc.stdout + proc.stderr
        assert "open process handles" in combined, (
            "a -journal holder was not treated as a database holder — the "
            f"exact-match set dropped the journal (rc={proc.returncode})\n{combined}"
        )
        assert proc.returncode != 0, "a refused restore must not exit 0"
    finally:
        holder.kill()
        holder.wait()
        db.unlink(missing_ok=True)


def test_hidepid_reads_the_effective_overmounted_proc_line(sandbox):
    """Only the LAST /proc mount line is the effective one.

    /proc/mounts lists overmounts in order; with an older `hidepid=off` procfs
    overmounted by a `hidepid=2` one, an any-line match finds the stale `off`
    entry and scans anyway — a fail-open in exactly the direction the hidepid
    refusal exists to prevent (measured during review).
    """
    _seed_live_db(sandbox["gd"])
    fake_mounts = sandbox["tmp"] / "mounts"
    find_log = sandbox["tmp"] / "find.argv"
    _write_proc_mounts_redirect(sandbox["bind"], fake_mounts)
    _write_find_proc_recorder(sandbox["bind"], find_log)

    fake_mounts.write_text(
        "proc /proc proc rw,nosuid,nodev,noexec,relatime,hidepid=off 0 0\n"
        "proc /proc proc rw,nosuid,nodev,noexec,relatime,hidepid=2 0 0\n"
    )
    proc = _run_restore(sandbox, scan_mode="plain")
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        f"plain scan proceeded under an effective hidepid=2 overmount\n{combined}"
    )
    assert not find_log.exists(), (
        "the unprivileged scan RAN although the EFFECTIVE (last) /proc mount "
        "line enables hidepid — a stale hidepid=off line above it was matched:\n"
        f"{find_log.read_text() if find_log.exists() else ''}"
    )


def test_db_file_directory_refuses_before_touching_anything(sandbox):
    """A DB_FILE that is a DIRECTORY must refuse, not swallow the staged DB.

    ``mv SOURCE DIR`` moves the source INSIDE a directory and exits 0, so the
    swap would report success, final verification would fail against the
    directory, and the staged database would be stranded inside it.
    """
    db_dir = sandbox["gd"] / "data" / "genesis.db"
    db_dir.mkdir()

    proc = _run_restore(sandbox, scan_mode="none")

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"restore onto a directory DB_FILE must die\n{combined}"
    assert "is a directory" in combined, (
        f"the refusal did not name the directory misconfiguration\n{combined}"
    )
    assert db_dir.is_dir() and not any(db_dir.iterdir()), (
        f"something was moved INSIDE the directory at the DB path: "
        f"{[p.name for p in db_dir.iterdir()]}"
    )


def test_first_sidecar_move_failure_reports_nothing_moved(sandbox):
    """When the FIRST sidecar move fails, the message must not claim a rollback.

    With nothing yet moved, the old message said the sidecars "moved before it
    were moved back" — misstating the on-disk state to an operator reading it
    mid-incident. The bytes were fine; the report was not.
    """
    db = _seed_live_db(sandbox["gd"])
    data = sandbox["gd"] / "data"
    wal, shm = data / "genesis.db-wal", data / "genesis.db-shm"
    before = {"db": db.read_bytes(), "wal": wal.read_bytes(), "shm": shm.read_bytes()}

    # Fail mv only for the WAL's move-aside destination (the FIRST move).
    _make_stub(
        sandbox["bind"] / "mv",
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in *".pre-restore."*-wal) exit 1 ;; esac; done\n'
        'exec /bin/mv "$@"\n',
    )

    proc = _run_restore(sandbox, scan_mode="none")

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, "an injected move-aside failure must make the restore die"
    assert "nothing had been moved before it" in combined, (
        f"the failure message does not state that nothing was moved\n{combined}"
    )
    assert "were moved back" not in combined, (
        f"the failure message claims a rollback that never ran\n{combined}"
    )
    assert db.read_bytes() == before["db"], "live database changed"
    assert wal.read_bytes() == before["wal"], "WAL changed despite a failed first move"
    assert shm.read_bytes() == before["shm"], "SHM changed despite a failed first move"
