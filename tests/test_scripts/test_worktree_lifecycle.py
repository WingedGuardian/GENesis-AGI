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
import errno
import importlib.util
import json
import os
import shutil
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
    """Backdate every entry in a worktree past the stale gate.

    RECURSIVE, and `follow_symlinks=False`. Both matter, and the second was a
    silent hole: `os.utime` FOLLOWS a symlink by default, so a link with an
    absolute or dangling target raised OSError, got swallowed by the suppress,
    and kept its original mtime while this helper reported success. The worktree
    then read as ACTIVE through a channel the old depth-limited activity walk
    never sampled, so nothing noticed until `_last_activity_time` started
    consulting git for edits at any depth.

    The docstring also used to say "dir + top-2 levels" and mirror the walk's
    sampling. That description was already wrong -- the loop below is `rglob` --
    and pinning a test helper to the shape of the thing under test is how a
    fixture stops being able to express the case that breaks it.
    """
    old = time.time() - days * 86400
    os.utime(path, (old, old))
    for item in path.rglob("*"):
        if ".git" in item.parts:
            continue
        with contextlib.suppress(OSError, NotImplementedError):
            os.utime(item, (old, old), follow_symlinks=False)


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

    This is one half of the recovery contract (copy-only-missing). The other half,
    uncommitted TRACKED changes, travels as the saved `.dirty.patch` and is
    reapplied with `git apply --3way` (see `_restore_from_dir` and the tests
    below). Only the staged/unstaged split is not reconstructed.
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


def _drop_registration(repo: Path, wt: Path) -> None:
    """Remove an archived worktree's registration, as an old archive or a repo-wide
    prune would have. Recovery then cannot reattach and must rebuild from patches."""
    _git(repo, "worktree", "unlock", str(wt))
    _git(repo, "worktree", "prune")


def _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit: str = "DIRTY EDIT\n") -> Path:
    """Archive `wt_branch_merged` holding an uncommitted edit to tracked `a.txt`."""
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text(edit)
    assert wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo) is True
    assert not wt.exists()
    return wt


def test_recover_reapplies_tracked_modification(reaper_repo, tmp_path, monkeypatch):
    """A trashed uncommitted edit to a TRACKED file IS reapplied on recovery.

    This test used to pin the opposite, on the premise that "the reaper only
    trashes worktrees already merged into main". That premise was false. The
    unmerged lane exists, and a branch with no commits of its own passed the
    merge test vacuously. So an uncommitted edit archived with its `.dirty.patch`
    came back as the committed content, and the edit survived only as a file
    nobody applied. A staged NEW file rides in the same patch. It is asserted too,
    because the apply runs before the untracked copy for exactly that file.
    """
    wt = reaper_repo.wt_branch_merged
    (wt / "staged_new.txt").write_text("staged\n")
    _git(wt, "add", "staged_new.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n", (
        "recovery did not reapply the archived uncommitted edit to a tracked file"
    )
    assert (wt / "staged_new.txt").read_text() == "staged\n"
    # The split comes back exactly: the staged file staged, the edit unstaged.
    assert _git(wt, "diff", "--cached", "--name-only").split() == ["staged_new.txt"]
    assert _git(wt, "diff", "--name-only").split() == ["a.txt"]


def test_recover_leaves_no_stray_patch_file(reaper_repo, tmp_path, monkeypatch):
    """A patch that applied has done its job, so it must not linger as an untracked file.

    The untracked-file copy used to carry `.dirty.patch` into the recovered tree
    like any other file. That is noise in `git status`, and a later reap would
    then have to save its own patch as `.dirty.patch.archived-1` beside a stale one.
    """
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n", "precondition: the patch applied"
    assert not os.path.lexists(wt / ".dirty.patch"), (
        "the reaper's own patch was copied into the recovered worktree after it applied"
    )
    assert ".dirty.patch" not in _git(wt, "status", "--porcelain", "-uall")


def test_recover_keeps_a_conflicting_patch_and_says_so_loudly(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """The branch moved on after archiving and now CONFLICTS with the saved edit.

    `git apply --3way` is not atomic. It leaves conflict markers and still applies
    the patch's other hunks. So a failure has to be rolled back, the patch kept
    where a human will look, and the message has to say the edits were NOT
    reapplied. A quiet success over a half-applied tree would be worse than
    the old behaviour.
    """
    repo = reaper_repo.repo
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)

    # Move the archived branch to a commit whose a.txt conflicts with the edit.
    # `update-ref`, because `branch -f` refuses a branch the (locked, archived)
    # registration still has checked out.
    cw = tmp_path / "conflict_src"
    _git(repo, "worktree", "add", "-q", "--detach", str(cw), reaper_repo.c0)
    (cw / "a.txt").write_text("CONFLICT\n")
    _git(cw, "commit", "-qam", "conflicting change")
    conflict = _git(cw, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(cw))
    _git(repo, "update-ref", "refs/heads/merged-br", conflict)

    _drop_registration(repo, reaper_repo.wt_branch_merged)  # force the patch fallback
    assert wl._recover("wt_branch_merged", repo) is True
    err = capsys.readouterr().err

    kept = wt / ".dirty.patch"
    assert "UNCOMMITTED EDITS WERE NOT REAPPLIED" in err, err
    assert str(kept) in err, f"the message must say where the patch is:\n{err}"
    assert kept.is_file(), "the unapplied patch must be kept in the worktree"
    assert "+DIRTY EDIT" in kept.read_text(), "and it must be the saved edit"
    assert (wt / "a.txt").read_text() == "CONFLICT\n", (
        "a failed apply was not rolled back: the tree holds a partial application"
    )
    assert _git(wt, "status", "--porcelain", "-uall").splitlines() == ["?? .dirty.patch"]


def test_recover_applies_the_reapers_patch_not_the_worktrees_own(
    reaper_repo, tmp_path, monkeypatch,
):
    """A worktree may own a `.dirty.patch`, and ours is then `.dirty.patch.archived-1`.

    Recovery must apply the reaper's file (named by `patch_file` in the metadata)
    and restore the worktree's own file untouched, as the untracked file it was.
    """
    wt = reaper_repo.wt_branch_merged
    (wt / ".dirty.patch").write_text("MY OWN FILE\n")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n", (
        "recovery did not apply the reaper's patch saved under the fallback name"
    )
    assert (wt / ".dirty.patch").read_text() == "MY OWN FILE\n"
    assert not os.path.lexists(wt / ".dirty.patch.archived-1")


def test_recover_reapplies_from_an_archive_without_patch_file(
    reaper_repo, tmp_path, monkeypatch,
):
    """Every archive written before `patch_file` existed must still get its edits back.

    MEASURED 2026-09-25 on a live install: 0 of 204 archive metadata files carry
    `patch_file`. Those archives are the ones waiting to be recovered, so the
    fallback is the path that matters most right now. A directory entry is used,
    by failing compression, so the metadata can be edited in place.
    """
    import json

    def _no_tar(*_a, **_k):
        raise OSError("compression disabled for this test")

    monkeypatch.setattr(wl.tarfile, "open", _no_tar)
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)

    (entry,) = [d for d in (tmp_path / "trash").iterdir()
                if d.is_dir() and d.name.startswith("wt_branch_merged")]
    meta_path = entry / ".trash_meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta.get("patch_file") == ".dirty.patch", "precondition: new archives record it"
    for legacy_absent in ("patch_file", "index_patch_file", "patch_format"):
        del meta[legacy_absent]  # an archive from before the reaper recorded names
    meta_path.write_text(json.dumps(meta))
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n", (
        "an archive without `patch_file` did not get its uncommitted edit back"
    )


def _a_valid_patch_creating_u(wt: Path) -> str:
    """A real patch that CREATES `u.txt` in ``wt``: one that WOULD apply if chosen."""
    (wt / "u.txt").write_text("CREATED BY THE USERS OWN PATCH\n")
    _git(wt, "add", "-N", "u.txt")
    body = _git(wt, "diff", "u.txt")
    _git(wt, "rm", "-q", "--cached", "u.txt")
    (wt / "u.txt").unlink()
    assert "+CREATED BY THE USERS OWN PATCH" in body
    return body


def test_recover_applies_nothing_when_the_reapers_patch_write_failed(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """A failed patch write must be RECORDED, never guessed around.

    The worktree owns a VALID `.dirty.patch`, so ours goes to
    `.dirty.patch.archived-1`, and that write fails. Before the fix the metadata
    said `had_tracked_patch` True with no `patch_file`, recovery guessed the name,
    picked the user's file, applied it, and reported success. MEASURED by a
    verification pass with a simulated ENOSPC.
    """
    wt = reaper_repo.wt_branch_merged
    own = _a_valid_patch_creating_u(wt)
    (wt / ".dirty.patch").write_text(own)

    real_open = os.open

    def _enospc_for_ours(path, *a, **k):
        if str(path).endswith(".dirty.patch.archived-1"):
            raise OSError(28, "No space left on device")
        return real_open(path, *a, **k)

    monkeypatch.setattr(wl.os, "open", _enospc_for_ours)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    monkeypatch.setattr(wl.os, "open", real_open)
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    out = capsys.readouterr().out
    assert not (wt / "u.txt").exists(), "the user's own .dirty.patch was applied"
    assert "Reapplied" not in out, out
    assert (wt / ".dirty.patch").read_text() == own, "the user's file was not restored intact"


def test_recover_uses_patch_file_not_a_guess(reaper_repo, tmp_path, monkeypatch):
    """`patch_file` is authoritative; the numbered-name guess must not override it.

    The worktree owns `.dirty.patch.archived-5` (a valid patch) and no
    `.dirty.patch`, so ours is saved as `.dirty.patch`. The legacy guess would
    pick the user's numbered file.
    """
    wt = reaper_repo.wt_branch_merged
    own = _a_valid_patch_creating_u(wt)
    (wt / ".dirty.patch.archived-5").write_text(own)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n", "the reaper's patch was not applied"
    assert not (wt / "u.txt").exists(), "the user's numbered patch was applied instead"
    assert (wt / ".dirty.patch.archived-5").read_text() == own


def test_legacy_archive_picks_the_highest_numbered_patch(reaper_repo, tmp_path, monkeypatch):
    """An archive without `patch_file`: ours took the first free number, so the highest.

    The worktree owns `.dirty.patch` and `.dirty.patch.archived-1`, so ours is
    `.dirty.patch.archived-2`. Stripping `patch_file` forces the legacy fallback.
    """
    import json

    def _no_tar(*_a, **_k):
        raise OSError("compression disabled for this test")

    monkeypatch.setattr(wl.tarfile, "open", _no_tar)
    wt = reaper_repo.wt_branch_merged
    (wt / ".dirty.patch").write_text("MY OWN FILE\n")
    (wt / ".dirty.patch.archived-1").write_text("MY OTHER FILE\n")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)

    (entry,) = [d for d in (tmp_path / "trash").iterdir()
                if d.is_dir() and d.name.startswith("wt_branch_merged")]
    meta_path = entry / ".trash_meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta.get("patch_file") == ".dirty.patch.archived-2", "precondition"
    for legacy_absent in ("patch_file", "index_patch_file", "patch_format"):
        del meta[legacy_absent]  # an archive from before the reaper recorded names
    meta_path.write_text(json.dumps(meta))
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n", "the legacy guess picked the wrong file"
    assert (wt / ".dirty.patch").read_text() == "MY OWN FILE\n"
    assert (wt / ".dirty.patch.archived-1").read_text() == "MY OTHER FILE\n"


