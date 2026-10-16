"""Tests for prune_backup_runs in scripts/disk_hygiene.sh: the retention prune of
~/.genesis/premerge-backups (45 days) and ~/.genesis/deploy-backups (90 days).

Sources the script (main is guarded) and calls the function against a scratch
directory, the source-and-call pattern of test_disk_hygiene_guard_corpus_prune.
"""

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _age(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def _prune(path: Path, days: int = 90) -> subprocess.CompletedProcess:
    # Paths travel as positional parameters, never as script text.
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\nprune_backup_runs "$2" deploy-backups "$3"',
            "x",
            str(_HYGIENE),
            str(path),
            str(days),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def _run_dir(parent: Path, name: str, days: float) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / "genesis-server.service").write_text("[Service]\n")
    _age(d, days)
    return d


def test_an_old_run_goes_and_a_young_one_stays(tmp_path):
    root = tmp_path / "deploy-backups"
    old = _run_dir(root, "20260101T000000Z", 120)
    young = _run_dir(root, "20261001T000000Z", 10)
    r = _prune(root)
    assert not old.exists(), r.stdout
    assert young.exists()
    assert "failed or spared" not in r.stdout


def test_a_symlinked_backup_dir_is_never_pruned_through(tmp_path):
    """A symlink points at a directory Genesis does not own; every old directory
    there would be deleted (#3138 review)."""
    elsewhere = tmp_path / "shared-archive"
    unrelated = _run_dir(elsewhere, "2025-photos", 400)
    link = tmp_path / "deploy-backups"
    link.symlink_to(elsewhere)
    r = _prune(link)
    assert unrelated.exists()
    assert "prune SKIPPED" in r.stdout and "is a symlink" in r.stdout


def test_a_missing_backup_dir_is_a_quiet_no_op(tmp_path):
    r = _prune(tmp_path / "absent")
    assert r.stdout == ""
