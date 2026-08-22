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


# ─── detached reap predicate is ancestor-ONLY (Codex P1 findings B & C) ───────


def test_is_merged_detached_merge_commit_kept(reaper_repo):
    """A detached MERGE commit (both parents in main, unique tree, NOT an
    ancestor) must be KEPT. `git cherry` omits merges, so the old patch-id path
    read empty output as "merged" and would wrongly reap unique merge work.
    Ancestor-only detached detection keeps it. (Codex finding C.)
    """
    repo = reaper_repo.repo
    side_tree = _git(repo, "rev-parse", f"{reaper_repo.c_side}^{{tree}}").strip()
    merge = _git(
        repo,
        "commit-tree",
        side_tree,
        "-p",
        reaper_repo.c1,
        "-p",
        reaper_repo.c0,
        "-m",
        "unique-merge",
    ).strip()
    # Sanity: not an ancestor, and git cherry emits nothing (merge omitted).
    not_anc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", merge, "main"],
        capture_output=True,
    ).returncode
    assert not_anc != 0, "merge commit should not be an ancestor of main"
    assert _git(repo, "cherry", "main", merge).strip() == ""
    assert wl._is_merged(merge, repo, is_branch=False) is False


def test_is_merged_detached_patch_equal_kept(reaper_repo):
    """A detached commit patch-EQUAL to main but NOT an ancestor must be KEPT.
    The old patch-id path counted it merged and reaped it, but it is referenced
    only by the worktree HEAD → GC-fragile in the recovery window. Ancestor-only
    detached detection keeps it. (Codex finding B.)
    """
    repo = reaper_repo.repo
    c1_tree = _git(repo, "rev-parse", f"{reaper_repo.c1}^{{tree}}").strip()
    # Same tree as c1, parent c0, distinct message → distinct sha, patch-equal, not an ancestor.
    patch_equal = _git(
        repo, "commit-tree", c1_tree, "-p", reaper_repo.c0, "-m", "cherrypicked"
    ).strip()
    assert patch_equal != reaper_repo.c1
    # git cherry marks it patch-equal ('-'), i.e. zero unique '+' → old logic said merged.
    assert _git(repo, "cherry", "main", patch_equal).strip().startswith("-")
    assert wl._is_merged(patch_equal, repo, is_branch=False) is False


# ─── recovery preserves uncommitted tracked edits (Codex P1 finding A) ────────


def test_recover_restores_untracked_file(reaper_repo, tmp_path, monkeypatch):
    """Recovery restores UNTRACKED files that the fresh checkout would not recreate.

    This is the recovery contract's positive guarantee (copy-only-missing). Full
    dirty-state reconstruction — uncommitted tracked edits, deletions, mode-only
    changes, the staged split — is an intentional non-goal (see _recover docstring),
    since the reaper only trashes worktrees already merged into main.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")

    wt = reaper_repo.wt_branch_merged
    (wt / "scratch.txt").write_text("untracked scratch\n")  # untracked, absent from the commit

    wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo)
    assert not wt.exists()

    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    assert (wt / "scratch.txt").read_text() == "untracked scratch\n", (
        "recovery dropped an untracked file that was in the trash"
    )


def test_recover_does_not_reapply_tracked_modification(reaper_repo, tmp_path, monkeypatch):
    """A trashed uncommitted edit to a TRACKED file is NOT reapplied on recovery.

    copy-only-missing leaves the checked-out (committed) content intact. This LOCKS
    the overlay->copy-only-missing revert: the old filecmp overlay would have
    overwritten the tracked file with the trashed modification, so this test fails
    on the pre-revert code and passes now. (Codex 444/456 non-goal, by design.)
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")

    wt = reaper_repo.wt_branch_merged
    committed = (wt / "a.txt").read_text()  # tracked, committed at c0
    (wt / "a.txt").write_text("DIRTY EDIT\n")  # uncommitted tracked modification

    wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo)
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    assert (wt / "a.txt").read_text() == committed, (
        "recovery reapplied a trashed tracked modification (overlay behavior)"
    )


def test_skip_locked_worktree(reaper_repo, tmp_path, monkeypatch):
    """A locked (git worktree lock) worktree is never reaped, even when merged."""
    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "_repo_root", lambda: reaper_repo.repo)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py"])

    # merged branch worktree that would otherwise be reaped → lock it
    _git(
        reaper_repo.repo,
        "worktree",
        "lock",
        str(reaper_repo.wt_branch_merged),
        "--reason",
        "protected",
    )

    # sanity: the parser flags it locked
    assert _wt_by_path(reaper_repo.repo, reaper_repo.wt_branch_merged).get("locked") is True

    assert wl.main() == 0
    assert reaper_repo.wt_branch_merged.exists(), "locked worktree must not be reaped"


