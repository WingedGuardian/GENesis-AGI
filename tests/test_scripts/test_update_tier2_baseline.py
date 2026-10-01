"""Tests for update.sh's _tier2_pending_since_baseline (extracted snippet).

The no-op ("Already up to date") path must fall through to full activation
when update.sh-only paths changed since the last RECORDED update — the
recovery the deploy-staleness alert advertises. These tests run the exact
shipped bash function against a throwaway git repo + sqlite update_history.
"""

from __future__ import annotations

import re
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


def _extract_named_function(name: str) -> str:
    """A top-level shell function from the REAL update.sh, up to the next one."""
    text = UPDATE_SH.read_text()
    start = text.index(f"{name}() {{")
    next_function = re.search(r"\n[A-Za-z_][A-Za-z0-9_]*\(\) \{", text[start + 1 :])
    assert next_function, f"missing function boundary after {name}"
    return text[start : start + 1 + next_function.start()]


def _harness(root: Path, venv: Path) -> str:
    # The check reads with the history writer's interpreter selector, defined
    # once elsewhere in update.sh — supply that real definition beside it.
    return (
        "set -u\n"
        f'GENESIS_ROOT="{root}"\n'
        f'VENV_DIR="{venv}"\n'
        + _extract_named_function("_metadata_python")
        + "\n"
        + _extract_function()
        + "\n_tier2_pending_since_baseline\n"
    )


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
    result = subprocess.run(
        ["bash", "-c", _harness(root, venv)], capture_output=True, text=True, timeout=60
    )
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


def test_no_history_table_is_not_pending(tmp_path):
    """No table yet (first update before migrations) is ABSENT: shortcut as before."""
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "f").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "c1")
    sqlite3.connect(root / "data" / "genesis.db").execute("PRAGMA user_version = 1")
    assert _run_check(root, tmp_path / "missing-venv") == 1


def test_unreadable_history_is_pending(genesis_root):
    """A history that exists but cannot be read is NOT "no baseline".

    The function's contract is to fail toward the full run; reading an
    unreadable database as absent took the shortcut instead.
    """
    root, venv = genesis_root
    (root / "data" / "genesis.db").write_bytes(b"this is not a sqlite database" * 100)
    assert _run_check(root, venv) == 0


def test_no_interpreter_for_the_reader_is_pending(genesis_root, tmp_path):
    """No interpreter able to read the history: unknown, so fail toward the full run."""
    root, _venv = genesis_root
    _record_success(root, _git(root, "rev-parse", "--short", "HEAD"))
    bin_dir = tmp_path / "git-only-bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to("/usr/bin/git")
    result = subprocess.run(
        ["/bin/bash", "-c", _harness(root, tmp_path / "missing-venv")],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
