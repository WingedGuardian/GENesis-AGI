"""Deploy-mutex lock for scripts/update.sh (deploy-audit P5-A, finding #5).

Two update.sh runs (a CLI run + a dashboard-triggered run, or two dashboard
runs) could previously overlap — both stop the server, both merge — and corrupt
the deploy. This was observed LIVE (a concurrent session deployed mid-session).
A whole-run `flock -n` now makes a second run refuse immediately.

Extraction locks assert the shipped guard + its placement; a functional test
proves the flock mechanism actually excludes a second holder.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


def test_flock_guard_present(text: str) -> None:
    assert 'UPDATE_LOCK_FILE="$HOME/.genesis/locks/update.lock"' in text
    assert 'exec {_UPDATE_LOCK_FD}>"$UPDATE_LOCK_FILE"' in text
    assert 'flock -n "$_UPDATE_LOCK_FD"' in text


def test_nohup_fallback_closes_lock_fd(text: str) -> None:
    """The degraded nohup server OUTLIVES update.sh; it must not inherit the lock
    FD, or the advisory lock stays held after exit and deadlocks every future
    update. The nohup fallback closes the lock FD for that child."""
    nohup = text.find('nohup "$VENV_DIR/bin/python" -m genesis serve')
    assert nohup != -1, "nohup fallback not found"
    block = text[nohup : nohup + 200]
    assert "{_UPDATE_LOCK_FD}>&-" in block, "nohup fallback must close the lock FD for the child"


def test_flock_after_worktree_refusal_before_backup(text: str) -> None:
    """Placement: after the worktree refusal (so worktree runs never take the
    lock) and before the rollback tag / pre-update backup (so the whole mutating
    run is protected) — and thus before the ERR/signal traps arm."""
    worktree = text.find("update.sh must not run from a worktree")
    lock = text.find('exec {_UPDATE_LOCK_FD}>"$UPDATE_LOCK_FILE"')
    rollback_tag = text.find('ROLLBACK_TAG="pre-update-')
    trap_arm = text.find("trap _on_err ERR")
    assert -1 < worktree < lock < rollback_tag, (
        "flock must sit after worktree refusal, before the rollback tag"
    )
    assert lock < trap_arm, (
        "flock must acquire before the ERR trap arms (contention exit is server-safe)"
    )


def test_flock_functionally_excludes_second_run(tmp_path: Path) -> None:
    """The shipped flock pattern must let exactly one holder in; a second
    non-blocking attempt fails fast with exit 1."""
    lockfile = tmp_path / "update.lock"
    ready = tmp_path / "ready"
    # Holder: acquires the lock exactly like update.sh, signals READY, holds it.
    holder_sh = f"""#!/bin/bash
set -euo pipefail
exec {{FD}}>"{lockfile}"
flock -n "$FD" || {{ echo "HOLDER_FAILED"; exit 9; }}
touch "{ready}"
sleep 5
"""
    # Contender: same non-blocking acquire — must fail while the holder holds it.
    contender_sh = f"""#!/bin/bash
set -euo pipefail
exec {{FD}}>"{lockfile}"
if ! flock -n "$FD"; then echo "LOCKED"; exit 1; fi
echo "ACQUIRED"
"""
    (tmp_path / "holder.sh").write_text(holder_sh)
    (tmp_path / "contender.sh").write_text(contender_sh)

    holder = subprocess.Popen(["bash", str(tmp_path / "holder.sh")])
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "holder never acquired the lock"
        result = subprocess.run(
            ["bash", str(tmp_path / "contender.sh")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1, f"contender should be refused, got rc={result.returncode}"
        assert "LOCKED" in result.stdout
        assert "ACQUIRED" not in result.stdout
    finally:
        holder.wait(timeout=10)

    # After the holder exits, the lock is free again — a fresh acquire succeeds.
    free = subprocess.run(
        ["bash", str(tmp_path / "contender.sh")], capture_output=True, text=True, timeout=10
    )
    assert free.returncode == 0 and "ACQUIRED" in free.stdout