def _retry_commands(err: str) -> list[str]:
    lines = err.splitlines()
    start = next(i for i, ln in enumerate(lines) if "Retry by hand" in ln)
    out = []
    for ln in lines[start + 1:]:
        if not ln.startswith("    "):
            break
        out.append(ln.strip())
    return out


def test_the_printed_retry_command_survives_a_created_file(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """The retry line must not fail on a file the patch creates.

    After a rollback, the untracked copy restores the staged new file from the
    archive, and a bare `git apply` then refuses to create it. MEASURED: the old
    printed command failed with "does not exist in index". The retry may still
    CONFLICT on the moved file -- that is the genuine reason it failed -- but it
    must not trip on the created one.
    """
    repo = reaper_repo.repo
    wt = reaper_repo.wt_branch_merged
    (wt / "staged_new.txt").write_text("staged\n")
    _git(wt, "add", "staged_new.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)

    cw = tmp_path / "conflict_src"
    _git(repo, "worktree", "add", "-q", "--detach", str(cw), reaper_repo.c0)
    (cw / "a.txt").write_text("CONFLICT\n")
    _git(cw, "commit", "-qam", "conflicting change")
    conflict = _git(cw, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(cw))
    _git(repo, "update-ref", "refs/heads/merged-br", conflict)

    _drop_registration(repo, reaper_repo.wt_branch_merged)  # force the patch fallback
    assert wl._recover("wt_branch_merged", repo) is True
    err = capsys.readouterr().err
    assert (wt / "staged_new.txt").read_text() == "staged\n", "precondition: restored as untracked"
    cmds = _retry_commands(err)
    assert cmds[0].startswith("cd ") and "staged_new.txt.restored" in cmds[0], cmds
    assert "--exclude" not in err, err
    move, index_apply, unstaged_apply = cmds
    for cmd in (move, index_apply):
        rerun = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        assert rerun.returncode == 0, (cmd, rerun.stderr)
    # The staged file is back, staged, with its content; the restored copy is kept.
    assert _git(wt, "diff", "--cached", "--name-only").split() == ["staged_new.txt"]
    assert (wt / "staged_new.txt.restored").read_text() == "staged\n"
    # The unstaged edit still conflicts with the moved branch — the genuine reason
    # the recovery failed — and must not trip on the created file.
    rerun = subprocess.run(["bash", "-c", unstaged_apply], capture_output=True, text=True)
    assert "staged_new.txt" not in rerun.stderr, rerun.stderr


# ─── an "ancestor" verdict with no commits of its own is not merged work ──────


@pytest.mark.parametrize("which", ["wt_branch_merged", "wt_det_merged"])
def test_uncommitted_work_on_a_zero_commit_worktree_is_not_on_the_merged_clock(
    reaper_repo, tmp_path, monkeypatch, which,
):
    """A worktree whose tip is a main commit has merged NOTHING.

    `merge-base --is-ancestor` passes for it anyway, so it was classed MERGED
    and archived at 7 days. That is the short clock meant for work that is
    already in main, but here the uncommitted edits were the only copy. MEASURED
    on a live install: 12 of 204 archives are ancestor-verdict, and all 12 have
    their tip on main's first-parent line. 9 of those were dirty.
    Both fixtures qualify: `merged-br` sits at c0 with no commit of its own, and
    the detached one is checked out at c0.
    """
    repo = reaper_repo.repo
    wt = getattr(reaper_repo, which)
    (wt / "a.txt").write_text("uncommitted work\n")
    _age_path(wt, 10)  # past MERGED_STALE_DAYS, short of UNMERGED_STALE_DAYS

    worktrees = wl._list_worktrees(repo)
    cls = wl._classify(_wt_by_path(repo, wt), worktrees, repo)
    assert cls["state"] == wl.STATE_AT_RISK, cls
    assert cls["merged"] is False and cls["merge_method"] == "", cls

    trash = tmp_path / "trash"
    assert _run_main(monkeypatch, repo, trash) == 0
    assert wt.exists(), (
        f"{which} holds only uncommitted work and was archived on the merged clock"
    )
    assert not list(trash.glob(f"{which}-*"))


def test_the_merged_clock_still_applies_to_real_merged_work(reaper_repo, tmp_path, monkeypatch):
    """The control, and it has to MOVE: two worktrees at 10 days still go.

    * a branch with a real commit merged into main through a merge commit. It is
      reaped at the merged threshold even though it is dirty, because its tip sits
      on a second parent and not on main's first-parent line;
    * a CLEAN zero-commit worktree. It holds nothing main lacks, so it stays on
      the merged clock and stays out of the at-risk alert set.
    Either assertion fails if the lane test is widened to every ancestor verdict,
    or to every zero-commit worktree regardless of whether it is dirty.
    """
    import json

    repo = reaper_repo.repo
    real = tmp_path / "wt_real_merged"
    _git(repo, "worktree", "add", "-q", "-b", "real-br", str(real), "main")
    (real / "r.txt").write_text("real work\n")
    _git(real, "add", "r.txt")
    _git(real, "commit", "-qm", "real work")
    _git(repo, "merge", "--no-ff", "-q", "-m", "merge real-br", "real-br")
    (real / "a.txt").write_text("leftover uncommitted edit\n")

    for p in (real, reaper_repo.wt_branch_merged, reaper_repo.wt_det_merged,
              reaper_repo.wt_det_unmerged):
        _age_path(p, 10)

    trash = tmp_path / "trash"
    assert _run_main(monkeypatch, repo, trash) == 0

    assert not real.exists(), "merged work past 7 days must still be reaped"
    assert not reaper_repo.wt_branch_merged.exists(), (
        "a CLEAN zero-commit worktree has nothing to lose and stays on the merged clock"
    )
    assert reaper_repo.wt_det_unmerged.exists(), "the unmerged control must be kept"
    lanes = {json.loads(f.read_text())["original_path"].rsplit("/", 1)[-1]:
             json.loads(f.read_text())["lane"] for f in trash.glob("*.meta.json")}
    assert lanes.get("wt_real_merged") == "merged", lanes
    assert lanes.get("wt_branch_merged") == "merged", lanes


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


def test_no_network_never_publishes_the_shared_board(reaper_repo, tmp_path, monkeypatch):
    """A degraded classification must not become the board other surfaces read.

    --no-network skips the gh PR check, which can only demote a merged branch to
    "unmerged". Harmless for the caller who asked for it; corrosive as SHARED
    state, because neither the session-start block nor the dashboard can tell a
    degraded board from a current one. MEASURED on the real tree: the degraded
    run reported 42 at-risk where the complete one reported 11.
    """
    cache = tmp_path / "board.json"
    monkeypatch.setattr(wl, "BOARD_CACHE", cache)
    _run_main(
        monkeypatch, reaper_repo.repo, tmp_path / "trash",
        argv=("worktree_lifecycle.py", "--report-json", "--no-network"),
    )
    assert not cache.exists(), "--no-network must leave the shared board alone"


def test_a_network_complete_report_does_publish(reaper_repo, tmp_path, monkeypatch):
    """The negative control — otherwise the test above passes on a broken writer."""
    cache = tmp_path / "board.json"
    monkeypatch.setattr(wl, "BOARD_CACHE", cache)
    _run_main(
        monkeypatch, reaper_repo.repo, tmp_path / "trash",
        argv=("worktree_lifecycle.py", "--report-json"),
    )
    assert cache.exists(), "a complete classification SHOULD be published"


def test_archiving_leaves_the_registration_and_recovery_clears_it(
    reaper_repo, tmp_path, monkeypatch, capsys
):
    """The whole point of this split, in one test.

    Archiving no longer prunes, because pruning drops the per-worktree HEAD and
    for a detached worktree that ref is the only thing keeping its commits
    reachable — unsafe until the archive carries its own copy of the history.
    So the registration is expected to SURVIVE the reap.

    And that is exactly what breaks recovery if nothing clears it: `git worktree
    add` refuses a path that is still registered ("missing but already
    registered"), and `_recover` would fall through to its plain-directory
    fallback, producing a tree that is not a git worktree at all — the failure
    the fallback exists to avoid rather than to cause.

    Both halves are asserted here, because either alone passes for the wrong
    reason: the registration surviving is only correct if recovery still returns
    a REAL worktree, and recovery working proves nothing if the registration was
    silently pruned after all.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")

    wt = reaper_repo.wt_branch_merged
    wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo)

    # 1. The registration SURVIVES the archive. `git worktree list` still names
    #    the path even though the directory is now a tarball.
    listed = _git(reaper_repo.repo, "worktree", "list", "--porcelain")
    assert str(wt) in listed, (
        "the registration was pruned at archive time — the commits an archive "
        "refers to would be collectable"
    )

    # 2. Recovery clears it and rebuilds a REAL worktree, not the fallback.
    #
    # Keyed on the RECOVERY PATH TAKEN, not on whether `git status` works
    # afterwards. That distinction was found by mutation: with nothing pruning
    # anywhere, the admin directory also survives, so the plain-directory
    # fallback leaves a `.git` file still pointing at a valid admin dir and
    # `git status` succeeds inside it. A status check therefore passes for BOTH
    # outcomes and discriminates nothing. The message the function prints when
    # it falls back is the only thing that actually differs.
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    out = capsys.readouterr().out
    assert "not git worktree" not in out, (
        "recovery fell through to its plain-directory fallback — `git worktree "
        "add` refused the still-registered path, so nothing cleared it"
    )
    assert (wt / ".git").exists(), "recovery produced no .git at all"


def test_a_dangling_internal_name_symlink_is_not_written_through(
    reaper_repo, tmp_path, monkeypatch,
):
    """A DANGLING symlink is a directory entry that `Path.exists()` calls absent.

    `.dirty.patch` is not a reserved name, so the archive path already avoided
    overwriting a real one. It resolved the collision with `Path.exists()`, which
    follows the link -- so a DANGLING `.dirty.patch` symlink read as "no
    collision", the canonical name was kept, and the `os.open(O_CREAT)` below
    FOLLOWED the link and created its target, which can sit anywhere on the
    filesystem. MEASURED before the fix, in a scratch tree:
        os.path.lexists -> True, Path.exists -> False
        os.open(..., O_CREAT|O_TRUNC) created the outside file
    For a module whose contract is that it deletes nothing, writing a file
    OUTSIDE the tree it was handed is the contract breaking in the other
    direction.
    """
    repo = reaper_repo.repo
    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", trash / "tomb.jsonl")
    trash.mkdir(parents=True, exist_ok=True)

    wt = reaper_repo.wt_branch_merged
    # Uncommitted tracked work, so a recovery patch is actually produced.
    (wt / "a.txt").write_text("locally modified\n")

    outside = tmp_path / "OUTSIDE_TARGET.txt"
    os.symlink(str(outside), str(wt / ".dirty.patch"))
    assert not outside.exists(), "precondition: the symlink target does not exist"
    _age_path(wt, 20)

    worktrees = wl._list_worktrees(repo)
    entry = next(w for w in worktrees if Path(w["path"]) == wt)
    cls = wl._classify(entry, worktrees, repo)
    wl._trash_worktree(cls, repo, lane="merged", merge_method=cls["merge_method"])

    assert not outside.exists(), (
        "the recovery patch was written THROUGH a dangling symlink and landed "
        f"outside the worktree at {outside}"
    )

    import tarfile as _tf

    # THE COLLISION CHECK ITSELF must have seen the dangling entry. Asserting
    # only "the outside file was not created" is BLIND: the O_NOFOLLOW guard at
    # the write closes that hole on its own, so the assertion above passes with
    # this check reverted. MEASURED by mutation -- the test stayed green until
    # this line existed. The two guards answer different questions and each needs
    # its own witness.
    # (asserted against the archive's members below, once it is opened)

    archives = sorted(trash.glob("*.tar.gz"))
    assert archives, "the worktree should still have been archived"
    with _tf.open(archives[0], "r:gz") as fh:
        members = {m.name.split("/", 1)[-1]: m for m in fh.getmembers()}
    assert ".dirty.patch" in members, "the user's own entry must survive in the archive"
    assert members[".dirty.patch"].issym(), "and it must still be their symlink"
    assert ".dirty.patch.archived-1" in members, (
        "the collision check did not see the dangling .dirty.patch symlink, so the "
        "recovery patch claimed the canonical name"
    )


def test_a_dangling_trash_meta_symlink_is_preserved(reaper_repo, tmp_path, monkeypatch):
    """Same class, other name. A dangling `.trash_meta.json` symlink read as
    absent, so `rename` replaced the user's entry instead of moving it aside."""
    repo = reaper_repo.repo
    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", trash / "tomb.jsonl")
    trash.mkdir(parents=True, exist_ok=True)

    wt = reaper_repo.wt_det_merged
    os.symlink(str(tmp_path / "NOWHERE.json"), str(wt / ".trash_meta.json"))
    _age_path(wt, 20)

    worktrees = wl._list_worktrees(repo)
    entry = next(w for w in worktrees if Path(w["path"]) == wt)
    cls = wl._classify(entry, worktrees, repo)
    wl._trash_worktree(cls, repo, lane="merged", merge_method=cls["merge_method"])

    import tarfile as _tf

    archives = sorted(trash.glob("*.tar.gz"))
    assert archives, "the worktree should still have been archived"
    with _tf.open(archives[0], "r:gz") as fh:
        names = {m.name.split("/", 1)[-1] for m in fh.getmembers()}
    assert ".trash_meta.json.from-worktree-1" in names, (
        "the user's own .trash_meta.json was destroyed rather than kept aside"
    )
    assert ".trash_meta.json" in names, "and ours must take the canonical name"


def test_the_patch_write_refuses_to_follow_a_symlink_the_check_missed(
    reaper_repo, tmp_path, monkeypatch,
):
    """The second guard, tested ALONE.

    The collision check answers "is this name taken"; the open answers "am I
    about to write through somebody's symlink". They are not the same question,
    and the gap between them is a real window -- the entry can appear between the
    check and the write. Here the check is forced blind so only O_NOFOLLOW is
    left standing.
    """
    repo = reaper_repo.repo
    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", trash / "tomb.jsonl")
    trash.mkdir(parents=True, exist_ok=True)

    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("locally modified\n")
    outside = tmp_path / "OUTSIDE_TARGET_2.txt"
    os.symlink(str(outside), str(wt / ".dirty.patch"))
    _age_path(wt, 20)

    real_lexists = os.path.lexists
    monkeypatch.setattr(
        os.path,
        "lexists",
        lambda q: False if str(q).endswith(".dirty.patch") else real_lexists(q),
    )

    worktrees = wl._list_worktrees(repo)
    entry = next(w for w in worktrees if Path(w["path"]) == wt)
    cls = wl._classify(entry, worktrees, repo)
    wl._trash_worktree(cls, repo, lane="merged", merge_method=cls["merge_method"])

    assert not outside.exists(), (
        "with the collision check blind, the open FOLLOWED the symlink and "
        f"created {outside} outside the tree"
    )

    # POSITIVE WITNESSES. "No outside file appeared" passes vacuously for any
    # change that never reaches the write at all -- a reap that skipped, or an
    # empty patch. MEASURED: with the patch text forced empty, the assertion
    # above still passed. So prove the write was REACHED and that it refused.
    import tarfile as _tf

    archives = sorted(trash.glob("*.tar.gz"))
    assert archives, "the worktree was not archived at all, so nothing was written"
    with _tf.open(archives[0], "r:gz") as fh:
        members = {m.name.split("/", 1)[-1]: m for m in fh.getmembers()}
    assert ".dirty.patch.archived-1" not in members, (
        "the collision check was not actually blinded, so this test is measuring "
        "the other guard"
    )
    assert ".dirty.patch" in members and members[".dirty.patch"].issym(), (
        "the user's symlink must still be the entry the write refused to follow"
    )


def _is_locked(repo, wt_path) -> bool:
    """True when git still holds a lock on this worktree's registration."""
    out = _git(repo, "worktree", "list", "--porcelain")
    block, found = [], False
    for line in out.splitlines():
        if line.startswith("worktree "):
            if found:
                break
            block, found = [], line[len("worktree "):] == str(wt_path)
        if found:
            block.append(line)
    return any(ln == "locked" or ln.startswith("locked ") for ln in block)


def test_a_failed_recovery_leaves_the_history_anchor_in_place(
    reaper_repo, tmp_path, monkeypatch,
):
    """Recovery unlocks the registration, and that lock IS the anchor.

    Archiving locks the worktree's registration, and the lock is the only thing
    keeping the archived commits reachable -- `git worktree prune` and `git gc`
    both leave a LOCKED registration alone and both remove an unlocked one.
    Recovery has to release it so `git worktree add --force` can take the path
    over. When everything after that fails, an earlier version returned with the
    registration still unlocked, so the tarball sat in the trash pointing at
    commits the next gc could collect.

    The same path also ran a repo-wide `git worktree prune`, which is harmless to
    every OTHER archive -- theirs are locked -- and fatal to this one, whose lock
    had just been released.

    REACHING THE PATH IS THE HARD PART, and the first version of this test did
    not: occupying the destination trips an "already exists" check that returns
    BEFORE the unlock, so the lock was still held for trivial reasons and the
    test passed against a `_relock` that did nothing. Both the checkout and the
    fallback move have to fail, with the destination free, to land on a return
    that the unlock precedes.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")

    wt = reaper_repo.wt_branch_merged
    wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo)
    assert not wt.exists()
    assert _is_locked(reaper_repo.repo, wt), "precondition: archiving locks the anchor"

    real_run = subprocess.run

    def _add_always_fails(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["git", "worktree", "add"]:
            return subprocess.CompletedProcess(cmd, 1, "", "simulated add failure")
        return real_run(cmd, *args, **kwargs)

    def _move_always_fails(*_a, **_k):
        raise OSError("simulated move failure")

    monkeypatch.setattr(subprocess, "run", _add_always_fails)
    monkeypatch.setattr(wl.shutil, "move", _move_always_fails)
    # The unlock lives on the REBUILD path. Reattach takes precedence and places
    # the tree with a rename, so make it impossible the way a real archive can:
    # its `.git` pointer is unusable while the locked registration survives.
    monkeypatch.setattr(wl, "_reattach_target", lambda *_a: ("absent", None))

    assert wl._recover("wt_branch_merged", reaper_repo.repo) is False

    assert _is_locked(reaper_repo.repo, wt), (
        "recovery failed and left the registration UNLOCKED — the archive's "
        "commits are now one prune or gc away from being collectable"
    )


def test_a_successful_recovery_still_works(reaper_repo, tmp_path, monkeypatch):
    """The control that moves. A re-lock on every failure path is worthless if it
    also fires on success, or if dropping the repo-wide prune broke recovery."""
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")

    wt = reaper_repo.wt_branch_merged
    (wt / "scratch.txt").write_text("untracked scratch\n")
    wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo)

    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    assert (wt / "scratch.txt").read_text() == "untracked scratch\n"
    assert not _is_locked(reaper_repo.repo, wt), (
        "a SUCCESSFUL recovery must leave a normal, unlocked worktree"
    )


def test_an_edit_below_the_sampled_depth_counts_as_activity(reaper_repo, tmp_path):
    """A worktree being actively edited must not read as idle.

    `_last_activity_time` walks the root and TWO levels. Modifying a file
    updates that file's mtime and NEVER its ancestors', and nearly all source in
    this repo lives below the sampled depth — so an actively developed worktree
    reported as weeks idle and became eligible for archiving.

    MEASURED against the pre-fix helper: backdate a worktree 19 days, edit
    `src/genesis/memory/store.py`, and it still reports 19.0 days. The control
    is what hid it — editing a depth-1 file like `README.md` always reported
    0.0, so the obvious test passed.
    """
    wt = reaper_repo.wt_branch_merged
    deep = wt / "src" / "genesis" / "memory"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "store.py").write_text("original\n")
    _age_path(wt, 19)

    # Precondition: the shallow walk alone must still call this idle, otherwise
    # the fixture is not exercising the gap and the assertion below is vacuous.
    assert (time.time() - os.path.getmtime(wt)) / 86400 > 10

    (deep / "store.py").write_text("EDITED\n")
    now = time.time()
    os.utime(deep / "store.py", (now, now))
    # Ancestors stay backdated, which is what a real edit looks like.
    for ancestor in (deep, deep.parent, deep.parent.parent, wt):
        os.utime(ancestor, (now - 19 * 86400, now - 19 * 86400))

    age_days = (time.time() - wl._last_activity_time(str(wt))) / 86400
    assert age_days < 1, (
        f"an edit at depth 3 left the worktree reading as {age_days:.1f} days "
        "idle — it would be archived while someone is working in it"
    )


def test_an_untouched_worktree_still_reads_as_idle(reaper_repo, tmp_path):
    """The control that moves, and the reason the commit timestamp was rejected.

    Consulting git must not make everything look busy. An aged worktree with a
    clean tree stays idle — including one whose HEAD commit is recent, which is
    every worktree freshly cut from the mainline. Keying on the commit time
    instead of on edits made exactly those read as active forever, and nine
    tests failed on it.
    """
    wt = reaper_repo.wt_branch_merged
    _age_path(wt, 19)
    age_days = (time.time() - wl._last_activity_time(str(wt))) / 86400
    assert age_days > 10, (
        f"a clean, aged worktree reported {age_days:.1f} days — consulting git "
        "made an idle worktree look active, so nothing would ever be reclaimed"
    )


# ─── review round 1: what the recovery patches must carry, and what happens when
#     one is missing. Each cell reproduces a measured loss. ─────────────────────


def _trash_dir_entry(reaper_repo, tmp_path, monkeypatch) -> Path:
    """Archive `wt_branch_merged` as a plain DIRECTORY entry (compression disabled)."""

    def _no_tar(*_a, **_k):
        raise OSError("compression disabled for this test")

    monkeypatch.setattr(wl.tarfile, "open", _no_tar)
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    wt = reaper_repo.wt_branch_merged
    assert wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo) is True
    (entry,) = [d for d in trash.iterdir() if d.is_dir() and d.name.startswith("wt_branch_merged")]
    return entry


def test_staged_and_unstaged_versions_of_one_file_both_come_back(
    reaper_repo, tmp_path, monkeypatch,
):
    """MEASURED before: a file staged as one version and edited to another kept only
    the second — the single HEAD→worktree patch had nowhere to put the first."""
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("STAGED UNIQUE\n")
    _git(wt, "add", "a.txt")
    (wt / "a.txt").write_text("WORKTREE UNIQUE\n")
    _trash_dir_entry(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    assert _git(wt, "show", ":a.txt") == "STAGED UNIQUE\n"
    assert (wt / "a.txt").read_text() == "WORKTREE UNIQUE\n"


def test_a_textconv_attribute_does_not_leak_into_the_saved_patch(
    reaper_repo, tmp_path, monkeypatch,
):
    """MEASURED before: an uppercasing textconv saved `a -> new` as `A -> NEW`, which
    cannot apply to `a`, and the edit was lost."""
    wt = reaper_repo.wt_branch_merged
    _git(reaper_repo.repo, "config", "diff.up.textconv", "tr a-z A-Z <")
    (wt / ".git_info_attrs").write_text("")  # keep the tree otherwise untouched
    info = Path(_git(wt, "rev-parse", "--git-common-dir").strip())
    if not info.is_absolute():
        info = wt / info
    (info / "info").mkdir(exist_ok=True)
    (info / "info" / "attributes").write_text("*.txt diff=up\n")
    (wt / "a.txt").write_text("new edit\n")
    (wt / ".git_info_attrs").unlink()
    _trash_dir_entry(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)  # force the patch fallback

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached", "this test is about the patch fallback"
    assert (wt / "a.txt").read_text() == "new edit\n"


def test_a_patch_write_that_fails_part_way_leaves_no_patch_and_applies_nothing(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """A truncated patch left under a reaper name was read at recovery as a user file,
    and a directory entry — the only complete copy — was deleted."""
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("DIRTY EDIT\n")
    real_fdopen = os.fdopen

    def _fail_patch_write(fd, *a, **k):
        fh = real_fdopen(fd, *a, **k)
        if "b" in (a[0] if a else k.get("mode", "")):
            class _Boom:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    fh.close()
                    return False

                def write(self, _data):
                    raise OSError(28, "No space left on device")
            return _Boom()
        return fh

    monkeypatch.setattr(wl.os, "fdopen", _fail_patch_write)
    entry = _trash_dir_entry(reaper_repo, tmp_path, monkeypatch)
    monkeypatch.setattr(wl.os, "fdopen", real_fdopen)

    assert not list(entry.glob(".dirty*patch*")), "a partial patch was left behind"
    _drop_registration(reaper_repo.repo, reaper_repo.wt_branch_merged)  # force the patch fallback
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    err = capsys.readouterr().err
    assert "UNCOMMITTED EDITS WERE NOT REAPPLIED" in err, err
    assert entry.is_dir(), "the directory entry holding the only copy was deleted"


def test_a_missing_recorded_patch_keeps_the_trash_and_applies_nothing(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """The metadata says tracked changes were saved but names no surviving patch."""
    import json

    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("DIRTY EDIT\n")
    entry = _trash_dir_entry(reaper_repo, tmp_path, monkeypatch)
    meta_path = entry / ".trash_meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta["had_tracked_patch"] is True and meta["patch_file"], "precondition"
    (entry / meta["patch_file"]).unlink()

    _drop_registration(reaper_repo.repo, reaper_repo.wt_branch_merged)  # force the patch fallback
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    err = capsys.readouterr().err
    assert "missing" in err, err
    assert (wt / "a.txt").read_text() == "a\n"
    assert entry.is_dir()


def test_a_non_utf8_filename_does_not_abort_recovery(reaper_repo, tmp_path, monkeypatch, capsys):
    """MEASURED before: the retry builder decoded git's filename bytes as strict UTF-8
    and raised, ending recovery before it said where the edits went."""
    repo = reaper_repo.repo
    wt = reaper_repo.wt_branch_merged
    odd = os.fsdecode(b"bad-\xff.txt")
    (wt / odd).write_text("new\n")
    _git(wt, "add", "--", odd)
    (wt / "a.txt").write_text("DIRTY EDIT\n")
    _trash_dirty_moved(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(repo, reaper_repo.wt_branch_merged)  # force the patch fallback
    assert wl._recover("wt_branch_merged", repo) is True
    err = capsys.readouterr().err
    cmds = _retry_commands(err)
    # The printed command must name the SAME bytes: run it and see the file move.
    move = subprocess.run(["bash", "-c", cmds[0]], capture_output=True)
    assert move.returncode == 0, (cmds[0], move.stderr)
    assert os.path.lexists(wt / (odd + ".restored")), cmds[0]


def _trash_dirty_moved(reaper_repo, tmp_path, monkeypatch):
    """Archive the worktree, then move its branch to a commit that conflicts."""
    repo = reaper_repo.repo
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    wt = reaper_repo.wt_branch_merged
    assert wl._trash_worktree(_wt_by_path(repo, wt), repo) is True
    cw = tmp_path / "conflict_src2"
    _git(repo, "worktree", "add", "-q", "--detach", str(cw), reaper_repo.c0)
    (cw / "a.txt").write_text("CONFLICT\n")
    _git(cw, "commit", "-qam", "conflicting change")
    conflict = _git(cw, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(cw))
    _git(repo, "update-ref", "refs/heads/merged-br", conflict)


def test_a_rename_is_moved_aside_not_excluded_in_the_retry(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """`--exclude` on a rename target skips the whole rename (MEASURED), and would drop
    a later edit to the renamed file. The retry moves the restored copy aside."""
    wt = reaper_repo.wt_branch_merged
    _git(wt, "mv", "a.txt", "renamed.txt")
    (wt / "renamed.txt").write_text("EDITED AFTER RENAME\n")
    _trash_dirty_moved(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, reaper_repo.wt_branch_merged)  # force the patch fallback
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    err = capsys.readouterr().err
    assert "UNCOMMITTED EDITS WERE NOT REAPPLIED" in err, err
    assert "--exclude" not in err, err
    cmds = _retry_commands(err)
    assert "mv -- renamed.txt renamed.txt.restored" in cmds[0], cmds
    for cmd in cmds[:2]:
        rerun = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        assert rerun.returncode == 0, (cmd, rerun.stderr)
    assert "renamed.txt" in _git(wt, "diff", "--cached", "--name-only").split()
    assert (wt / "renamed.txt.restored").read_text() == "EDITED AFTER RENAME\n"


def test_a_failed_patch_copy_leaves_no_truncated_file(tmp_path, monkeypatch):
    src = tmp_path / "src.patch"
    src.write_bytes(b"x" * 100)
    dest_dir = tmp_path / "wt"
    dest_dir.mkdir()
    real_fdopen = os.fdopen

    class _Boom:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            return False

        def write(self, data):
            self.fh.write(data[:10])
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(wl.os, "fdopen", lambda fd, *a, **k: _Boom(real_fdopen(fd, *a, **k)))
    dest, err = wl._keep_unapplied_patch(src, str(dest_dir))
    assert dest is None and err
    assert list(dest_dir.iterdir()) == [], "a truncated patch was left in the worktree"


def test_metadata_that_names_no_patch_is_never_guessed_at(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """Devin's case: the rewrite that records the patch name fails, so every copy of
    the metadata still says `patch_file: None`. A user file under a patch-like name
    must not be applied in its place; nothing is applied and the trash is kept."""
    import json

    wt = reaper_repo.wt_branch_merged
    own = _a_valid_patch_creating_u(wt)
    (wt / ".dirty.patch.archived-5").write_text(own)
    (wt / "a.txt").write_text("DIRTY EDIT\n")
    entry = _trash_dir_entry(reaper_repo, tmp_path, monkeypatch)
    meta_path = entry / ".trash_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["patch_file"] = None
    meta["index_patch_file"] = None
    meta_path.write_text(json.dumps(meta))

    _drop_registration(reaper_repo.repo, reaper_repo.wt_branch_merged)  # force the patch fallback
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    assert not (wt / "u.txt").exists(), "a user file was applied as the patch"
    assert "missing" in capsys.readouterr().err
    assert entry.is_dir()


# ─── review round 2: reattach first; the patch fallback fails safe ─────────────


def _state(wt: Path) -> tuple[str, str, str]:
    """Everything a recovery must preserve: index entries with flags, status, content."""
    return (
        _git(wt, "ls-files", "-s", "-v", "-t"),
        _git(wt, "status", "--porcelain=v2", "--untracked-files=all"),
        "".join(sorted(f"{p.relative_to(wt)}={p.read_bytes()!r}\n"
                       for p in wt.rglob("*") if p.is_file() and ".git" not in p.parts)),
    )


def _rich_dirty_state(wt: Path) -> None:
    (wt / "a.txt").write_text("STAGED UNIQUE\n")
    _git(wt, "add", "a.txt")
    (wt / "a.txt").write_text("WORKTREE UNIQUE\n")
    (wt / "ita.txt").write_text("intent to add\n")
    _git(wt, "add", "-N", "ita.txt")
    (wt / "untracked.txt").write_text("untracked\n")


@pytest.mark.parametrize("compress", [True, False], ids=["archive", "directory"])
def test_reattach_restores_the_exact_state(reaper_repo, tmp_path, monkeypatch, compress):
    """The registration (and its index) survives archiving, so moving the tree back
    under it restores what no patch can carry: the staged/unstaged split, an
    intent-to-add entry, untracked files — byte for byte, flags included."""
    wt = reaper_repo.wt_branch_merged
    _rich_dirty_state(wt)
    before = _state(wt)
    if not compress:
        def _no_tar(*_a, **_k):
            raise OSError("compression disabled for this test")

        monkeypatch.setattr(wl.tarfile, "open", _no_tar)
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    assert wl._trash_worktree(_wt_by_path(reaper_repo.repo, wt), reaper_repo.repo) is True
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") == "reattached", report
    assert _state(wt) == before


def test_reattach_keeps_the_worktrees_own_files_and_drops_only_the_reapers(
    reaper_repo, tmp_path, monkeypatch,
):
    wt = reaper_repo.wt_branch_merged
    (wt / ".dirty.patch").write_text("MY OWN PATCH\n")
    (wt / ".trash_meta.json").write_text("MY OWN META\n")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") == "reattached"
    assert (wt / ".dirty.patch").read_text() == "MY OWN PATCH\n"
    assert (wt / ".trash_meta.json").read_text() == "MY OWN META\n"
    left = sorted(p.name for p in wt.iterdir() if p.name.startswith(".dirty"))
    assert left == [".dirty.patch"], left
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"


def test_reattach_after_the_branch_was_deleted_detaches_and_keeps_the_index(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("STAGED\n")
    _git(wt, "add", "a.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit="WORKTREE\n")
    _git(reaper_repo.repo, "update-ref", "-d", "refs/heads/merged-br")
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    assert "DETACHED" in capsys.readouterr().err
    assert _git(wt, "show", ":a.txt") == "STAGED\n"
    assert (wt / "a.txt").read_text() == "WORKTREE\n"


def test_recovery_rebuilds_from_patches_when_the_registration_is_gone(
    reaper_repo, tmp_path, monkeypatch,
):
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"


def test_user_diff_config_does_not_break_the_saved_patches(reaper_repo, tmp_path, monkeypatch):
    """SF-1: porcelain `git diff` honoured these and every apply failed (MEASURED)."""
    repo = reaper_repo.repo
    for key, val in (("diff.noprefix", "true"), ("color.diff", "always"),
                     ("diff.context", "0")):
        _git(repo, "config", key, val)
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(repo, wt)
    assert wl._recover("wt_branch_merged", repo) is True
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"


@pytest.mark.parametrize("failing", [".dirty.patch", ".dirty.index.patch"])
def test_one_failed_patch_half_is_missing_not_clean(
    reaper_repo, tmp_path, monkeypatch, capsys, failing,
):
    """B-1: a half whose write failed recorded no name, read as "nothing to save",
    and recovery reported success and deleted the only copy (MEASURED)."""
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("STAGED\n")
    _git(wt, "add", "a.txt")
    real_open = os.open

    def _enospc(path, *a, **k):
        if str(path).endswith("/" + failing):
            raise OSError(28, "No space left on device")
        return real_open(path, *a, **k)

    def _no_tar(*_a, **_k):
        raise OSError("compression disabled for this test")

    monkeypatch.setattr(wl.tarfile, "open", _no_tar)
    monkeypatch.setattr(wl.os, "open", _enospc)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit="WORKTREE\n")
    monkeypatch.setattr(wl.os, "open", real_open)
    (entry,) = [d for d in (tmp_path / "trash").iterdir()
                if d.is_dir() and d.name.startswith("wt_branch_merged")]
    _drop_registration(reaper_repo.repo, wt)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("incomplete") is True
    # The MECHANISM, not only the outcome: verify-before-delete would also keep the
    # entry, so the assertion names the missing-patch refusal itself.
    assert "missing" in capsys.readouterr().err
    assert entry.is_dir(), "the directory entry holding the only copy was deleted"


def test_a_legacy_directory_is_kept_when_the_recovered_tree_differs(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """Verify-before-delete. The archived tree held an UNTRACKED `u.txt`; the branch
    has since gained a TRACKED `u.txt` with other content, so the rebuilt checkout
    creates it and copy-only-missing (rightly) will not overwrite it. The archive's
    version exists nowhere else, so the legacy directory entry must survive."""
    repo = reaper_repo.repo

    def _no_tar(*_a, **_k):
        raise OSError("compression disabled for this test")

    monkeypatch.setattr(wl.tarfile, "open", _no_tar)
    wt = reaper_repo.wt_branch_merged
    (wt / "u.txt").write_text("ARCHIVED UNTRACKED\n")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    (entry,) = [d for d in (tmp_path / "trash").iterdir()
                if d.is_dir() and d.name.startswith("wt_branch_merged")]
    cw = tmp_path / "mover"
    _git(repo, "worktree", "add", "-q", "--detach", str(cw), reaper_repo.c0)
    (cw / "u.txt").write_text("TRACKED ON THE BRANCH\n")
    _git(cw, "add", "u.txt")
    _git(cw, "commit", "-qm", "the branch gains u.txt")
    moved = _git(cw, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(cw))
    _git(repo, "update-ref", "refs/heads/merged-br", moved)
    _drop_registration(repo, wt)
    report: dict = {}
    assert wl._recover("wt_branch_merged", repo, report) is True
    err = capsys.readouterr().err
    assert report.get("incomplete") is True
    assert "DIFFERS FROM THE ARCHIVE" in err and "u.txt" in err, err
    assert (entry / "u.txt").read_text() == "ARCHIVED UNTRACKED\n"


def test_recover_exits_2_when_the_edits_were_not_reapplied(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """SF-3: a worktree without its edits is not a successful recovery."""
    repo = reaper_repo.repo
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    cw = tmp_path / "conflict_exit"
    _git(repo, "worktree", "add", "-q", "--detach", str(cw), reaper_repo.c0)
    (cw / "a.txt").write_text("CONFLICT\n")
    _git(cw, "commit", "-qam", "conflicting change")
    conflict = _git(cw, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(cw))
    _git(repo, "update-ref", "refs/heads/merged-br", conflict)
    _drop_registration(repo, wt)
    rc = _run_main(monkeypatch, repo, tmp_path / "trash",
                   argv=("worktree_lifecycle.py", "--recover", "wt_branch_merged"))
    assert rc == 2
    # The MECHANISM: verify-before-delete alone would also give 2.
    assert "UNCOMMITTED EDITS WERE NOT REAPPLIED" in capsys.readouterr().err


def test_running_the_printed_move_step_twice_never_overwrites_a_backup(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """SF-2: the `.restored` copies hold the only final working-tree content, and
    running the printed block a second time used to replace them (MEASURED)."""
    repo = reaper_repo.repo
    wt = reaper_repo.wt_branch_merged
    (wt / "staged_new.txt").write_text("staged\n")
    _git(wt, "add", "staged_new.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    cw = tmp_path / "conflict_twice"
    _git(repo, "worktree", "add", "-q", "--detach", str(cw), reaper_repo.c0)
    (cw / "a.txt").write_text("CONFLICT\n")
    _git(cw, "commit", "-qam", "conflicting change")
    conflict = _git(cw, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(cw))
    _git(repo, "update-ref", "refs/heads/merged-br", conflict)
    _drop_registration(repo, wt)
    assert wl._recover("wt_branch_merged", repo) is True
    move = _retry_commands(capsys.readouterr().err)[0]
    assert move.startswith("cd "), move
    first = subprocess.run(["bash", "-c", move], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    (wt / "staged_new.txt").write_text("partially applied\n")
    again = subprocess.run(["bash", "-c", move], capture_output=True, text=True)
    assert again.returncode != 0
    assert (wt / "staged_new.txt.restored").read_text() == "staged\n"


def test_an_inherited_index_file_variable_does_not_break_recovery(
    reaper_repo, tmp_path, monkeypatch,
):
    """NOTE-4: GIT_INDEX_FILE pointed `git apply --3way` at the wrong index. A STAGED
    change is what makes recovery read the index at all."""
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("DIRTY EDIT\n")
    _git(wt, "add", "a.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere.index"))
    assert wl._recover("wt_branch_merged", reaper_repo.repo) is True
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"
    # The staged CONTENT, not the name: with no index at all `diff --cached` also
    # lists a.txt (as a staged deletion), which passed for the wrong reason.
    assert _git(wt, "show", ":a.txt") == "DIRTY EDIT\n"


def test_a_non_utf8_name_with_quotepath_off_does_not_crash_the_dirty_check(tmp_path):
    """NOTE-5: `core.quotePath=false` prints the name raw, and a strict decode raised."""
    repo = tmp_path / "qp"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "core.quotePath", "false")
    (repo / os.fsdecode(b"bad-\xff.txt")).write_text("x\n")
    assert wl._has_uncommitted_changes(str(repo)) is True


def test_a_pre_patch_archive_is_not_reported_as_missing_its_patch(tmp_path):
    """NOTE-1: an archive from before patches were recorded has no
    `had_tracked_patch` at all, and a clean one is not missing anything."""
    entry = tmp_path / "entry"
    entry.mkdir()
    assert wl._saved_patches_in(entry, {"original_path": "x"}) == (None, None, False)


# ─── reattach and fallback: round-3 review findings ─────────────────────────


def _disable_compression(monkeypatch) -> None:
    def _no_tar(*_a, **_k):
        raise OSError("compression disabled for this test")

    monkeypatch.setattr(wl.tarfile, "open", _no_tar)


def _only_entry(tmp_path: Path) -> Path:
    (entry,) = [d for d in (tmp_path / "trash").iterdir()
                if d.is_dir() and d.name.startswith("wt_branch_merged")]
    return entry


def test_a_cross_filesystem_reattach_never_deletes_the_complete_copy(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """F1: across filesystems a move is copy-then-delete-source. When the delete
    failed part way (a read-only directory), the source was left PARTIAL and the
    code deleted the COMPLETE copy at the destination. MEASURED: 10 of 40 files
    gone everywhere, and the entry's metadata with them."""
    import errno

    _disable_compression(monkeypatch)
    wt = reaper_repo.wt_branch_merged
    for i in range(6):
        (wt / f"file{i}.txt").write_text(f"content {i}\n")
    ro = wt / "aa_ro"
    ro.mkdir()
    (ro / "inner.txt").write_text("inside a read-only directory\n")
    os.chmod(ro, 0o555)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    entry = _only_entry(tmp_path)

    real_rename = os.rename

    def _exdev(src, dst, *a, **k):
        if Path(src) == entry:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(wl.os, "rename", _exdev)
    report: dict = {}
    try:
        assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
        assert report.get("mode") == "reattached", report
        for i in range(6):
            assert (wt / f"file{i}.txt").read_text() == f"content {i}\n"
        assert (wt / "aa_ro" / "inner.txt").read_text() == "inside a read-only directory\n"
        assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"
    finally:
        monkeypatch.setattr(wl.os, "rename", real_rename)
        for p in (wt / "aa_ro", entry / "aa_ro"):
            if p.exists():
                os.chmod(p, 0o755)


def test_a_cross_filesystem_reattach_that_does_not_verify_keeps_the_archive(
    reaper_repo, tmp_path, monkeypatch,
):
    """F1, the move-back half: with the source intact, only the copy is removed."""
    import errno

    _disable_compression(monkeypatch)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    entry = _only_entry(tmp_path)
    before = sorted(str(p.relative_to(entry)) for p in entry.rglob("*"))
    real_rename, real_git = os.rename, wl._run_git

    def _exdev(src, dst, *a, **k):
        if Path(src) == entry:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *a, **k)

    def _silent(root, args, *, timeout):
        if "--absolute-git-dir" in args:
            return None
        return real_git(root, args, timeout=timeout)

    monkeypatch.setattr(wl.os, "rename", _exdev)
    monkeypatch.setattr(wl, "_run_git", _silent)
    assert wl._recover("wt_branch_merged", reaper_repo.repo, {}) is False
    monkeypatch.setattr(wl.os, "rename", real_rename)
    assert not reaper_repo.wt_branch_merged.exists(), "the unverified copy was left in place"
    assert sorted(str(p.relative_to(entry)) for p in entry.rglob("*")) == before
    assert _is_locked(reaper_repo.repo, reaper_repo.wt_branch_merged)


def test_an_older_archive_never_reattaches_under_a_newer_generation(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """F2: archive A1, recover it, archive the same path again as A2, then recover
    A1. The registration's `gitdir` points at the same path for both, so the path
    check accepted it: A1's tree came back under A2's index ("exactly as
    archived") and A2's history anchor was removed. MEASURED."""
    repo = reaper_repo.repo
    wt = reaper_repo.wt_branch_merged
    trash = tmp_path / "trash"
    _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit="GENERATION ONE\n")
    (a1,) = trash.glob("wt_branch_merged*.tar.gz")
    assert wl._recover(a1.name, repo) is True
    (wt / "a.txt").write_text("GENERATION TWO\n")
    _git(wt, "add", "a.txt")
    assert wl._trash_worktree(_wt_by_path(repo, wt), repo) is True
    (a2,) = [p for p in trash.glob("wt_branch_merged*.tar.gz") if p != a1]

    report: dict = {}
    assert wl._recover(a1.name, repo, report) is False
    err = capsys.readouterr().err
    assert report.get("mode") != "reattached"
    assert not wt.exists(), "the older archive was placed at the path"
    assert _is_locked(repo, wt), "the newer archive's history anchor was removed"
    assert a2.name.removesuffix(".tar.gz") in err, err

    assert wl._recover(a2.name, repo, {}) is True
    assert _git(wt, "show", ":a.txt") == "GENERATION TWO\n"


def test_a_nested_clone_and_a_nested_meta_named_file_come_back(
    reaper_repo, tmp_path, monkeypatch,
):
    """F3: the copy loop and the verify-before-delete check both skipped `.git` and
    `.trash_meta.json` at ANY depth, so a legacy entry holding an untracked nested
    clone (or a user's own file of that name) was deleted with them missing."""
    _disable_compression(monkeypatch)
    wt = reaper_repo.wt_branch_merged
    nested = wt / "vendor" / "clone"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    _git(nested, "-c", "user.email=a@a", "-c", "user.name=a",
         "commit", "-q", "--allow-empty", "-m", "nested history")
    (wt / "sub").mkdir()
    (wt / "sub" / ".trash_meta.json").write_text("A USER FILE\n")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached"
    assert (wt / "sub" / ".trash_meta.json").read_text() == "A USER FILE\n"
    assert (nested / ".git" / "HEAD").is_file(), "the nested clone lost its history"
    assert "nested history" in _git(nested, "log", "--format=%s")


def test_an_intent_to_add_entry_does_not_break_the_fallback(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """F4: the staged patch carried an intent-to-add file as an empty new file and the
    unstaged patch created it again, so the whole apply failed ("already exists")
    and an unrelated edit was not reapplied. MEASURED."""
    wt = reaper_repo.wt_branch_merged
    (wt / "ita.txt").write_text("intent to add\n")
    _git(wt, "add", "-N", "ita.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(reaper_repo.repo, wt)

    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    err = capsys.readouterr().err
    assert "NOT REAPPLIED" not in err, err
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"
    assert (wt / "ita.txt").read_text() == "intent to add\n"


def test_a_reattach_whose_commit_was_collected_is_incomplete(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """F5: the lock anchors the registration, not a deleted branch's commit. Once the
    worktree's reflog expired, gc collected it and the reattach still said "exactly
    as archived" with exit 0 while `git status` showed no commits. MEASURED."""
    repo = reaper_repo.repo
    wt = reaper_repo.wt_branch_merged
    (wt / "own.txt").write_text("own\n")
    _git(wt, "add", "own.txt")
    _git(wt, "commit", "-q", "-m", "a commit only this branch has")
    unique = _git(wt, "rev-parse", "HEAD").strip()
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _git(repo, "update-ref", "-d", "refs/heads/merged-br")
    _git(repo, "-c", "gc.reflogExpire=now", "-c", "gc.reflogExpireUnreachable=now",
         "gc", "-q", "--prune=now")
    gone = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", unique],
                          capture_output=True)
    assert gone.returncode != 0, "precondition: gc collected the branch's commit"

    report: dict = {}
    assert wl._recover("wt_branch_merged", repo, report) is True
    err = capsys.readouterr().err
    assert report.get("incomplete") is True
    assert unique[:8] in err and "no longer exists" in err.lower(), err


def test_a_non_utf8_untracked_name_does_not_crash_the_reattach(
    reaper_repo, tmp_path, monkeypatch,
):
    """F6: the reattach verify decoded `git status` strictly. With core.quotePath off
    a raw name raised after the move and the unlock, leaving the tree unlocked with
    the reaper's files in it and a traceback. MEASURED."""
    wt = reaper_repo.wt_branch_merged
    _git(reaper_repo.repo, "config", "core.quotePath", "false")
    bad = os.fsdecode(b"bad-\xff.txt")
    (wt / bad).write_text("raw name\n")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") == "reattached", report
    assert (wt / bad).read_text() == "raw name\n"
    assert not (wt / ".trash_meta.json").exists()


def test_a_git_that_cannot_answer_does_not_destroy_the_index(
    reaper_repo, tmp_path, monkeypatch,
):
    """F7: a verify step that merely FAILED to run (a timeout on a slow disk) sent
    recovery to the rebuild, whose `worktree add --force` replaced the registration
    and the only complete copy of the staged state with it. MEASURED."""
    repo = reaper_repo.repo
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("STAGED\n")
    _git(wt, "add", "a.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit="WORKTREE\n")
    real = wl._run_git

    def _silent(root, args, *, timeout):
        if "--absolute-git-dir" in args:
            return None
        return real(root, args, timeout=timeout)

    monkeypatch.setattr(wl, "_run_git", _silent)
    assert wl._recover("wt_branch_merged", repo, {}) is False
    assert not wt.exists()
    assert _is_locked(repo, wt), "the history anchor was not put back"

    monkeypatch.setattr(wl, "_run_git", real)
    report: dict = {}
    assert wl._recover("wt_branch_merged", repo, report) is True
    assert report.get("mode") == "reattached", report
    assert _git(wt, "show", ":a.txt") == "STAGED\n"
    assert (wt / "a.txt").read_text() == "WORKTREE\n"


def test_user_whitespace_config_does_not_alter_the_reapplied_edit(
    reaper_repo, tmp_path, monkeypatch,
):
    """F8: the saved patches ignore user diff config, but `git apply` honoured
    `apply.whitespace=fix` and silently stripped trailing whitespace. MEASURED."""
    repo = reaper_repo.repo
    _git(repo, "config", "apply.whitespace", "fix")
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit="trailing space   \n")
    _drop_registration(repo, wt)
    report: dict = {}
    assert wl._recover("wt_branch_merged", repo, report) is True
    assert (wt / "a.txt").read_text() == "trailing space   \n"
    assert not report.get("incomplete"), report


def test_messages_name_the_stored_archive_not_its_scratch_copy(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """F9: for a tarball the message named `.extract-…`, which `_recover` deletes on
    the way out, at exactly the moment the user needs the real location."""
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("STAGED\n")
    _git(wt, "add", "a.txt")
    real_open = os.open

    def _enospc(path, *a, **k):
        if str(path).endswith("/.dirty.patch"):
            raise OSError(28, "No space left on device")
        return real_open(path, *a, **k)

    monkeypatch.setattr(wl.os, "open", _enospc)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit="WORKTREE\n")
    monkeypatch.setattr(wl.os, "open", real_open)
    (archive,) = (tmp_path / "trash").glob("wt_branch_merged*.tar.gz")
    _drop_registration(reaper_repo.repo, wt)
    assert wl._recover("wt_branch_merged", reaper_repo.repo, {}) is True
    err = capsys.readouterr().err
    assert str(archive) in err, err
    assert ".extract-" not in err, err


def test_a_recovery_that_could_not_recreate_the_worktree_is_incomplete(
    reaper_repo, tmp_path, monkeypatch,
):
    """N2: the plain-directory fallback returned success, so `--recover` exited 0
    for a tree git no longer recognises."""
    import json

    _disable_compression(monkeypatch)
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    meta_path = _only_entry(tmp_path) / ".trash_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["commit"] = "0" * 40
    meta_path.write_text(json.dumps(meta))
    _git(reaper_repo.repo, "update-ref", "-d", "refs/heads/merged-br")
    _drop_registration(reaper_repo.repo, wt)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("incomplete") is True


def test_a_reattach_onto_a_moved_branch_is_flagged(reaper_repo, tmp_path, monkeypatch, capsys):
    """N3: the tree is exactly as archived, but the branch moved, so its staged
    changes now read as reverting the other side's commits. Exit 2, and say so."""
    repo = reaper_repo.repo
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _git(repo, "update-ref", "refs/heads/merged-br", reaper_repo.c1)
    report: dict = {}
    assert wl._recover("wt_branch_merged", repo, report) is True
    err = capsys.readouterr().err
    assert report.get("mode") == "reattached"
    assert report.get("incomplete") is True
    assert "MOVED" in err, err


def test_run_git_survives_a_raw_non_utf8_name(tmp_path):
    """F6, the helper itself: git prints a raw name with core.quotePath off, and a
    strict decode raised out of `_run_git`. The surrogates map back to the bytes."""
    repo = tmp_path / "raw"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "core.quotePath", "false")
    name = os.fsdecode(b"bad-\xff.txt")
    (repo / name).write_text("x\n")
    out = wl._run_git(repo, ["ls-files", "--others"], timeout=30)
    assert out is not None and out.strip() == name


def test_the_worktrees_own_meta_file_comes_back_in_the_fallback(
    reaper_repo, tmp_path, monkeypatch,
):
    """Archiving sets a worktree's own `.trash_meta.json` aside so the reaper's can
    take the name. Only the reattach gave it back; the rebuild left it under the
    aside name, and the verify compared it there, so nothing noticed."""
    _disable_compression(monkeypatch)
    wt = reaper_repo.wt_branch_merged
    (wt / ".trash_meta.json").write_text("MY OWN META\n")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    entry = _only_entry(tmp_path)
    _drop_registration(reaper_repo.repo, wt)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") != "reattached"
    assert (wt / ".trash_meta.json").read_text() == "MY OWN META\n"
    assert not list(wt.glob(".trash_meta.json.from-worktree-*"))
    assert not report.get("incomplete"), report
    assert not entry.exists(), "a legacy entry that came back whole is consumed"


def test_unrestored_notices_a_lost_executable_bit(tmp_path):
    """N5: a file whose bytes came back but whose executable bit did not differs."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for d in (src, dst):
        (d / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(src / "run.sh", 0o755)
    os.chmod(dst / "run.sh", 0o644)
    assert wl._unrestored(src, str(dst), set()) == ["run.sh"]
    os.chmod(dst / "run.sh", 0o755)
    assert wl._unrestored(src, str(dst), set()) == []


# ─── round 2 of cross-model review ───────────────────────────────────────────


def test_an_unreadable_registration_pointer_changes_nothing(reaper_repo, tmp_path, monkeypatch):
    """A registration that could not be READ was treated as absent, and the rebuild
    replaced it and the archived index with it. An unanswered question now stops the
    recovery with nothing changed (cross-model review, round 2)."""
    _disable_compression(monkeypatch)
    wt = reaper_repo.wt_branch_merged
    (wt / "a.txt").write_text("STAGED\n")
    _git(wt, "add", "a.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch, edit="WORKTREE\n")
    pointer = _only_entry(tmp_path) / ".git"
    os.chmod(pointer, 0o000)
    try:
        assert wl._recover("wt_branch_merged", reaper_repo.repo, {}) is False
        assert not wt.exists()
        assert _is_locked(reaper_repo.repo, wt), "the history anchor was released"
    finally:
        os.chmod(pointer, 0o644)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") == "reattached", report
    assert _git(wt, "show", ":a.txt") == "STAGED\n"


def test_a_non_utf8_worktree_name_still_reattaches(reaper_repo, tmp_path, monkeypatch):
    """Git writes a worktree's `.git` pointer and its admin `gitdir` byte for byte, and
    a strict decode of a non-UTF-8 name sent recovery down the lossy rebuild."""
    repo = reaper_repo.repo
    name = os.fsdecode(b"wt-raw-\xff")
    wt = tmp_path / name
    _git(repo, "worktree", "add", "-q", "-b", "raw-br", str(wt), reaper_repo.c0)
    (wt / "a.txt").write_text("STAGED RAW\n")
    _git(wt, "add", "a.txt")
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    assert wl._trash_worktree(_wt_by_path(repo, wt), repo) is True
    report: dict = {}
    assert wl._recover("wt-raw-", repo, report) is True
    assert report.get("mode") == "reattached", report
    assert _git(wt, "show", ":a.txt") == "STAGED RAW\n"


def test_a_branch_held_by_another_worktree_is_recovered_detached(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """The rebuild's `--force` (needed to take over the still-registered path) also
    allowed a second checkout of a branch another worktree already had, so the two
    trees would move one ref under each other (cross-model review, round 2)."""
    repo = reaper_repo.repo
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(repo, wt)
    other = tmp_path / "other_holder"
    _git(repo, "worktree", "add", "-q", str(other), "merged-br")
    assert wl._recover("wt_branch_merged", repo, {}) is True
    out, err = capsys.readouterr()
    assert "DETACHED" in err
    assert "branch: merged-br" not in out, "the success line claimed the branch"
    head = subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"],
                          capture_output=True, text=True)
    assert head.returncode != 0, "the recovered tree is a second checkout of the branch"
    assert _git(other, "symbolic-ref", "--short", "HEAD").strip() == "merged-br"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"


def test_a_reattach_that_cannot_remove_the_reapers_files_is_incomplete(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """With the tree's root not writable the reaper's patch and metadata stay in it,
    and the recovery claimed an exact state with exit 0 (cross-model review, round 2)."""
    _disable_compression(monkeypatch)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    entry = _only_entry(tmp_path)
    wt = reaper_repo.wt_branch_merged
    real_place = wl._place_tree

    # Read-only only AFTER the tree is placed: Linux needs write permission on a
    # directory to re-parent it, so a read-only archive entry never gets that far.
    def place_then_lock(src, dest):
        moved = real_place(src, dest)
        os.chmod(dest, 0o555)
        return moved

    monkeypatch.setattr(wl, "_place_tree", place_then_lock)
    report: dict = {}
    try:
        assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    finally:
        os.chmod(wt if wt.exists() else entry, 0o755)
    assert report.get("mode") == "reattached", report
    assert report.get("incomplete") is True
    assert (wt / ".trash_meta.json").exists(), "the precondition did not hold"
    assert "exactly as archived" not in capsys.readouterr().out


def test_the_retry_commands_name_a_non_utf8_worktree_exactly():
    """The worktree path was quoted with `shlex.quote` and printed with a lossy
    escape, so every `cd` / `git -C` named a different directory."""
    worktree = os.fsdecode(b"/nonexistent/w-\xff")
    text = "\n".join(wl._retry_lines([Path(worktree) / ".dirty.patch"], worktree, set()))
    assert "$'/nonexistent/w-\\xff'" in text, text
    assert "\udcff" not in text


def test_the_retry_never_replaces_a_dangling_symlink_backup(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """`test ! -e` follows a symlink, so it reported a DANGLING `X.restored` link as
    absent and the move replaced the user's link (cross-model review, round 2)."""
    repo = reaper_repo.repo
    wt = reaper_repo.wt_branch_merged
    (wt / "staged_new.txt").write_text("staged\n")
    _git(wt, "add", "staged_new.txt")
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    cw = tmp_path / "conflict_link"
    _git(repo, "worktree", "add", "-q", "--detach", str(cw), reaper_repo.c0)
    (cw / "a.txt").write_text("CONFLICT\n")
    _git(cw, "commit", "-qam", "conflicting change")
    conflict = _git(cw, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "remove", "--force", str(cw))
    _git(repo, "update-ref", "refs/heads/merged-br", conflict)
    _drop_registration(repo, wt)
    assert wl._recover("wt_branch_merged", repo) is True
    move = _retry_commands(capsys.readouterr().err)[0]
    link = wt / "staged_new.txt.restored"
    os.symlink(str(tmp_path / "nowhere"), link)
    res = subprocess.run(["bash", "-c", move], capture_output=True, text=True)
    assert res.returncode != 0
    assert link.is_symlink(), "the user's symlink was replaced"


def test_a_worktree_meta_that_cannot_be_renamed_back_is_reported(tmp_path):
    """The worktree's own `.trash_meta.json`, set aside at archive time, could fail to
    get its name back and the caller was never told (cross-model review, round 2)."""
    dest = tmp_path / "dest"
    dest.mkdir()
    aside = ".trash_meta.json.from-worktree-1"
    (dest / aside).write_text("MINE\n")
    os.chmod(dest, 0o555)
    try:
        assert wl._restore_preserved_meta(dest, {"preserved_meta": aside}) is False
    finally:
        os.chmod(dest, 0o755)
    assert wl._restore_preserved_meta(dest, {"preserved_meta": aside}) is True
    assert (dest / ".trash_meta.json").read_text() == "MINE\n"


@pytest.mark.parametrize("rebuild", [False, True], ids=["reattach", "rebuild"])
def test_a_meta_that_did_not_come_back_makes_the_recovery_incomplete(
    reaper_repo, tmp_path, monkeypatch, rebuild,
):
    """Both recovery paths must report a worktree file left under its aside name."""
    _disable_compression(monkeypatch)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    if rebuild:
        _drop_registration(reaper_repo.repo, reaper_repo.wt_branch_merged)
    monkeypatch.setattr(wl, "_restore_preserved_meta", lambda *_a: False)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert (report.get("mode") == "reattached") is (not rebuild), report
    assert report.get("incomplete") is True


def test_a_failed_rebuild_of_a_non_utf8_worktree_is_reported(reaper_repo, tmp_path, monkeypatch):
    """`git worktree add` names the target path when it fails, and a strict decode of a
    non-UTF-8 name raised out of the recovery instead of reporting the failure."""
    repo = reaper_repo.repo
    parent = tmp_path / "ro_parent"
    parent.mkdir()
    wt = parent / os.fsdecode(b"wt-raw-\xff")
    _git(repo, "worktree", "add", "-q", "-b", "raw-br2", str(wt), reaper_repo.c0)
    (wt / "a.txt").write_text("EDIT RAW\n")
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    assert wl._trash_worktree(_wt_by_path(repo, wt), repo) is True
    _drop_registration(repo, wt)
    os.chmod(parent, 0o555)
    try:
        assert wl._recover("wt-raw-", repo, {}) is False
    finally:
        os.chmod(parent, 0o755)
    assert [d for d in trash.iterdir() if d.name.startswith("wt-raw-")], "the archive went"


@pytest.mark.parametrize("break_it", ["head_missing", "gitdir_unreadable", "git_silent"])
def test_every_unanswered_registration_check_is_unknown(
    reaper_repo, tmp_path, monkeypatch, break_it,
):
    """Every check `_reattach_target` could not ANSWER must come back unknown, never
    absent: absent sends recovery to the rebuild, which replaces the registration."""
    _disable_compression(monkeypatch)
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    entry = _only_entry(tmp_path)
    state, admin = wl._reattach_target(entry, str(wt), reaper_repo.repo)
    assert state == "ok", state
    if break_it == "head_missing":
        (admin / "HEAD").rename(admin / "HEAD.aside")
    elif break_it == "gitdir_unreadable":
        os.chmod(admin / "gitdir", 0o000)
    else:
        monkeypatch.setattr(wl, "_run_git", lambda *_a, **_k: None)
    try:
        assert wl._reattach_target(entry, str(wt), reaper_repo.repo) == ("unknown", None)
    finally:
        if break_it == "head_missing":
            (admin / "HEAD.aside").rename(admin / "HEAD")
        elif break_it == "gitdir_unreadable":
            os.chmod(admin / "gitdir", 0o644)



def test_a_clean_reattach_says_it_is_exact(reaper_repo, tmp_path, monkeypatch, capsys):
    """CONTROL for the incomplete case: the exact-restore line is still printed when
    every check passed."""
    _disable_compression(monkeypatch)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") == "reattached" and not report.get("incomplete"), report
    assert "exactly as archived" in capsys.readouterr().out


def test_a_branch_being_rebased_elsewhere_is_not_checked_out_twice(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """`worktree list` shows a worktree mid-rebase as detached, yet git counts it as
    holding the branch. Copying the list's view let the rebuild make a second
    checkout; git's own switch is what decides now (review of round 2)."""
    repo = reaper_repo.repo
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(repo, wt)
    other = tmp_path / os.fsdecode(b"rebasing-\xfc")  # git's refusal names this path
    _git(repo, "worktree", "add", "-q", str(other), "merged-br")
    stop = subprocess.run(["git", "-C", str(other), "rebase", "--exec", "false", "--root"],
                          capture_output=True, text=True)
    assert stop.returncode != 0, "the rebase must stop part way"
    listing = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                             capture_output=True, check=True).stdout
    assert b"branch refs/heads/merged-br" not in listing, "precondition: listed detached"
    assert wl._recover("wt_branch_merged", repo, {}) is True
    assert "DETACHED" in capsys.readouterr().err
    head = subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"],
                          capture_output=True, text=True)
    assert head.returncode != 0, "a second checkout of a branch being rebased elsewhere"
    assert (wt / "a.txt").read_text() == "DIRTY EDIT\n"


def test_recover_survives_a_strict_stdout_with_a_non_utf8_name(
    reaper_repo, tmp_path, monkeypatch,
):
    """Under a locale whose stdout is strict, printing a non-UTF-8 path raised after
    the tree had moved. pytest's own capture hides this, so stdout is replaced with a
    strict stream and `main()` is driven directly (review of round 2)."""
    import io

    repo = reaper_repo.repo
    wt = tmp_path / os.fsdecode(b"wt-raw-\xfe")
    _git(repo, "worktree", "add", "-q", "-b", "raw-br3", str(wt), reaper_repo.c0)
    (wt / "a.txt").write_text("EDIT\n")
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "LOG_DIR", trash / "logs")
    assert wl._trash_worktree(_wt_by_path(repo, wt), repo) is True
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py", "--recover", "wt-raw-"])
    raw = io.BytesIO()
    strict = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", strict)
    assert wl.main() == 0
    strict.flush()
    assert b"wt-raw-" in raw.getvalue()


