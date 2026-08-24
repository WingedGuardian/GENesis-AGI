"""Tests for scripts/inbox_sync.sh — vault↔local response-file sync.

Focus: the cleanup step (step 2) must be FAIL-CLOSED on the vault listing.
Regression under test (2026-07-29 incident): when `rclone lsf` failed during a
Dropbox API outage, `REMOTE_GENESIS` silently became empty and the cleanup loop
deleted every local `*.genesis.md` response file (150 in one cycle) as
"deleted from vault" — a false positive that reset the response counter and
caused silent overwrites of already-delivered files in the user's vault.

Also locked here (2026-08-24 adversarial review):
- deletion requires the file to have been SEEN on the vault at least once
  (a healthy listing containing it), so a push that never landed is retried,
  not reaped;
- filenames are handled as data (leading dash, spaces, ``&``, apostrophes —
  the live vault's actual shapes), never as grep/rm options;
- listing stderr stays out of the membership data.

The intended behavior being preserved: a response file the user genuinely
deleted in Obsidian (absent from a HEALTHY listing, previously seen on the
vault, older than the 10-minute grace period) is still cleaned up locally.

Runs the REAL script with a fake `rclone` on PATH (behavior driven by env).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inbox_sync.sh"
_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

_FAKE_RCLONE = """#!/bin/bash
# Fake rclone for inbox_sync.sh tests. Dispatch on subcommand.
echo "rclone $*" >> "$RCLONE_CALL_LOG"
case "$1" in
  lsf)
    if [ "${RCLONE_LSF_RC:-0}" -ne 0 ]; then
      echo "simulated outage: dial tcp: lookup api.dropboxapi.com: i/o timeout" >&2
      exit "$RCLONE_LSF_RC"
    fi
    if [ -n "${RCLONE_LSF_OUTPUT_FILE:-}" ] && [ -f "$RCLONE_LSF_OUTPUT_FILE" ]; then
      cat "$RCLONE_LSF_OUTPUT_FILE"
    fi
    ;;
  sync|copy)
    exit 0
    ;;
