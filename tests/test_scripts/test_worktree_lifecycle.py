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


# Every module path the reaper WRITES to. Each is redirected below; the guard
# test at the bottom fails if a new one is added without being listed here.
_WRITABLE_PATH_CONSTANTS = ("TRASH_DIR", "LOG_DIR", "TOMBSTONE_INDEX", "BOARD_CACHE")


@pytest.fixture(autouse=True)
def _isolate_write_targets(tmp_path, monkeypatch):
    """Never let a test write into the operator's real ~/.genesis.

    Autouse and module-wide on purpose. This leak has happened TWICE — first the
    tombstone index, then the board cache — and both times it was silent: a write
    to a file nobody was watching, from tests that call helpers directly rather
    than going through ``_run_main``. Redirecting by NAME here, plus the guard
    test below, is what makes a third instance impossible rather than unlikely.
    """
    for name in _WRITABLE_PATH_CONSTANTS:
        monkeypatch.setattr(wl, name, tmp_path / f"isolated-{name.lower()}")


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
    # TOMBSTONE_INDEX defaults to a path under the real ~/.genesis. Without this
    # redirect every reaper test appends rows describing tmp_path worktrees to the
    # operator's live index — measured, 20 junk rows from one run.
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", trash / "tombstones.jsonl")
    monkeypatch.setattr(sys, "argv", list(argv))
    return wl.main()


def _tombstones(trash: Path) -> list[dict]:
    """Rows the run appended to the (redirected) tombstone index."""
    import json

    f = trash / "tombstones.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def test_main_archives_both_lanes_and_deletes_nothing(reaper_repo, tmp_path, monkeypatch):
    """Every reaped worktree becomes a recoverable archive. NOTHING is deleted.

    All three fixtures are 20 days idle, so merged and unmerged alike are past
    their thresholds. The point of this test is that the two lanes differ only in
    the LABEL they carry, never in whether the work survives: a merged worktree
    is a duplicate of main and an unmerged one may be the only copy, but the
    reaper is not the thing that decides either is expendable.
    """
    trash = tmp_path / "trash"
    rc = _run_main(monkeypatch, reaper_repo.repo, trash)
    assert rc == 0

    # All three left their working paths...
    for wt in (reaper_repo.wt_det_merged,
               reaper_repo.wt_branch_merged,
               reaper_repo.wt_det_unmerged):
        assert not wt.exists(), f"{wt.name} should have been reaped"

    # ...and all three are recoverable archives, not holes.
    archives = sorted(a.name for a in trash.glob("*.tar.gz"))
    assert len(archives) == 3, f"expected 3 archives, got {archives}"
    for stem in ("wt_det_merged", "wt_branch_merged", "wt_det_unmerged"):
        assert any(a.startswith(stem) for a in archives), f"{stem} missing from {archives}"

    # The branch of a MERGED worktree is left alone. An earlier revision deleted
    # it; a branch ref is the cheapest handle onto the commits a session made.
    assert _git(reaper_repo.repo, "branch", "--list", "merged-br").strip() != ""


def test_lane_is_recorded_but_changes_no_outcome(reaper_repo, tmp_path, monkeypatch):
    """Lane survives as provenance on the metadata, distinguishing the two cases."""
    import json

    trash = tmp_path / "trash"
    _run_main(monkeypatch, reaper_repo.repo, trash)

    metas = [json.loads(f.read_text()) for f in trash.glob("*.meta.json")]
    assert metas, f"no sidecar metadata written: {list(trash.iterdir())}"
    got = {m["original_path"].rsplit("/", 1)[-1]: m["lane"] for m in metas}
    assert got["wt_det_unmerged"] == "unmerged"
    assert got["wt_det_merged"] == "merged"
    assert got["wt_branch_merged"] == "merged"


