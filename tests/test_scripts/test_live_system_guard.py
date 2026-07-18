"""Tests for scripts/lib/live_system_guard.sh — bootstrap's live-system guard.

Running bootstrap.sh on a live machine (installers, MCP/systemd re-registration,
dotfile edits on top of a running server) was the direct trigger of the
2026-07-13 eth0/load incident. The guard makes the "bootstrap is not a deploy
path" rule mechanical: refuse while genesis-server is live, unless update.sh's
GENESIS_BOOTSTRAP_ALLOW_LIVE opt-out or a human --force is present.

Environment-independent: ``systemctl`` and ``pgrep`` are BOTH faked via PATH
injection (the real ones would detect this dev machine's actually-live server),
so the cases run identically in CI and on a workstation.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "lib" / "live_system_guard.sh"

_SYSTEM_PATH = "/usr/bin:/bin"  # real bash for the harness


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _stub_detection(bindir: Path, *, systemctl_rc: int, pgrep_rc: int) -> None:
    """Fake systemctl + pgrep with fixed exit codes (all the guard reads)."""
    bindir.mkdir(exist_ok=True)
    _write_exec(bindir / "systemctl", f"#!/usr/bin/env bash\nexit {systemctl_rc}\n")
    _write_exec(bindir / "pgrep", f"#!/usr/bin/env bash\nexit {pgrep_rc}\n")


def _run_guard(bindir: Path, *args: str, env_extra: dict | None = None):
    env = {
        "PATH": f"{bindir}:{_SYSTEM_PATH}",
        "HOME": os.environ.get("HOME", "/tmp"),
        **(env_extra or {}),
    }
    return subprocess.run(
        ["bash", "-c", f'. "{_LIB}" && bootstrap_refuse_if_server_live "$@"', "guard", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_live_server_refused_without_override(tmp_path: Path) -> None:
    """systemd reports active, no --force, no opt-out → refuse (rc=1)."""
    bindir = tmp_path / "bin"
    _stub_detection(bindir, systemctl_rc=0, pgrep_rc=1)
    res = _run_guard(bindir)
    assert res.returncode == 1
    assert "REFUSED" in res.stderr
    assert "scripts/update.sh" in res.stderr  # teaches the sanctioned deploy path
    assert "--force" in res.stderr  # and the override


def test_live_server_force_proceeds_with_warning(tmp_path: Path) -> None:
    """--force on a live server → proceed (rc=0) but warn loudly."""
    bindir = tmp_path / "bin"
    _stub_detection(bindir, systemctl_rc=0, pgrep_rc=1)
    res = _run_guard(bindir, "--force")
    assert res.returncode == 0
    assert "WARNING" in res.stdout
    assert "LIVE" in res.stdout


def test_allow_live_env_is_silent_optout(tmp_path: Path) -> None:
    """update.sh's GENESIS_BOOTSTRAP_ALLOW_LIVE=1 → rc=0, no output at all.

    This is the deploy path: update.sh's ERR trap is armed around its bootstrap
    call, so any refusal here would escalate into a full update rollback.
    """
    bindir = tmp_path / "bin"
    _stub_detection(bindir, systemctl_rc=0, pgrep_rc=0)  # maximally "live"
    res = _run_guard(bindir, env_extra={"GENESIS_BOOTSTRAP_ALLOW_LIVE": "1"})
    assert res.returncode == 0
    assert res.stdout == ""
    assert res.stderr == ""


def test_inactive_server_proceeds_silently(tmp_path: Path) -> None:
    """Unit unknown/inactive (rc=4) and no process → rc=0, silent (fail-open)."""
    bindir = tmp_path / "bin"
    _stub_detection(bindir, systemctl_rc=4, pgrep_rc=1)
    res = _run_guard(bindir)
    assert res.returncode == 0
    assert res.stdout == ""
    assert res.stderr == ""


def test_pgrep_detects_live_when_systemd_unavailable(tmp_path: Path) -> None:
    """No usable systemd (rc=4) but a bare `genesis serve` process → refuse."""
    bindir = tmp_path / "bin"
    _stub_detection(bindir, systemctl_rc=4, pgrep_rc=0)
    res = _run_guard(bindir)
    assert res.returncode == 1
    assert "REFUSED" in res.stderr


def test_bootstrap_sources_and_calls_guard() -> None:
    """Wiring guardrail: bootstrap.sh must source the lib and gate on it
    BEFORE the crash-recovery block (whose git reset --hard is unsafe live)."""
    bootstrap = (_REPO_ROOT / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    source_pos = bootstrap.find("lib/live_system_guard.sh")
    call_pos = bootstrap.find("bootstrap_refuse_if_server_live")
    crash_pos = bootstrap.find("Crash recovery")
    assert source_pos != -1, "bootstrap.sh no longer sources live_system_guard.sh"
    assert call_pos != -1, "bootstrap.sh no longer calls bootstrap_refuse_if_server_live"
    assert crash_pos != -1
    assert call_pos < crash_pos, "guard must run before the crash-recovery git reset"


def test_update_sh_sets_optout_on_bootstrap_call() -> None:
    """Wiring guardrail: update.sh's bootstrap invocation must carry the
    opt-out env — a refusal there would trigger a full ERR-trap rollback."""
    update = (_REPO_ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    for line in update.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "scripts/bootstrap.sh" not in stripped:
            continue
        # Only actual invocations (not comments/path lists) must carry the env.
        if stripped.endswith("scripts/bootstrap.sh \\") or 'bootstrap.sh" 2>&1' in stripped:
            assert "GENESIS_BOOTSTRAP_ALLOW_LIVE=1" in stripped, (
                f"update.sh bootstrap invocation missing opt-out: {stripped}"
            )
