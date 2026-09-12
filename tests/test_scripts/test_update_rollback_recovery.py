"""Rollback + recovery correctness for scripts/update.sh (deploy-audit P6, #6/#7).

#7 — a rollback that runs AFTER migrations applied must restore the pre-update DB
     (else the rolled-back old code runs against a migrated schema). Gated on a
     MIGRATIONS_RAN flag set BEFORE the runner (a partial failure still altered
     the schema); the server is stopped during rollback so the DB is quiescent.
#6 — the restart+health block is skipped when WERE_RUNNING is empty, so a
     recovery re-run (a prior failed update left the server stopped) recorded
     "success" with the server down and no health check. Detect a recovery run
     via failure artifacts and force genesis-server back; an operator-stop is
     respected but recorded with a degraded note, not a bare success.

update.sh can't run in CI (destructive), so most locks are extraction-style,
plus one functional test of the recovery DECISION logic.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


# ── #7 — DB restore on rollback when migrated ──────────────────────────────
def test_migrations_ran_flag_set_before_runner(text: str) -> None:
    runner = text.find("-m genesis.db.migrations --apply")
    flag = text.find("MIGRATIONS_RAN=1")
    assert -1 < flag < runner, "MIGRATIONS_RAN=1 must be set BEFORE the migration runner"
    assert "MIGRATIONS_RAN=0" in text, "the flag must be initialized to 0"


def test_rollback_restores_db_only_when_migrated(text: str) -> None:
    rb = text[text.find("_do_rollback() {") : text.find("To diagnose")]
    assert '[ "${MIGRATIONS_RAN:-0}" = "1" ]' in rb, "DB restore must be gated on MIGRATIONS_RAN"
    assert '[ "${DB_SNAPSHOT_TAKEN:-0}" != "1" ]' in rb, (
        "restore must trust the THIS-RUN snapshot flag, not mere file existence "
        "(a stale prior-run snapshot must not be restored as if current)"
    )
    assert "migrations ran but no current DB snapshot exists" in rb, (
        "migrated-but-no-snapshot must be a LOUD rollback failure, not a silent success"
    )
    assert 'cp "$DB_FILE.pre-update" "$DB_FILE"' in rb, (
        "restore copies the pre-update snapshot back"
    )
    # The snapshot flag is set ONLY on a successful `.backup` this run.
    snap = text[text.find("--- Snapshotting database ---") : text.find("_do_rollback() {")]
    assert "DB_SNAPSHOT_TAKEN=1" in snap, "flag set on successful .backup"
    assert snap.index("DB_SNAPSHOT_TAKEN=1") > snap.index('".backup'), (
        "flag set AFTER the .backup succeeds, inside the success branch"
    )
    assert 'rm -f "$DB_FILE-wal" "$DB_FILE-shm"' in rb, (
        "restore MUST clear the stale WAL/SHM or SQLite replays the migrated changes"
    )
    assert '[ "$server_down" != "true" ]' in rb, (
        "DB restore must be skipped if the server is not confirmed down (don't cp a live DB)"
    )
    assert '[ "$db_ok" = "true" ]' in rb, "db_ok must gate the rolled_back-vs-failed outcome"
    assert "systemctl --user daemon-reload" in rb, "rollback should daemon-reload for unit drift"
    assert "NOT reverted: installed systemd units" in rb, (
        "header must state what rollback does NOT restore"
    )


# ── #6 — recovery detection ────────────────────────────────────────────────
def test_recovery_detection_forces_server_restart(text: str) -> None:
    # Between health_check state and the restart block.
    seg = text[text.find('_write_state "health_check"') : text.find("# ── Restart services")]
    assert "last_update_failure.json" in seg, "recovery detection checks the failure artifact"
    assert "rolled_back | failed)" in seg, "and the last update_history status"
    assert 'WERE_RUNNING+=("genesis-server")' in seg, (
        "a recovery run forces genesis-server to restart"
    )
    assert "_OPERATOR_STOP=true" in seg, (
        "an operator-stop (no artifact) is recorded, not force-started"
    )


def test_operator_stop_recorded_as_degraded_not_bare_success(text: str) -> None:
    assert "genesis-server-not-restarted" in text, "operator-stop must flag the not-running server"


def test_noop_path_clears_stale_failure_signals_when_server_was_up(text: str) -> None:
    """The 'Already up to date' no-op path must clear a stale
    last_update_failure.json (and supersede the rolled_back status) when the
    server was up at start — else a long-resolved failure later force-restarts an
    operator-stopped server. Gated on server-was-up so an UNRESOLVED failure's
    artifact survives to drive recovery."""
    noop = text[text.find("Already up to date") : text.find("Nothing to do")]
    assert 'rm -f "$HOME/.genesis/last_update_failure.json"' in noop, (
        "no-op path must clear the stale failure marker"
    )
    assert '[[ " ${WERE_RUNNING[*]} " == *" genesis-server "* ]]' in noop, (
        "the clear must be gated on genesis-server having been up at start"
    )
    # The clear and the status supersession happen together.
    clear_at = noop.find('rm -f "$HOME/.genesis/last_update_failure.json"')
    record_at = noop.find('_record_update_history "success"', clear_at)
    assert clear_at < record_at < clear_at + 200, (
        "clearing the artifact must record a fresh success to supersede the stale status row"
    )


def _extract_recovery_block(text: str) -> str:
    seg = text[text.find("_OPERATOR_STOP=false") : text.find("# ── Restart services")]
    # Run against python3 + a test DB (drop the venv-specific interpreter path).
    return seg.replace('"$VENV_DIR/bin/python"', "python3")


def _run_recovery(tmp_path: Path, text: str, *, artifact: bool, last_status: str | None) -> bool:
    """Drive the shipped recovery-detection block. Returns True if it decided
    this is a recovery run (genesis-server added to WERE_RUNNING)."""
    # Unique per call so a test can invoke this helper more than once.
    home = tmp_path / f"home_{artifact}_{last_status}"
    (home / ".genesis").mkdir(parents=True)
    (home / "genesis" / "data").mkdir(parents=True)
    if artifact:
        (home / ".genesis" / "last_update_failure.json").write_text("{}")
    db = home / "genesis" / "data" / "genesis.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE update_history (status TEXT, started_at TEXT)")
    if last_status is not None:
        con.execute(
            "INSERT INTO update_history VALUES (?, ?)", (last_status, "2026-07-22T00:00:00")
        )
    con.commit()
    con.close()
    harness = f"""#!/bin/bash
set -Eeuo pipefail
WERE_RUNNING=()
{_extract_recovery_block(text)}
printf '%s\\n' "${{WERE_RUNNING[@]:-}}"
"""
    script = tmp_path / "h.sh"
    script.write_text(harness)
    out = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert out.returncode == 0, out.stderr
    return "genesis-server" in out.stdout


def test_recovery_run_from_failure_artifact(tmp_path: Path, text: str) -> None:
    assert _run_recovery(tmp_path, text, artifact=True, last_status="success") is True


def test_recovery_run_from_failed_status(tmp_path: Path, text: str) -> None:
    assert _run_recovery(tmp_path, text, artifact=False, last_status="failed") is True
    assert _run_recovery(tmp_path, text, artifact=False, last_status="rolled_back") is True


def test_operator_stop_is_not_a_recovery(tmp_path: Path, text: str) -> None:
    # Clean prior state (last success, no artifact) → operator-stop, NOT forced restart.
    assert _run_recovery(tmp_path, text, artifact=False, last_status="success") is False
    assert _run_recovery(tmp_path, text, artifact=False, last_status=None) is False