def test_skip_in_progress_worktree(reaper_repo, tmp_path, monkeypatch):
    """A worktree with a paused Git operation (MERGE_HEAD) is never reaped."""
    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "_repo_root", lambda: reaper_repo.repo)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py"])

    wt = reaper_repo.wt_det_merged  # detached at a merged commit → would be reaped
    # Simulate an in-progress merge: write MERGE_HEAD into the worktree's admin dir.
    admin = _git(wt, "rev-parse", "--absolute-git-dir").strip()
    (Path(admin) / "MERGE_HEAD").write_text(_git(wt, "rev-parse", "HEAD"))
    assert wl._has_in_progress_op(str(wt)) is True

    assert wl.main() == 0
    assert wt.exists(), "worktree with an in-progress git op must not be reaped"


# ─── final class-closing round: symlink-safe recovery + fail-closed + nested ──


def test_recover_restores_untracked_symlink_as_symlink(reaper_repo, tmp_path, monkeypatch):
    """An untracked symlink is restored AS a symlink, not materialized/dropped."""
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")

    wt = reaper_repo.wt_branch_merged
    (wt / "lnk").symlink_to("some/relative/target")  # untracked (dangling) symlink

    wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo)
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    restored = wt / "lnk"
    assert restored.is_symlink(), "untracked symlink was not restored as a symlink"
    assert os.readlink(str(restored)) == "some/relative/target"


def test_recover_does_not_write_through_dangling_dest_symlink(tmp_path, monkeypatch):
    """Recovery must NEVER write outside the worktree via a checked-out dest symlink.

    Committed tree has a dangling symlink `esc` -> OUTSIDE; the dirty worktree
    replaced it with a regular file (so the trash holds a regular `esc`). On
    recovery, `git worktree add` restores the committed dangling symlink; the copy
    loop must NOT write the trashed regular file through it to OUTSIDE. (Codex 512.)
    """
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "s@s")
    _git(repo, "config", "user.name", "s")
    outside = tmp_path / "OUTSIDE.txt"  # must never be created
    os.symlink(str(outside), str(repo / "esc"))  # committed symlink -> outside
    _git(repo, "add", "esc")
    _git(repo, "commit", "-qm", "add symlink")
    _git(repo, "branch", "wbr", "HEAD")

    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "wbr")
    (wt / "esc").unlink()
    (wt / "esc").write_text("dirty payload")  # dirty regular file replacing the symlink

    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    wtdict = next(w for w in wl._list_worktrees(repo) if Path(w["path"]) == wt)
    wl._trash_worktree(wtdict, repo)

    assert wl._recover("wt", repo) is True
    assert not outside.exists(), (
        "recovery wrote through a dangling dest symlink OUTSIDE the worktree"
    )


def test_has_in_progress_op_fails_closed(tmp_path):
    """When git state can't be resolved (non-repo / broken .git), fail CLOSED (True)."""
    d = tmp_path / "notgit"
    d.mkdir()
    assert wl._has_in_progress_op(str(d)) is True


def test_skip_worktree_containing_nested(reaper_repo, tmp_path, monkeypatch):
    """A worktree that CONTAINS another linked worktree is never reaped."""
    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "_repo_root", lambda: reaper_repo.repo)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py"])

    parent = reaper_repo.wt_branch_merged  # merged → would be reaped
    nested = parent / "nested_wt"
    # nested at an UNMERGED commit so it is never independently reaped (deterministic)
    _git(reaper_repo.repo, "worktree", "add", "-q", "--detach", str(nested), reaper_repo.c_side)
    _age_path(parent, 20)
    # _age_path skips '.git', but the nested worktree's gitdir pointer file
    # (nested_wt/.git) stays fresh and keeps the PARENT reading as active — which
    # would mask the guard (the activity check, not the nested guard, would protect
    # the parent). Backdate the nested worktree fully so ONLY the nested guard can
    # keep the parent alive → the test REDs if the guard is removed.
    old = time.time() - 20 * 86400
    for p in nested.rglob("*"):
        with contextlib.suppress(OSError):
            os.utime(p, (old, old), follow_symlinks=False)
    os.utime(nested / ".git", (old, old))

    assert wl.main() == 0
    assert parent.exists(), "worktree containing a nested worktree must not be reaped"