def test_a_locked_non_utf8_worktree_reads_as_locked(reaper_repo, tmp_path):
    """The act-time lock re-read decoded the `.git` pointer lossily, so a non-UTF-8
    admin path named no directory and the lock read as absent: fail-open."""
    repo = reaper_repo.repo
    wt = tmp_path / os.fsdecode(b"wt-lock-\xfd")
    _git(repo, "worktree", "add", "-q", "-b", "lock-br", str(wt), reaper_repo.c0)
    _git(repo, "worktree", "lock", "--reason", "mine", str(wt))
    assert wl._is_locked_now(wt) is True


def test_an_inherited_git_dir_cannot_hide_an_operation_in_progress(
    reaper_repo, tmp_path, monkeypatch,
):
    """`_has_in_progress_op` ran git without the scrubbed environment, so an inherited
    GIT_DIR pointed `--git-path` at another repository."""
    wt = reaper_repo.wt_branch_merged
    marker = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip())
    marker.write_text(_git(wt, "rev-parse", "HEAD"))
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    assert wl._has_in_progress_op(str(wt)) is True


# ─── full-diff audit of the whole change ─────────────────────────────────────


def _edit_meta(entry: Path, **changes) -> None:
    meta_file = entry / ".trash_meta.json"
    meta = json.loads(meta_file.read_text())
    for key, value in changes.items():
        if value is _DROP:
            meta.pop(key, None)
        else:
            meta[key] = value
    meta_file.write_text(json.dumps(meta))