def test_tombstone_index_records_every_reap(reaper_repo, tmp_path, monkeypatch):
    """The index is what makes the trash greppable without unpacking archives.

    It must carry the facts that die with the worktree: which branch, which
    commit, which lane, and the commits that exist nowhere but here.
    """
    trash = tmp_path / "trash"
    _run_main(monkeypatch, reaper_repo.repo, trash)

    rows = _tombstones(trash)
    assert len(rows) == 3, f"expected one tombstone per reap, got {len(rows)}"

    by_name = {r["original_path"].rsplit("/", 1)[-1]: r for r in rows}
    unmerged = by_name["wt_det_unmerged"]
    assert unmerged["lane"] == "unmerged"
    assert unmerged["commit"] == reaper_repo.c_side
    assert unmerged["archive"].endswith(".tar.gz")
    # The unmerged detached commit is not in main, so it is exactly the work a
    # tombstone exists to name.
    assert any(reaper_repo.c_side[:7] in c for c in unmerged["unique_commits"]), \
        f"unique commits should name c_side, got {unmerged['unique_commits']}"

    merged = by_name["wt_branch_merged"]
    assert merged["lane"] == "merged"
    assert merged["unique_commits"] == []


def test_archive_is_verified_before_the_directory_goes(reaper_repo, tmp_path, monkeypatch):
    """A failed compression keeps the uncompressed directory rather than losing it.

    This is the invariant that makes compression safe to add at all: it is an
    optimisation, and it must never be the reason a recovery is impossible.
    """
    trash = tmp_path / "trash"

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(wl.tarfile, "open", _boom)
    _run_main(monkeypatch, reaper_repo.repo, trash)

    assert not list(trash.glob("*.tar.gz")), "no archive should survive a failed write"
    dirs = [d for d in trash.iterdir() if d.is_dir() and d.name != "logs"]
    assert len(dirs) == 3, f"all three must remain as directories, got {dirs}"
    for d in dirs:
        assert (d / ".trash_meta.json").exists()


# ─── _recover: detached round-trip (Part 3) ──────────────────────────────────


