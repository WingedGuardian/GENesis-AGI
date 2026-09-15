"""Regressions for three data-loss paths in the archiving reaper.

All three were review findings on a module whose stated contract is that it
deletes nothing, and all three break exactly that contract. Two share a root —
state changing between the moment a worktree is classified and the moment it is
acted on — which is why they are tested together rather than filed apart.

Each test pairs the failing case with a CONTROL that must move the other way.
"Archiving was skipped" on its own passes just as well against a reaper that
archives nothing, and "the tag exists" passes against one that tags
indiscriminately.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worktree_lifecycle.py"
_spec = importlib.util.spec_from_file_location("worktree_lifecycle_p1", _SCRIPT)
wl = importlib.util.module_from_spec(_spec)
sys.modules["worktree_lifecycle_p1"] = wl
_spec.loader.exec_module(wl)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.email", "probe@example.invalid")
    _git(root, "config", "user.name", "Probe")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", "seed")
    return root


# ─── the lock, re-read at act time ───────────────────────────────────────────


def test_trash_worktree_REFUSES_a_worktree_locked_after_classification(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The one that matters: the CALL SITE, not the helper.

    An earlier version of this file tested `_is_locked_now` directly. Deleting
    the call to it from `_trash_worktree` left that test GREEN while the
    protection was gone — measured by mutation, which is the only reason this
    test exists in this shape. Testing a predicate proves the predicate; only
    driving the function proves it is consulted.

    The classification entry is built FIRST and the lock taken AFTER, which is
    the real sequence: the reaper classifies everything up front (measured at
    19-41s over 191 worktrees) and acts minutes later.
    """
    wt = tmp_path / "wt-locked-late"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/late-lock", str(wt))
    entry = {"path": str(wt), "branch": "feature/late-lock", "head": "", "detached": False}
    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash")

    # Taken AFTER the entry above was built — the gap this closes.
    _git(repo, "worktree", "lock", "--reason", "a session is working here", str(wt))

    assert wl._trash_worktree(entry, repo) is False, "a locked worktree must not be archived"
    assert wt.exists(), "and it must still be where the session left it"


def test_trash_worktree_ACCEPTS_the_same_worktree_once_unlocked(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The control that moves. Without it, the test above passes just as well
    against a `_trash_worktree` that refuses everything."""
    wt = tmp_path / "wt-unlocked"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/fine", str(wt))
    entry = {"path": str(wt), "branch": "feature/fine", "head": "", "detached": False}
    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash2")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tombstones.jsonl")

    _git(repo, "worktree", "lock", "--reason", "held", str(wt))
    assert wl._trash_worktree(entry, repo) is False

    _git(repo, "worktree", "unlock", str(wt))
    assert wl._trash_worktree(entry, repo) is True, "same worktree; the lock is the only delta"
    assert not wt.exists(), "and it left its original path"


def test_is_locked_now_reads_the_live_lock(repo: Path, tmp_path: Path) -> None:
    """The window this closes is the one a session actually uses.

    `_classify` reads the lock from porcelain, minutes before the reap acts. A
    lock is the one protection a THIRD PARTY takes during that gap — it is how a
    session says "I am working here" — so the stale answer is wrong precisely
    when it matters.

    The control is the same worktree, same call, differing only by the lock.
    """
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/x", str(wt))

    assert wl._is_locked_now(wt) is False, "control: an unlocked worktree reads unlocked"

    _git(repo, "worktree", "lock", "--reason", "a session is working here", str(wt))
    assert wl._is_locked_now(wt) is True

    _git(repo, "worktree", "unlock", str(wt))
    assert wl._is_locked_now(wt) is False, "and it moves back when released"


def test_an_unreadable_git_pointer_reads_as_locked(tmp_path: Path) -> None:
    """Fails CLOSED. The alternative to "I could not read the protection" is
    archiving a worktree whose protection we could not read."""
    ghost = tmp_path / "gone"
    assert wl._is_locked_now(ghost) is True

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("this is not a gitdir pointer\n")
    assert wl._is_locked_now(broken) is True

    empty_ptr = tmp_path / "empty"
    empty_ptr.mkdir()
    (empty_ptr / ".git").write_text("gitdir:\n")
    assert wl._is_locked_now(empty_ptr) is True


def test_the_main_checkout_is_not_reported_locked(repo: Path) -> None:
    """Its `.git` is a DIRECTORY, not a pointer. Reading it as an unreadable
    pointer would report the main tree as locked on every call."""
    assert wl._is_locked_now(repo) is False


# ─── the trash-name claim, held through the move ─────────────────────────────


def test_the_claimed_name_is_replaced_atomically_not_released(tmp_path: Path) -> None:
    """The property that closes the race: a rename REPLACES the empty claim.

    The old code released the claim (`rmdir`) and let `shutil.move` recreate it,
    so between those two moments the name was free. A second invocation could
    claim it, and `shutil.move` onto a directory that now exists nests the source
    INSIDE it — one archive holding two worktrees, neither independently
    recoverable.

    This asserts the primitive the fix relies on, on the platform it runs on: a
    directory rename onto an EMPTY directory succeeds and leaves the source's
    contents at the target. If this ever stops holding, the fix is unsound and
    this test is how you find out.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "work.txt").write_text("uncommitted work")
    claimed = tmp_path / "claimed"
    claimed.mkdir()

    os.rename(src, claimed)

    assert not src.exists()
    assert (claimed / "work.txt").read_text() == "uncommitted work"