_DROP = object()


def test_a_second_recovery_of_a_kept_archive_is_not_called_exact(
    reaper_repo, tmp_path, monkeypatch, capsys,
):
    """A recovery unlocks the registration while a compressed archive is kept. A
    second recovery of that archive then found the registration unlocked, its index
    changed by later work, and still reported an exact restore (MEASURED)."""
    repo = reaper_repo.repo
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    assert wl._recover("wt_branch_merged", repo, {}) is True
    (wt / "later.txt").write_text("later\n")
    _git(wt, "add", "later.txt")
    shutil.rmtree(wt)
    capsys.readouterr()
    report: dict = {}
    assert wl._recover("wt_branch_merged", repo, report) is True
    out, err = capsys.readouterr()
    assert report.get("incomplete") is True, report
    assert "exactly as archived" not in out
    assert "not held by this archive" in err


def test_a_rebuild_onto_a_moved_branch_is_incomplete(reaper_repo, tmp_path, monkeypatch, capsys):
    """With no patch to conflict, a rebuild onto a branch that moved exited 0 and
    deleted the legacy entry, though the tree no longer sits on the archived commit."""
    _disable_compression(monkeypatch)
    repo = reaper_repo.repo
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(repo, wt)
    mover = tmp_path / "mover"
    _git(repo, "worktree", "add", "-q", str(mover), "merged-br")
    (mover / "new.txt").write_text("new\n")
    _git(mover, "add", "new.txt")
    _git(mover, "commit", "-qm", "move the branch")
    _git(repo, "worktree", "remove", "--force", str(mover))
    report: dict = {}
    assert wl._recover("wt_branch_merged", repo, report) is True
    assert report.get("incomplete") is True, report
    assert "MOVED" in capsys.readouterr().err


