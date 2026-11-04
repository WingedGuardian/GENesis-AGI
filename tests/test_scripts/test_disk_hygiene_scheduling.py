"""Exercise the daily entry points without touching install state."""

from __future__ import annotations

import configparser
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_mode(tmp_path, mode, *, failure=""):
    home = tmp_path / "home"
    home.mkdir()
    calls = tmp_path / "calls"
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'case "$*" in *"$FAILURE"*) [ -z "$FAILURE" ] || exit 7 ;; esac\n'
    )
    python.chmod(0o700)
    script = '''
source "$HYGIENE"
VENV_PY="$FAKE_PYTHON"
REPO_DIR="$TEST_REPO"
reclaim_lock() { return 0; }
reclaim_unlock() { :; }
reap_bg_sandboxes() { :; }
prune_tmp() { :; }
prune_mcp_spawn() { :; }
prune_guard_corpus() { :; }
prune_browser_backups() { :; }
prune_camoufox_engines_step() { :; }
rotate_mcp_logs() { :; }
main "$MODE"
'''
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=15,
        env={**os.environ, "HOME": str(home), "CALLS": str(calls),
             "FAKE_PYTHON": str(python), "TEST_REPO": str(tmp_path / "repo"),
             "HYGIENE": str(ROOT / "scripts/disk_hygiene.sh"),
             "MODE": mode, "FAILURE": failure},
    )
    return result, calls.read_text().splitlines() if calls.exists() else []


@pytest.mark.parametrize("mode", ["--maintenance-only", "--worktrees-only", ""])
def test_modes_keep_observation_before_reaping_and_separate_maintenance(tmp_path, mode):
    result, calls = run_mode(tmp_path, mode)
    assert result.returncode == 0, result.stderr
    worktrees = [c for c in calls if "worktree_lifecycle.py" in c]
    maintenance = [c for c in calls if "disk_reclaim.py" in c]
    assert bool(worktrees) == (mode != "--maintenance-only")
    assert bool(maintenance) == (mode != "--worktrees-only")
    if worktrees:
        assert "zero_drop_worker.py" in calls[0]
        assert "--release-stale-claims" in calls[1]
        assert calls[2].endswith("worktree_lifecycle.py")
    assert "duration_s=" in result.stdout


def test_reaper_failure_is_visible_and_manual_mode_still_maintains(tmp_path):
    result, calls = run_mode(tmp_path, "", failure="worktree_lifecycle.py")
    assert result.returncode == 7
    assert any("disk_reclaim.py" in c for c in calls)
    assert "status=7" in result.stdout


def test_services_run_independently_under_the_existing_timer():
    units = ROOT / "scripts/systemd"
    maintenance = configparser.ConfigParser(interpolation=None)
    maintenance.read(units / "genesis-disk-hygiene.service.template")
    assert maintenance["Service"]["ExecStart"].endswith("--maintenance-only")
    assert maintenance["Unit"]["Wants"] == "genesis-worktree-reaper.service"
    for key in ("After", "Before", "Requires", "BindsTo", "PartOf"):
        assert "genesis-worktree-reaper" not in maintenance["Unit"].get(key, "")
    reaper = configparser.ConfigParser(interpolation=None)
    reaper.read(units / "genesis-worktree-reaper.service.template")
    assert reaper["Service"]["ExecStart"].endswith("--worktrees-only")
    assert all(u["Service"]["TimeoutStartSec"] == "7200" for u in (maintenance, reaper))
    assert "genesis-worktree-reaper.service" in (ROOT / "scripts/uninstall.sh").read_text()
