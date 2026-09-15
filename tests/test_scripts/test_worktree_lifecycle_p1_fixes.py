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
import json
import os
import subprocess
import sys
import tarfile
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


def test_an_archived_worktree_survives_branch_deletion_and_a_gc(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """THE invariant the archive exists for, asserted against the worst case.

    This replaces three tests that asserted a tag anchor. The tag is gone — the
    archive now carries its own commit graph — but the property it protected is
    unchanged and is what is locked here: after the executor deletes the branch
    (`git branch -D`, which git performs even when unmerged) and a gc collects
    the now-unreferenced commit, the archived work must still be recoverable.

    The sequence is REPLAYED rather than simulated, and deliberately total:
    delete the branch, prune, expire every reflog, `gc --prune=now`, then assert
    with `cat-file -e` that the commit is genuinely GONE before recovering it.
    Without that precondition this would pass against a repository that simply
    still had the commit, which is the failure mode of every "recovery worked"
    assertion.
    """
    wt = tmp_path / "wt-doomed"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/doomed", str(wt))
    (wt / "only-copy.txt").write_text("the sole record of this work\n")
    _git(wt, "add", "only-copy.txt")
    _git(wt, "commit", "--quiet", "-m", "work that exists nowhere else")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert sha

    trash = tmp_path / "trash-doomed"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb-doomed.jsonl")
    entry = {"path": str(wt), "branch": "feature/doomed", "head": sha, "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    # Destroy every ref to the commit, exactly as the executor plus routine gc do.
    _git(repo, "worktree", "prune")
    assert _git(repo, "branch", "-D", "feature/doomed").returncode == 0
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now", "--quiet")
    assert _git(repo, "cat-file", "-e", sha).returncode != 0, (
        "precondition: the commit must be genuinely collected, or this test "
        "proves nothing about recovery"
    )

    # Select the ARCHIVE explicitly. `next(trash.iterdir())` is ordered by the
    # filesystem, and the entry also has a sidecar `.meta.json` beside it — so
    # this picked the sidecar in CI and the tarball locally, which is a flaky
    # test rather than a flaky product. Caught by CI, not by me.
    name = next(trash.glob("*.tar.gz")).name[: -len(".tar.gz")]
    assert wl._recover(name, repo) is True

    assert _git(repo, "cat-file", "-e", sha).returncode == 0, (
        "the archived commit was not restored — the archive held files whose "
        "history no longer exists anywhere"
    )
    restored = Path(str(wt))
    status = _git(restored, "status", "--porcelain")
    assert status.returncode == 0, f"recovered tree is not usable: {status.stderr.strip()}"
    assert (restored / "only-copy.txt").read_text() == "the sole record of this work\n"


def test_the_archive_carries_its_history_rather_than_pointing_at_it(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The structural half: the history must be INSIDE the archive.

    The previous design left the commits reachable only through a ref outside the
    archive, and every finding in that cluster was a different way for that
    external link to break while the archive itself stayed perfectly intact. So
    this asserts the bundle is a member of the tarball — and that NO tag was
    created, because the point is that the dependency is gone rather than merely
    unused.
    """
    wt = tmp_path / "wt-selfcontained"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/selfcontained", str(wt))
    (wt / "f.txt").write_text("x\n")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "--quiet", "-m", "unique work")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    trash = tmp_path / "trash-sc"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb-sc.jsonl")
    entry = {
        "path": str(wt),
        "branch": "feature/selfcontained",
        "head": sha,
        "detached": False,
    }
    assert wl._trash_worktree(entry, repo) is True

    archive = next(trash.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getnames()
    assert any(m.endswith(wl._HISTORY_BUNDLE) for m in members), (
        f"the archive does not contain {wl._HISTORY_BUNDLE}; its history lives "
        f"outside it again. members sampled: {members[:8]}"
    )
    tags = _git(repo, "for-each-ref", "--format=%(refname)", "refs/tags/").stdout
    assert "worktree-archive/" not in tags, (
        "a tag anchor was still created — the dependency this replaced is back"
    )


def test_the_recorded_commit_is_head_at_ARCHIVE_time_not_classification_time(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """A commit made between the scan and the move must still be preserved.

    Classification runs over every worktree up front (MEASURED at 19-41s across
    ~191), so the snapshot HEAD can be minutes old and a session can commit in
    that window. Passing a deliberately STALE head in the entry is what makes
    this a real test: the reaper must ignore it and re-read the worktree.
    """
    wt = tmp_path / "wt-moving"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/moving", str(wt))
    (wt / "a.txt").write_text("first\n")
    _git(wt, "add", "a.txt")
    _git(wt, "commit", "--quiet", "-m", "A")
    stale = _git(wt, "rev-parse", "HEAD").stdout.strip()

    (wt / "b.txt").write_text("second\n")
    _git(wt, "add", "b.txt")
    _git(wt, "commit", "--quiet", "-m", "B, committed after classification")
    fresh = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert stale != fresh

    trash = tmp_path / "trash-moving"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb-moving.jsonl")
    entry = {"path": str(wt), "branch": "feature/moving", "head": stale, "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    archive = next(trash.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as tf:
        member = next(m for m in tf.getnames() if m.endswith(".trash_meta.json"))
        meta = json.loads(tf.extractfile(member).read())
    assert meta["commit"] == fresh, (
        f"metadata recorded {meta['commit'][:8]} (the classification snapshot) "
        f"rather than {fresh[:8]} (HEAD at archive time), so recovery would land "
        "on a commit this archive never bundled"
    )
