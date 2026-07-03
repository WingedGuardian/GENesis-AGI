"""Tests for the ~/tmp age-prune in scripts/disk_hygiene.sh (the ``prune_tmp`` fn).

Sources the script (which only DEFINES functions when sourced — ``main`` is guarded) and
calls ``prune_tmp`` against a fixture dir, mirroring test_watchgod_instrumentation's
source-and-call pattern. Age is set via os.utime -> wall-clock-independent.
"""

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _age(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def _run_prune(tmp_dir: Path) -> subprocess.CompletedProcess:
    """Source disk_hygiene.sh (defines functions, does NOT run main) then call prune_tmp."""
    return subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nprune_tmp '{tmp_dir}'"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def test_old_file_pruned(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    old = d / "old_job.log"
    old.write_text("x")
    _age(old, 10)
    _run_prune(d)
    assert not old.exists()


def test_old_dir_pruned_whole(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    old = d / "old_job"
    old.mkdir()
    (old / "inner").write_text("x")
    _age(old / "inner", 10)
    _age(old, 10)  # set dir mtime AFTER creating contents
    _run_prune(d)
    assert not old.exists()


def test_recent_file_kept(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    fresh = d / "running.log"
    fresh.write_text("x")
    _age(fresh, 1)
    _run_prune(d)
    assert fresh.exists()


def test_bg_cc_sessions_excluded(tmp_path):
    """bg-cc-sessions is reaped separately at 24h — the 7d prune must never touch it."""
    d = tmp_path / "tmp"
    d.mkdir()
    bg = d / "bg-cc-sessions"
    bg.mkdir()
    (bg / "sess").mkdir()
    _age(bg, 30)
    _run_prune(d)
    assert bg.exists(), "bg-cc-sessions must be excluded from the 7d prune"


def test_missing_dir_is_noop(tmp_path):
    r = _run_prune(tmp_path / "does_not_exist")
    assert r.returncode == 0