def test_a_rebuild_whose_commit_is_gone_still_uses_the_branch(reaper_repo, tmp_path, monkeypatch):
    """Starting from a recorded commit that no longer exists left a plain directory
    even though the branch was still there (main used the branch)."""
    _disable_compression(monkeypatch)
    repo = reaper_repo.repo
    wt = _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    _drop_registration(repo, wt)
    _edit_meta(_only_entry(tmp_path), commit="0123456789abcdef0123456789abcdef01234567")
    wl._recover("wt_branch_merged", repo, {})
    status = subprocess.run(["git", "-C", str(wt), "status", "--short"], capture_output=True)
    assert status.returncode == 0, "recovered as a plain directory, not a worktree"
    assert _git(wt, "symbolic-ref", "--short", "HEAD").strip() == "merged-br"


@pytest.mark.parametrize(
    "shape",
    ["legacy-unnamed", "named-format-lost-name"],
)
def test_a_reattach_that_may_leave_the_reapers_patch_is_incomplete(
    reaper_repo, tmp_path, monkeypatch, capsys, shape,
):
    """The reaper's patch can stay in the tree when its name was never recorded
    (older archives) or was lost (the final metadata rewrite failed); a later
    `git add -A` would commit it, so the restore is not exact."""
    _disable_compression(monkeypatch)
    _trash_dirty(reaper_repo, tmp_path, monkeypatch)
    entry = _only_entry(tmp_path)
    if shape == "legacy-unnamed":
        _edit_meta(entry, patch_format=_DROP, patch_file=_DROP, index_patch_file=_DROP,
                   had_tracked_patch=True)
    else:
        _edit_meta(entry, patch_format=2, patch_file=None, patch_expected=True)
    report: dict = {}
    assert wl._recover("wt_branch_merged", reaper_repo.repo, report) is True
    assert report.get("mode") == "reattached", report
    assert report.get("incomplete") is True, report
    assert "exactly as archived" not in capsys.readouterr().out


