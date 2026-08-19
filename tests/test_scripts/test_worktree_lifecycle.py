"""Tests for scripts/worktree_lifecycle.py — the daily worktree reaper.

Regression coverage for the detached-HEAD blind spot: ``git worktree list
--porcelain`` emits a bare ``detached`` line (no ``branch``) for a detached
worktree, so the reaper used to default ``branch="unknown"`` and skip such
worktrees forever even when their HEAD commit is fully merged into ``main``.

These use REAL git repos in ``tmp_path`` (the house pattern from
``test_git_repair.py``); the reaper shells out to git directly, so there is no
mock seam. The load-bearing invariants under test:
  * ``_list_worktrees`` marks a detached worktree (``detached=True``, no ``branch``);
  * a detached HEAD at a MERGED commit is classified reapable, at an UNMERGED
    commit is kept (fail-safe);
  * the branch path is unchanged (merged branch still reaped);
  * ``main()`` reaps ONLY the merged worktrees and trashes recoverably;
  * a trashed detached worktree round-trips through ``_recover`` (re-added detached
    at its commit), not just a plain-directory move.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worktree_lifecycle.py"
_spec = importlib.util.spec_from_file_location("worktree_lifecycle", _SCRIPT)
wl = importlib.util.module_from_spec(_spec)
sys.modules["worktree_lifecycle"] = wl
_spec.loader.exec_module(wl)


# ─── fixtures / helpers ──────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _age_path(path: Path, days: float) -> None:
    """Backdate mtimes of a worktree (dir + top-2 levels) past the stale gate.

    Mirrors what ``_last_activity_time`` walks (dir + children, one level deep,
    skipping ``.git``), so the worktree reads as inactive.
    """
    old = time.time() - days * 86400
    os.utime(path, (old, old))
    for item in path.rglob("*"):
        if ".git" in item.parts:
            continue
        with contextlib.suppress(OSError):
            os.utime(item, (old, old))


@pytest.fixture
def reaper_repo(tmp_path: Path):
    """A real repo with detached + branch worktrees in known merge states.

    Returns an object with: ``repo`` (main tree), the commit shas ``c0``/``c1``
    (both on main) and ``c_side`` (NOT on main), and the worktree paths.
    """
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "s@s")
    _git(repo, "config", "user.name", "s")

    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "c0")
    c0 = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "c1")
    c1 = _git(repo, "rev-parse", "HEAD").strip()  # main tip; c0 is an ancestor

    # A commit that will NOT be in main's history (reachable only via a worktree).
    _git(repo, "branch", "sidebr", c1)
    _git(repo, "worktree", "add", "-q", str(tmp_path / "_sidewt"), "sidebr")
    (tmp_path / "_sidewt" / "s.txt").write_text("s\n")
    _git(tmp_path / "_sidewt", "add", "s.txt")
    _git(tmp_path / "_sidewt", "commit", "-q", "-m", "c-side")
    c_side = _git(tmp_path / "_sidewt", "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(tmp_path / "_sidewt"))
    _git(repo, "branch", "-D", "sidebr")  # c_side now only reachable via a detached HEAD

    # A real branch that is merged into main (points at the ancestor c0).
    _git(repo, "branch", "merged-br", c0)

    wt_det_merged = tmp_path / "wt_det_merged"
    wt_det_unmerged = tmp_path / "wt_det_unmerged"
    wt_branch_merged = tmp_path / "wt_branch_merged"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt_det_merged), c0)
    _git(repo, "worktree", "add", "-q", "--detach", str(wt_det_unmerged), c_side)
    _git(repo, "worktree", "add", "-q", str(wt_branch_merged), "merged-br")

    # Push all three past the 14-day inactivity gate.
    for p in (wt_det_merged, wt_det_unmerged, wt_branch_merged):
        _age_path(p, 20)

    return type(
        "ReaperRepo",
        (),
        {
            "repo": repo,
            "c0": c0,
            "c1": c1,
            "c_side": c_side,
            "wt_det_merged": wt_det_merged,
            "wt_det_unmerged": wt_det_unmerged,
            "wt_branch_merged": wt_branch_merged,
        },
    )()


def _wt_by_path(repo: Path, target: Path) -> dict:
    for wt in wl._list_worktrees(repo):
        if Path(wt["path"]) == target:
            return wt
    raise AssertionError(f"worktree not found: {target}")


# ─── _list_worktrees: detached marking ───────────────────────────────────────


def test_list_worktrees_marks_detached(reaper_repo):
    wt = _wt_by_path(reaper_repo.repo, reaper_repo.wt_det_merged)
    assert wt.get("detached") is True
    assert "branch" not in wt
    assert wt["head"] == reaper_repo.c0


def test_list_worktrees_branch_worktree_unmarked(reaper_repo):
    wt = _wt_by_path(reaper_repo.repo, reaper_repo.wt_branch_merged)
    assert wt.get("detached") is not True
    assert wt["branch"] == "merged-br"


# ─── _is_merged: detached evaluated by HEAD commit ───────────────────────────


def test_is_merged_detached_at_merged_commit(reaper_repo):
    wt = _wt_by_path(reaper_repo.repo, reaper_repo.wt_det_merged)
    assert wl._is_merged(wt["head"], reaper_repo.repo, is_branch=False) is True


def test_is_merged_detached_at_unmerged_commit(reaper_repo):
    wt = _wt_by_path(reaper_repo.repo, reaper_repo.wt_det_unmerged)
    assert wl._is_merged(wt["head"], reaper_repo.repo, is_branch=False) is False


def test_is_merged_branch_still_works(reaper_repo):
    # Regression guard: the branch path (Method 1 short-circuits, no gh) is unchanged.
    assert wl._is_merged("merged-br", reaper_repo.repo, is_branch=True) is True


# ─── main(): end-to-end wiring — reap only the merged, trash recoverably ──────


def _run_main(monkeypatch, repo: Path, trash: Path, *, argv=("worktree_lifecycle.py",)):
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    monkeypatch.setattr(sys, "argv", list(argv))
    return wl.main()


def test_main_reaps_detached_merged_only(reaper_repo, tmp_path, monkeypatch):
    trash = tmp_path / "trash"
    rc = _run_main(monkeypatch, reaper_repo.repo, trash)
    assert rc == 0

    # Detached-at-merged and branch-merged: reaped (moved out of place).
    assert not reaper_repo.wt_det_merged.exists()
    assert not reaper_repo.wt_branch_merged.exists()
    # Detached-at-unmerged: kept (fail-safe).
    assert reaper_repo.wt_det_unmerged.exists()

    # The detached entry landed in trash with detached metadata + real commit sha.
    import json

    metas = list(trash.glob("wt_det_merged-*/.trash_meta.json"))
    assert len(metas) == 1, f"expected one trashed detached worktree, got {metas}"
    meta = json.loads(metas[0].read_text())
    assert meta["detached"] is True
    assert meta["commit"] == reaper_repo.c0


# ─── _recover: detached round-trip (Part 3) ──────────────────────────────────


def test_recover_detached_roundtrip(reaper_repo, tmp_path, monkeypatch):
    trash = tmp_path / "trash"
    _run_main(monkeypatch, reaper_repo.repo, trash)
    assert not reaper_repo.wt_det_merged.exists()

    # Recover it — must come back as a DETACHED worktree at the original commit,
    # not a plain-directory move.
    ok = wl._recover("wt_det_merged", reaper_repo.repo)
    assert ok is True
    assert reaper_repo.wt_det_merged.exists()

    head = _git(reaper_repo.wt_det_merged, "rev-parse", "HEAD").strip()
    assert head == reaper_repo.c0
    # Detached HEAD: symbolic-ref for HEAD fails (not on a branch).
    detached = subprocess.run(
        ["git", "-C", str(reaper_repo.wt_det_merged), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
    )
    assert detached.returncode != 0, "recovered worktree should be detached, not on a branch"


def test_recover_legacy_branch_entry(reaper_repo, tmp_path, monkeypatch):
    """A PRE-FIX trash entry (no ``detached`` key, ``branch`` set) still round-trips.

    Locks the backward-compat guarantee for the ~dozens of legacy branch entries
    already on disk: ``_recover`` must default ``detached`` to False and take the
    ``git worktree add <path> <branch>`` path unchanged.
    """
    import json

    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)

    # Simulate the OLD reaper having trashed the branch worktree: move the dir out,
    # prune the registration, and write a LEGACY meta with no ``detached`` key.
    src = reaper_repo.wt_branch_merged
    entry = trash / "wt_branch_merged-20260101"
    subprocess.run(["mv", str(src), str(entry)], check=True)
    _git(reaper_repo.repo, "worktree", "prune")
    (entry / ".trash_meta.json").write_text(
        json.dumps(
            {
                "original_path": str(src),
                "branch": "merged-br",
                "commit": reaper_repo.c0,
                "trashed_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    ok = wl._recover("wt_branch_merged", reaper_repo.repo)
    assert ok is True
    assert src.exists()
    # Restored ON the branch (not detached) — the legacy path is unchanged.
    branch = _git(src, "symbolic-ref", "--short", "HEAD").strip()
    assert branch == "merged-br"