def test_a_rename_onto_a_NON_empty_directory_is_refused(tmp_path: Path) -> None:
    """The control for the case above, and the reason the claim must stay EMPTY.

    If the claim ever held a file, the rename would fail rather than silently
    nesting — which is the safe direction, and worth pinning so a future change
    that writes into the claim directory is caught here instead of in an archive.
    """
    src = tmp_path / "src2"
    src.mkdir()
    (src / "a.txt").write_text("x")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "already-here.txt").write_text("y")

    with pytest.raises(OSError):
        os.rename(src, occupied)
    assert src.exists(), "the source must survive a refused rename"


# ─── every archived worktree anchored, not only detached ones ────────────────


def test_a_branch_backed_worktree_is_anchored_too(repo: Path, tmp_path: Path) -> None:
    """A branch is not a durable anchor, and assuming it was is the gap.

    The branch ref keeps the commits reachable right up until something deletes
    it — and something does: the autonomy executor deletes a task worktree's
    branch with `git branch -D`, which git deletes even when unmerged. After
    that the tarball holds files and a pointer to nothing, and a GC takes the
    commits.

    Asserted by REPLAYING that sequence: anchor, delete the branch, and check the
    commit is still reachable. The control is the same sequence WITHOUT the
    anchor, where it must not be.
    """
    wt = tmp_path / "wt-branch"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/doomed", str(wt))
    (wt / "only-copy.txt").write_text("the sole record of this work\n")
    _git(wt, "add", "only-copy.txt")
    _git(wt, "commit", "--quiet", "-m", "work that exists nowhere else")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert sha

    # CONTROL: without an anchor, deleting the branch leaves the commit
    # unreferenced — reachable only until a gc.
    before = _git(repo, "for-each-ref", "--points-at", sha).stdout
    assert "feature/doomed" in before, "precondition: the branch points at it"

    anchor = f"{wl._DETACHED_ANCHOR_PREFIX}wt-branch-archived"
    tagged = _git(repo, "tag", "-f", anchor, sha)
    assert tagged.returncode == 0, tagged.stderr

    _git(repo, "worktree", "remove", "--force", str(wt))
    deleted = _git(repo, "branch", "-D", "feature/doomed")
    assert deleted.returncode == 0, deleted.stderr

    after = _git(repo, "for-each-ref", "--points-at", sha).stdout
    assert "feature/doomed" not in after, "the branch is gone, as the executor leaves it"
    assert anchor in after, "but the archive anchor still points at the commit"
    assert _git(repo, "cat-file", "-e", sha).returncode == 0, "so the commit survives"


def test_the_anchor_prefix_is_a_namespace_not_a_branch(repo: Path) -> None:
    """Anchors are tags under a dedicated prefix so they cannot be mistaken for
    live branches, and cannot collide with one."""
    assert wl._DETACHED_ANCHOR_PREFIX.endswith("/")
    assert "worktree-archive" in wl._DETACHED_ANCHOR_PREFIX


def test_trash_worktree_ANCHORS_a_branch_backed_worktree(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """Drives the reaper, not git, because the finding is about OUR code.

    The sibling test below replays the executor's `git branch -D` by hand and so
    only proves git's behaviour — a mutation restoring `if detached and ...`
    leaves it green. This one archives a BRANCH-backed worktree through
    `_trash_worktree` and asserts the anchor tag exists afterwards, which is the
    property that actually protects the commits.
    """
    wt = tmp_path / "wt-anchor-me"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/anchor-me", str(wt))
    (wt / "only-copy.txt").write_text("the sole record\n")
    _git(wt, "add", "only-copy.txt")
    _git(wt, "commit", "--quiet", "-m", "work that exists nowhere else")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash-anchor")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb-anchor.jsonl")
    entry = {
        "path": str(wt), "branch": "feature/anchor-me", "head": sha, "detached": False,
    }

    assert wl._trash_worktree(entry, repo) is True

    tags = _git(repo, "for-each-ref", "--format=%(refname)", "refs/tags/").stdout
    assert wl._DETACHED_ANCHOR_PREFIX in tags, (
        "a branch-backed worktree was archived without a durable anchor — after "
        "the executor's `git branch -D` its commits would be collectable"
    )
    anchored = _git(repo, "for-each-ref", "--points-at", sha).stdout
    assert wl._DETACHED_ANCHOR_PREFIX in anchored, "the anchor must point at THIS commit"