def test_recover_detached_roundtrip(reaper_repo, tmp_path, monkeypatch):
    """Detached round-trip, now exercised on the UNMERGED worktree.

    Retargeted deliberately: the merged detached worktree no longer reaches the
    trash at all (it is deleted outright), so the unmerged one is the only
    detached entry a recovery can be tested against — and it is also the case
    that actually matters, since its commit is reachable from nowhere else.
    """
    trash = tmp_path / "trash"
    _run_main(monkeypatch, reaper_repo.repo, trash)
    assert not reaper_repo.wt_det_unmerged.exists()

    # Recover it — must come back as a DETACHED worktree at the original commit,
    # not a plain-directory move.
    ok = wl._recover("wt_det_unmerged", reaper_repo.repo)
    assert ok is True
    assert reaper_repo.wt_det_unmerged.exists()

    head = _git(reaper_repo.wt_det_unmerged, "rev-parse", "HEAD").strip()
    assert head == reaper_repo.c_side
    # Detached HEAD: symbolic-ref for HEAD fails (not on a branch).
    detached = subprocess.run(
        ["git", "-C", str(reaper_repo.wt_det_unmerged), "symbolic-ref", "-q", "HEAD"],
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


# ─── the guard: no test may write into the real ~/.genesis ───────────────────


def test_every_writable_path_is_redirected_during_tests():
    """Enumerate the module's Path constants; none may point at the real store.

    This is the CLASS fix for a leak that shipped twice. It runs under the
    autouse fixture, so it sees the post-redirect state: a newly added constant
    that nobody added to _WRITABLE_PATH_CONSTANTS still points into the real
    ~/.genesis and fails here, instead of silently polluting an operator's data.
    """
    real = Path.home() / ".genesis"
    leaked = sorted(
        name
        for name, val in vars(wl).items()
        if name.isupper()
        and isinstance(val, Path)
        and (val == real or real in val.parents)
    )
    assert not leaked, (
        f"these module paths still point inside {real} during tests: {leaked}. "
        f"Add them to _WRITABLE_PATH_CONSTANTS if the reaper writes to them."
    )


# ─── the gaps a green suite left open ────────────────────────────────────────
#
# Every one of these covers a defect that a full test run did NOT catch. They are
# grouped because they share a root cause: inserting a COMPRESS step between the
# worktree and its resting place blinded guards written against the pre-compression
# name and shape, and a verify-before-delete that SAMPLED the artifact rather than
# reading it whole was never a verification at all.


def test_a_truncated_archive_is_rejected_and_the_source_survives(
    reaper_repo, tmp_path, monkeypatch,
):
    """B1: verification must read the WHOLE archive, not its first header.

    `tarfile.next()` reads one member header and stops, so gzip's trailing
    CRC32/ISIZE check — the only proof the stream is complete — is never reached.
    MEASURED: a 61-member archive truncated to 50% passed that check while a full
    walk raised EOFError. The source directory was removed immediately after.

    The assertion is the OUTCOME, not the mechanism: whatever the failure mode, a
    bad archive must never be the last copy.
    """
    real_open = wl.tarfile.open

    def truncating_open(name=None, mode="r", **kw):
        tf = real_open(name, mode, **kw)
        if "w" in str(mode):
            orig_close = tf.close

            def close_then_truncate():
                orig_close()
                f = Path(str(name))
                data = f.read_bytes()
                f.write_bytes(data[: len(data) // 2])

            tf.close = close_then_truncate
        return tf

    monkeypatch.setattr(wl.tarfile, "open", truncating_open)

    trash = tmp_path / "trash"
    _run_main(monkeypatch, reaper_repo.repo, trash)

    assert not list(trash.glob("*.tar.gz")), "a corrupt archive must not be kept"
    dirs = [d for d in trash.iterdir() if d.is_dir() and d.name != "logs"]
    assert dirs, "the uncompressed directory must survive a failed archive"
    for d in dirs:
        assert (d / ".trash_meta.json").exists(), "and it must still be recoverable"


def test_a_second_reap_of_the_same_basename_does_not_overwrite_an_archive(
    reaper_repo, tmp_path, monkeypatch,
):
    """B2: the collision guard must see ARCHIVED entries, not just directories.

    `_compress_entry` removes the directory, so `trash_path.exists()` is False for
    every already-archived entry. MEASURED: the loop re-picked the same name and
    `tarfile.open(..., "w:gz")` truncated the existing tarball — a silent,
    irreversible loss inside a module whose contract is that it deletes nothing.

    Two worktrees deliberately share a BASENAME while living under different
    parents, which is the shape that makes this reachable.
    """
    repo = reaper_repo.repo
    trash = tmp_path / "trash"
    wl_trash = trash

    first = tmp_path / "alpha" / "dup"
    second = tmp_path / "beta" / "dup"
    for i, (path, branch) in enumerate(((first, "dup-a"), (second, "dup-b"))):
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "branch", branch, reaper_repo.c0)  # merged: ancestor of main
        _git(repo, "worktree", "add", "-q", str(path), branch)
        (path / f"marker{i}.txt").write_text(f"worktree {i}")
        _age_path(path, 20)

    monkeypatch.setattr(wl, "TRASH_DIR", wl_trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", trash / "tomb.jsonl")
    wl_trash.mkdir(parents=True, exist_ok=True)

    worktrees = wl._list_worktrees(repo)
    for path in (first, second):
        wt = next(w for w in worktrees if Path(w["path"]) == path)
        cls = wl._classify(wt, worktrees, repo)
        wl._trash_worktree(cls, repo, lane="merged", merge_method=cls["merge_method"])

    archives = sorted(trash.glob("dup-*.tar.gz"))
    assert len(archives) == 2, (
        f"each reap needs its own archive; got {[a.name for a in archives]}"
    )
    # And the first one still holds ITS content, not the second's.
    import tarfile as _tf

    names = set()
    for a in archives:
        with _tf.open(a, "r:gz") as fh:
            names |= {Path(m.name).name for m in fh.getmembers()}
    assert {"marker0.txt", "marker1.txt"} <= names, (
        f"both worktrees' content must survive; archive holds {sorted(names)}"
    )


def test_an_archive_with_an_absolute_symlink_round_trips(
    reaper_repo, tmp_path, monkeypatch,
):
    """B3: recovery must survive our own `secrets.env -> /abs/path` convention.

    `extractall(filter="data")` raises AbsoluteLinkError on the first absolute
    link and ABORTS PARTWAY, leaving a directory that looks restored and is not.
    MEASURED 2026-09-10: 3 of the 48 worktrees due for archiving carry exactly
    that link, so this is a live path, not a hypothetical one.
    """
    wt = reaper_repo.wt_branch_merged
    link = wt / "secrets.env"
    # Any ABSOLUTE target reproduces this: the filter refuses the link by its
    # shape, never by what it points at, and the target need not exist. Kept
    # install-generic on purpose — a real home path in a fixture is a portability
    # hit and puts a username in the public repo for no test value.
    abs_target = "/opt/genesis-fixture/secrets.env"
    os.symlink(abs_target, link)
    (wt / "untracked-note.txt").write_text("keep me")
    _age_path(wt, 20)

    trash = tmp_path / "trash"
    _run_main(monkeypatch, reaper_repo.repo, trash)
    assert not wt.exists()
    assert list(trash.glob("wt_branch_merged-*.tar.gz")), "should have archived"

    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    assert wt.exists(), "recovery must restore the worktree"
    assert (wt / "untracked-note.txt").exists(), (
        "a partial extraction would drop members after the absolute link"
    )
    restored = wt / "secrets.env"
    assert restored.is_symlink(), "the link must come back AS a link, not a copy"
    assert os.readlink(restored) == abs_target


def test_a_non_utf8_diff_does_not_abort_the_run(reaper_repo, tmp_path, monkeypatch):
    """S1: UnicodeDecodeError is a ValueError, outside every except tuple.

    It propagated out of main(), so one worktree holding a latin-1 file stopped
    every worktree after it from being processed — and disk_hygiene.sh swallows
    the traceback into a single `|| echo` line, so nobody would see why.
    """
    wt = reaper_repo.wt_branch_merged
    f = wt / "latin.txt"
    f.write_bytes(b"caf\xe9 non-utf8 \xe9\xe8\xea\n")  # tracked + modified
    _git(wt, "add", "latin.txt")
    _git(wt, "commit", "-q", "-m", "add latin file")
    f.write_bytes(b"caf\xe9 CHANGED \xe9\xe8\xea\n")  # now dirty, non-UTF-8 diff
    _age_path(wt, 20)

    trash = tmp_path / "trash"
    rc = _run_main(monkeypatch, reaper_repo.repo, trash)  # must not raise
    assert rc == 0

    # And the other worktrees were still processed — the real damage was the
    # silent truncation of the run, not the one failed patch.
    assert not reaper_repo.wt_det_unmerged.exists(), (
        "worktrees after the failing one must still be reaped"
    )


def test_dry_run_writes_nothing_at_all(reaper_repo, tmp_path, monkeypatch):
    """S5: --dry-run's entire contract is that it changes nothing on disk."""
    trash = tmp_path / "trash"
    cache = tmp_path / "board.json"
    monkeypatch.setattr(wl, "BOARD_CACHE", cache)
    _run_main(monkeypatch, reaper_repo.repo, trash,
              argv=("worktree_lifecycle.py", "--dry-run"))

    assert not cache.exists(), "--dry-run must not publish the board cache"
    assert reaper_repo.wt_branch_merged.exists(), "--dry-run must not reap"
    assert not list(trash.glob("*.tar.gz"))


def test_a_worktree_that_becomes_active_mid_run_is_not_reaped(
    reaper_repo, tmp_path, monkeypatch,
):
    """S2: liveness must be re-checked at ACT time, not only at classify time.

    Classification now happens for every worktree up front (measured 19-41s over
    191), and archiving adds seconds each, so the window between "nothing is using
    this" and the move is minutes. Someone opening an old worktree during the scan
    is precisely what the process check exists to protect.
    """
    calls = {"n": 0}
    target = str(reaper_repo.wt_branch_merged)
    real = wl._find_processes_in_dir

    def busy_on_second_look(path):
        if path == target:
            calls["n"] += 1
            return [] if calls["n"] == 1 else [999999]  # idle at classify, busy at reap
        return real(path)

    monkeypatch.setattr(wl, "_find_processes_in_dir", busy_on_second_look)
    trash = tmp_path / "trash"
    _run_main(monkeypatch, reaper_repo.repo, trash)

    assert calls["n"] >= 2, "the reap path must re-check liveness independently"
    assert reaper_repo.wt_branch_merged.exists(), (
        "a worktree that became busy after classification must be left alone"
    )
    assert not list(trash.glob("wt_branch_merged-*")), "and nothing of it stored"
