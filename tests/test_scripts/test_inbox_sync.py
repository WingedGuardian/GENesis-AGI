"""Tests for scripts/inbox_sync.sh — vault↔local response-file sync.

Focus: the cleanup step (step 2) must be FAIL-CLOSED on the vault listing.
Regression under test (2026-07-29 incident): when `rclone lsf` failed during a
Dropbox API outage, `REMOTE_GENESIS` silently became empty and the cleanup loop
deleted every local `*.genesis.md` response file (150 in one cycle) as
"deleted from vault" — a false positive that reset the response counter and
caused silent overwrites of already-delivered files in the user's vault.

Also locked here:
- on a failed listing the step-3 push is age-gated so an old local copy of a
  vault-side deletion cannot be re-uploaded (resurrected) before the next
  healthy cycle can clean it;
- filenames are handled as data (leading dash, spaces, ``&``, apostrophes —
  the live vault's actual shapes), never as grep/rm options;
- listing stderr stays out of the membership data.

A durable "seen on the vault" ledger (never-landed-push protection) is
deliberately NOT part of this change — deferred to its own PR.

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


def _copy_calls(call_log: Path) -> list[str]:
    return [c for c in call_log.read_text().splitlines() if c.startswith("rclone copy")]


def test_lsf_failure_skips_cleanup_entirely(tmp_path):
    """REPRO of the 2026-07-29 wipe: listing fails → NOTHING may be deleted.

    Also asserts the SKIP line carries the diagnostics (rc + rclone stderr)."""
    env, inbox, log, _ = _setup(tmp_path)
    kept = [_old_response_file(inbox, f"Genesis-{n}.genesis.md") for n in (1, 2, 3)]
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


def test_failed_listing_push_is_age_gated(tmp_path):
    """On a failed-listing cycle the push must be gated to fresh files
    (--max-age) — otherwise an old local copy of a file the user just deleted
    on the vault is silently re-uploaded (resurrection), because cleanup is
    skipped but the push still runs."""
    env, inbox, _, call_log = _setup(tmp_path)
    _old_response_file(inbox, "Genesis-1.genesis.md")
    env["RCLONE_LSF_RC"] = "1"

    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = _copy_calls(call_log)
    assert calls, "push step did not run after a failed listing"
    assert "--max-age" in calls[-1], (
        "failed-listing push not age-gated — old files can resurrect deletions"
    )


def test_healthy_cycle_push_not_age_gated(tmp_path):
    """A healthy cycle must push ALL responses (no age gate)."""
    env, inbox, _, call_log = _setup(tmp_path)
    _old_response_file(inbox, "Genesis-1.genesis.md")
    _set_listing(env, ["Genesis-1.genesis.md"])

    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = _copy_calls(call_log)
    assert calls and "--max-age" not in calls[-1], (
        "healthy-cycle push must NOT be age-gated (all responses ship)"
    )


def test_healthy_listing_cleans_user_deleted_file(tmp_path):
    """Intent lock, with the filename shapes a real vault produces
    (spaces / & / apostrophe): a file absent from a HEALTHY listing and older
    than grace is a user deletion → cleaned; a still-listed file stays."""
    env, inbox, log, _ = _setup(tmp_path)
    gone = _old_response_file(inbox, "My todos & musings-1.genesis.md")
    stays = _old_response_file(inbox, "Someone's Notes-2.genesis.md")
    _set_listing(env, [stays.name])  # user deleted the first in Obsidian

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

    _set_listing(env, [])  # now genuinely gone from the vault
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert not dashy.exists()


def test_grace_period_keeps_recent_unpushed_file(tmp_path):
    """A response newer than 10min missing from the vault is NOT deleted."""
    env, inbox, _, _ = _setup(tmp_path)
    fresh = inbox / "Genesis-9.genesis.md"
    fresh.write_text("just written, not yet pushed\n")
    _set_listing(env, [])  # healthy but empty listing

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert fresh.exists()


def test_bulk_deletion_on_healthy_listing_warns_but_proceeds(tmp_path):
    """A large legit cleanup still works (no resurrection loop) but logs loudly."""
    env, inbox, log, _ = _setup(tmp_path)
    files = [_old_response_file(inbox, f"Genesis-{n}.genesis.md") for n in range(1, 9)]
    _set_listing(env, [])  # user deleted all of them in Obsidian

    r = _run(env)

    assert r.returncode == 0, r.stderr
    for f in files:
        assert not f.exists(), "healthy-listing deletions must still be honored"
    body = log.read_text()
    assert "WARNING: bulk cleanup" in body, "expected loud bulk-deletion warning"


def test_state_file_excluded_from_mirror_and_not_pushed(tmp_path):
    """The step-1 sync must carry --exclude '.genesis-*' so the mirror can
    never delete the sync's own state (the in-mirror response counter was
    deleted every cycle); and .genesis-* is never pushed to the vault."""
    env, inbox, _, call_log = _setup(tmp_path)
    _set_listing(env, [])
    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = call_log.read_text().splitlines()
    sync_calls = [c for c in calls if c.startswith("rclone sync")]
    assert sync_calls and "--exclude .genesis-*" in sync_calls[0], (
        "step-1 sync missing --exclude .genesis-* — the mirror will delete local state files"
    )
    copy_calls = _copy_calls(call_log)
    assert copy_calls and "--include *.genesis.md" in copy_calls[0], (
        "push must be scoped to *.genesis.md so .genesis-* state is never pushed"
    )
