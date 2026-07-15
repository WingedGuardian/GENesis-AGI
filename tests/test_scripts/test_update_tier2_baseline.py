"""Tests for update.sh's _tier2_pending_since_baseline (extracted snippet).

The no-op ("Already up to date") path must fall through to full activation
when update.sh-only paths changed since the last RECORDED update — the
recovery the deploy-staleness alert advertises. These tests run the exact
shipped bash function against a throwaway git repo + sqlite update_history.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

_BEGIN = "# BEGIN tier2-baseline-check"
_END = "# END tier2-baseline-check"


def _extract_function() -> str:
    text = UPDATE_SH.read_text()
    assert _BEGIN in text and _END in text, "extraction markers missing from update.sh"
    # Drop the rest of the BEGIN marker line itself (it carries a prose suffix).
    after_marker = text.split(_BEGIN, 1)[1].split("\n", 1)[1]
    return after_marker.split(_END, 1)[0]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin",
        },
    )
    return out.stdout.strip()


@pytest.fixture
def genesis_root(tmp_path):
    """Throwaway GENESIS_ROOT: git repo + data/genesis.db + fake venv python."""
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    (root / "scripts").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "scripts" / "update.sh").write_text("# v1\n")
    (root / "unrelated.py").write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "c1")

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)

    db = sqlite3.connect(root / "data" / "genesis.db")
    db.execute(
        "CREATE TABLE update_history (id TEXT PRIMARY KEY, old_tag TEXT, new_tag TEXT,"
        " old_commit TEXT, new_commit TEXT, status TEXT, rollback_tag TEXT,"
        " failure_reason TEXT, degraded_subsystems TEXT, started_at TEXT, completed_at TEXT)"
    )
    db.commit()
    db.close()
    return root, venv


def _record_success(root: Path, commit: str, completed_at: str = "2026-01-01T00:00:00+00:00"):
    db = sqlite3.connect(root / "data" / "genesis.db")
    db.execute(
        "INSERT INTO update_history (id, status, new_commit, completed_at)"
        " VALUES (?, 'success', ?, ?)",
        (f"row-{commit}-{completed_at}", commit, completed_at),
    )
    db.commit()
    db.close()


def _run_check(root: Path, venv: Path) -> int:
    harness = (
        "set -u\n"
        f'GENESIS_ROOT="{root}"\n'
        f'VENV_DIR="{venv}"\n' + _extract_function() + "\n_tier2_pending_since_baseline\n"
    )
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=60)
    assert result.stderr == "", result.stderr
    return result.returncode


def test_tier2_change_since_baseline_is_pending(genesis_root):
    root, venv = genesis_root
    _record_success(root, _git(root, "rev-parse", "--short", "HEAD"))
    (root / "scripts" / "update.sh").write_text("# v2\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "tier2 change (bare merge)")
    assert _run_check(root, venv) == 0  # pending -> full activation


def test_non_tier2_change_is_not_pending(genesis_root):
    root, venv = genesis_root
    _record_success(root, _git(root, "rev-parse", "--short", "HEAD"))
    (root / "unrelated.py").write_text("x = 2\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "code-only change")
    assert _run_check(root, venv) == 1  # shortcut path stays


def test_no_baseline_is_not_pending(genesis_root):
    root, venv = genesis_root  # empty update_history
    assert _run_check(root, venv) == 1


def test_unresolvable_baseline_is_not_pending(genesis_root):
    root, venv = genesis_root
    _record_success(root, "deadbeef")  # does not resolve in this repo
    assert _run_check(root, venv) == 1


def test_newest_success_row_wins(genesis_root):
    root, venv = genesis_root
    old_head = _git(root, "rev-parse", "--short", "HEAD")
    _record_success(root, old_head, "2026-01-01T00:00:00+00:00")
    (root / "scripts" / "update.sh").write_text("# v2\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "tier2 change")
    # A NEWER success row at current HEAD: baseline advanced, nothing pending.
    _record_success(root, _git(root, "rev-parse", "--short", "HEAD"), "2026-02-01T00:00:00+00:00")
    assert _run_check(root, venv) == 1