def test_an_archive_without_the_patch_flag_never_had_a_reaper_patch(tmp_path):
    """The flag and the reaper's patch arrived together, so an archive without the
    flag holds no patch of ours: a `.dirty.patch` in it is the user's file."""
    (tmp_path / ".dirty.patch").write_text("the user's own file\n")
    assert wl._saved_patches_in(tmp_path, {}) == (None, None, False)


def test_a_failed_copy_never_removes_a_directory_it_did_not_make(tmp_path, monkeypatch):
    """Across filesystems the tree is copied; if something appeared at the
    destination meanwhile, the failed copy removed it as its own partial copy."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("archived\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "theirs.txt").write_text("somebody else's\n")

    def cross_fs(_a, _b):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(wl.os, "rename", cross_fs)
    with pytest.raises(FileExistsError):
        wl._place_tree(src, dest)
    assert (dest / "theirs.txt").read_text() == "somebody else's\n"


def test_worktree_listing_ignores_an_inherited_git_dir(reaper_repo, tmp_path, monkeypatch):
    """The listing ran git without the scrubbed environment, so an exported GIT_DIR
    made the reaper enumerate ANOTHER repository's worktrees."""
    elsewhere = tmp_path / "elsewhere_repo"
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    paths = {Path(w["path"]) for w in wl._list_worktrees(reaper_repo.repo)}
    assert reaper_repo.wt_branch_merged in paths