esac
exit 0
"""


def _setup(tmp_path: Path) -> tuple[dict, Path, Path, Path]:
    """Create shim bin, local inbox dir, log path; return (env, inbox, log, call_log)."""
    shim_bin = tmp_path / "bin"
    shim_bin.mkdir()
    rclone = shim_bin / "rclone"
    rclone.write_text(_FAKE_RCLONE)
    rclone.chmod(0o755)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    log = tmp_path / "inbox_sync.log"
    call_log = tmp_path / "rclone_calls.log"
    call_log.write_text("")
    lsf_out = tmp_path / "lsf_output.txt"
    lsf_out.write_text("")

    env = {
        "PATH": f"{shim_bin}:{_BASE_PATH}",
        "HOME": str(tmp_path),
        "GENESIS_INBOX_PATH": str(inbox),
        "GENESIS_INBOX_SYNC_LOG": str(log),
        "RCLONE_CALL_LOG": str(call_log),
        "RCLONE_LSF_OUTPUT_FILE": str(lsf_out),
    }
    return env, inbox, log, call_log


def _old_response_file(inbox: Path, name: str) -> Path:
    """Create a response file older than the 10-minute grace period."""
    f = inbox / name
    f.write_text("response body\n")
    old = time.time() - 20 * 60
    os.utime(f, (old, old))
    return f


def _set_listing(env: dict, names: list[str]) -> None:
    Path(env["RCLONE_LSF_OUTPUT_FILE"]).write_text(
        "".join(f"{n}\n" for n in names),
    )


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_lsf_failure_skips_cleanup_entirely(tmp_path):
    """REPRO of the 2026-07-29 wipe: listing fails → NOTHING may be deleted.

    Also asserts the SKIP line carries the diagnostics (rc + rclone stderr)."""
    env, inbox, log, _ = _setup(tmp_path)
    kept = [_old_response_file(inbox, f"Genesis-{n}.genesis.md") for n in (1, 2, 3)]
    # Make every file eligible for deletion were the gate broken: seen before
    _set_listing(env, [f.name for f in kept])
    r = _run(env)
    assert r.returncode == 0, r.stderr

    env["RCLONE_LSF_RC"] = "7"  # Dropbox API outage: lsf fails
    r = _run(env)

    assert r.returncode == 0, r.stderr
    for f in kept:
        assert f.exists(), f"{f.name} was wiped on a failed listing (fail-open bug)"
    body = log.read_text()
    assert "Cleaned up (deleted from vault)" not in body
    assert "SKIP cleanup" in body, "expected a loud skip marker in the sync log"
    assert "rc=7" in body, "SKIP line must carry the listing exit code"
    assert "simulated outage" in body, "SKIP line must carry rclone's stderr"


def test_lsf_failure_still_pushes_responses(tmp_path):
    """A failed listing must not block step 3 — responses still reach the vault."""
    env, inbox, _, call_log = _setup(tmp_path)
    _old_response_file(inbox, "Genesis-1.genesis.md")
    env["RCLONE_LSF_RC"] = "1"

    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = call_log.read_text()
    assert "rclone copy" in calls, "push step skipped after a failed listing"


def test_healthy_listing_still_cleans_user_deleted_file(tmp_path):
    """Intent lock, with the live corpus's filename shapes (spaces/&/'):

    cycle 1: both files listed (seen recorded);
    cycle 2: one vanishes from a healthy listing → user deletion → cleaned;
    the still-listed one stays."""
    env, inbox, log, _ = _setup(tmp_path)
    gone = _old_response_file(inbox, "My todos & musings-1.genesis.md")
    stays = _old_response_file(inbox, "Jay's Notes-2.genesis.md")

    _set_listing(env, [gone.name, stays.name])
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert gone.exists() and stays.exists()

    _set_listing(env, [stays.name])
    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert not gone.exists(), "genuine user deletion no longer honored"
    assert stays.exists()
    assert "Cleaned up (deleted from vault): My todos & musings-1.genesis.md" in log.read_text()


def test_leading_dash_name_kept_when_listed(tmp_path):
    """A leading-dash filename is DATA, not grep options: when the healthy
    listing contains it, it must be kept (grep -qxF -- regression)."""
    env, inbox, _, _ = _setup(tmp_path)
    dashy = _old_response_file(inbox, "-x notes-1.genesis.md")
    _set_listing(env, [dashy.name])

    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert dashy.exists(), (
        "leading-dash filename deleted despite being in a healthy listing (grep read it as options)"
    )

    # And when it truly disappears from the vault, it IS cleaned up.
    _set_listing(env, [])
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert not dashy.exists()


def test_never_pushed_file_is_not_reaped(tmp_path):
    """Push-failure protection: a response the vault has NEVER listed (push
    kept failing while the listing stayed healthy) is not a user deletion —
    it must survive to be retried, not be reaped at the grace period."""
    env, inbox, log, _ = _setup(tmp_path)
    unlanded = _old_response_file(inbox, "Genesis-200.genesis.md")
    _set_listing(env, [])  # healthy, but the push never landed the file

    for _ in range(2):
        r = _run(env)
        assert r.returncode == 0, r.stderr

    assert unlanded.exists(), (
        "never-vault-listed response reaped as a 'user deletion' — "
        "push failures must be retried, not destroyed"
    )
    assert "Cleaned up" not in log.read_text()


def test_grace_period_keeps_recent_unpushed_file(tmp_path):
    """A response newer than 10min missing from the vault is NOT deleted."""
    env, inbox, _, _ = _setup(tmp_path)
    fresh = inbox / "Genesis-9.genesis.md"
    fresh.write_text("just written, not yet pushed\n")
    # healthy but empty listing
    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert fresh.exists()


def test_bulk_deletion_on_healthy_listing_warns_but_proceeds(tmp_path):
    """A large legit cleanup still works (no resurrection loop) but logs loudly."""
    env, inbox, log, _ = _setup(tmp_path)
    files = [_old_response_file(inbox, f"Genesis-{n}.genesis.md") for n in range(1, 9)]

    _set_listing(env, [f.name for f in files])  # cycle 1: all on vault (seen)
    r = _run(env)
    assert r.returncode == 0, r.stderr

    _set_listing(env, [])  # cycle 2: user deleted all of them in Obsidian
    r = _run(env)

    assert r.returncode == 0, r.stderr
    for f in files:
        assert not f.exists(), "healthy-listing deletions must still be honored"
    body = log.read_text()
    assert "WARNING: bulk cleanup" in body, "expected loud bulk-deletion warning"


def test_state_files_excluded_from_mirror_and_not_pushed(tmp_path):
    """The step-1 sync must carry --exclude '.genesis-*' so the mirror can
    never delete the sync's own state files (the in-mirror response counter
    was deleted every cycle for months)."""
    env, inbox, _, call_log = _setup(tmp_path)
    _set_listing(env, [])
    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = call_log.read_text()
    sync_calls = [c for c in calls.splitlines() if c.startswith("rclone sync")]
    assert sync_calls, "step-1 sync not invoked"
    assert "--exclude .genesis-*" in sync_calls[0], (
        "step-1 sync missing --exclude .genesis-* — the mirror will delete local state files"
    )
    # State files themselves exist after a healthy cycle
    assert (inbox / ".genesis-seen").exists()
