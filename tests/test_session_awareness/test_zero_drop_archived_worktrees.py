"""The zero-drop sweep resolves a reaper ARCHIVE only on positive evidence.

The sweep HOLDS a worktree whose directory it cannot reach, and that design is
correct: `prunable`, and a failing `status`, are byte-identical between a
DELETED directory and an UNREACHABLE one (moved aside, an unmounted volume), so
resolving would destroy the acknowledgement of work that may still exist.

The reaper's archive anchor fell into that hold by accident. Locking the
registration CLEARS the `prunable` marker, so an archived worktree reached
`git status`, failed rc=128 and was held as "unreadable" for ever.

These tests use REAL git worktrees, and the last one drives the REAL reaper
(`scripts/worktree_lifecycle.py`) into the REAL sweep, so the lock-reason format
is pinned at the seam where the two components meet rather than restated here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from genesis.session_awareness import zero_drop_worker as w
from genesis.session_awareness.zero_drop import worktree_identity
from tests.conftest import private_module

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worktree_lifecycle.py"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "T")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-qm", "seed")
    return r


def _archive_by_hand(repo: Path, wt: Path, trash: Path, entry: str, *, tarball: bool = True):
    """The reaper's end state: directory gone, registration locked with the
    archive marker naming `entry`, and (optionally) the tarball in the trash."""
    _git(repo, "worktree", "add", "-q", "-b", wt.name, str(wt))
    (wt / "dirty.txt").write_text("uncommitted\n")
    shutil.rmtree(wt)
    _git(
        repo,
        "worktree",
        "lock",
        "--reason",
        f"archived by the reaper -> {entry}; recover with --recover",
        str(wt),
    )
    trash.mkdir(parents=True, exist_ok=True)
    if tarball:
        (trash / f"{entry}.tar.gz").write_bytes(b"\x1f\x8b not inspected by the sweep")


async def test_an_archived_registration_is_resolved_not_held(repo, tmp_path):
    trash = tmp_path / "trash"
    wt = tmp_path / "arch"
    _archive_by_hand(repo, wt, trash, "arch-20260101")

    listing = await w.list_worktrees(str(repo))
    entry = next(x for x in listing["worktrees"] if x["path"] == str(wt))
    # The premise, asserted: locking cleared `prunable`, which is WHY the old
    # sweep fell through to `status` and held it.
    assert not entry["prunable"], "git now marks a locked missing worktree prunable"

    out = await w._observe_worktrees(str(repo), budget_s=60, trash_dir=trash)
    assert out["archived"] == 1
    assert worktree_identity(entry) not in out["held"], "an archive must not be held"
    assert out["errors"] == [], "an archive must not read as unreadable"
    assert out["total"] == 2


async def test_NEGATIVE_CONTROL_a_moved_aside_directory_with_no_tarball_stays_held(repo, tmp_path):
    """The arm that proves the discriminator did not just re-implement the
    failure the cross-model reviewer demonstrated: a directory moved aside is
    marked prunable exactly like a deleted one, and it must stay HELD."""
    trash = tmp_path / "trash"
    trash.mkdir()
    wt = tmp_path / "aside"
    _git(repo, "worktree", "add", "-q", "-b", "aside", str(wt))
    (wt / "work.txt").write_text("still here\n")
    os.rename(wt, tmp_path / "aside.moved")

    out = await w._observe_worktrees(str(repo), budget_s=60, trash_dir=trash)
    listing = await w.list_worktrees(str(repo))
    entry = next(x for x in listing["worktrees"] if x["path"] == str(wt))
    assert out["archived"] == 0
    assert worktree_identity(entry) in out["held"]


async def test_the_marker_lock_WITHOUT_its_tarball_stays_held(repo, tmp_path):
    """The lock alone is text anybody can write. Without the archive it names,
    it is not evidence — an unmounted trash volume looks exactly like this."""
    trash = tmp_path / "trash"
    wt = tmp_path / "notar"
    _archive_by_hand(repo, wt, trash, "notar-20260101", tarball=False)

    out = await w._observe_worktrees(str(repo), budget_s=60, trash_dir=trash)
    listing = await w.list_worktrees(str(repo))
    entry = next(x for x in listing["worktrees"] if x["path"] == str(wt))
    assert out["archived"] == 0
    assert worktree_identity(entry) in out["held"]


async def test_a_tarball_named_WITH_its_extension_in_the_reason_is_not_matched(repo, tmp_path):
    """The reason names the entry WITHOUT `.tar.gz`; the lookup appends it.
    A reader that compared verbatim reported every archive missing."""
    trash = tmp_path / "trash"
    wt = tmp_path / "ext"
    _archive_by_hand(repo, wt, trash, "ext-20260101")
    listing = await w.list_worktrees(str(repo))
    entry = next(x for x in listing["worktrees"] if x["path"] == str(wt))
    assert w._is_reaper_archive(entry, trash) is True
    assert (
        w._is_reaper_archive(
            {**entry, "locked": entry["locked"].replace("ext-20260101", "ext-20260101.tar.gz")},
            trash,
        )
        is False
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode-000 directory")
async def test_an_UNREADABLE_path_is_not_read_as_absent(repo, tmp_path):
    """`os.path.lexists` answers False on EACCES, so a present tree under an
    unreadable parent read as "gone" and its findings were resolved. Only an
    lstat that reports ENOENT/ENOTDIR counts as absent; anything else holds."""
    trash = tmp_path / "trash"
    parent = tmp_path / "parent"
    parent.mkdir()
    wt = parent / "unreadable"
    _archive_by_hand(repo, wt, trash, "unreadable-20260101")
    listing = await w.list_worktrees(str(repo))
    entry = next(x for x in listing["worktrees"] if x["path"] == str(wt))
    # Positive control: while the path is genuinely absent this IS an archive.
    assert w._is_reaper_archive(entry, trash) is True
    wt.mkdir()
    parent.chmod(0)
    try:
        with pytest.raises(PermissionError):
            os.lstat(wt)  # guard-the-guard: the fixture really is unreadable
        assert os.path.lexists(wt) is False, "fixture: lexists must be fooled here"
        assert w._is_reaper_archive(entry, trash) is False
    finally:
        parent.chmod(0o755)


async def test_a_present_directory_is_observed_even_under_an_archive_lock(repo, tmp_path):
    """A lock does not make live, readable work disappear."""
    trash = tmp_path / "trash"
    trash.mkdir()
    wt = tmp_path / "live"
    _git(repo, "worktree", "add", "-q", "-b", "live", str(wt))
    _git(
        repo,
        "worktree",
        "lock",
        "--reason",
        "archived by the reaper -> live-20260101; recover with --recover",
        str(wt),
    )
    (trash / "live-20260101.tar.gz").write_bytes(b"x")

    out = await w._observe_worktrees(str(repo), budget_s=60, trash_dir=trash)
    assert out["archived"] == 0
    assert any(o["path"] == str(wt) for o in out["observations"])


@pytest.mark.parametrize(
    "entry",
    ["../escape", "sub/dir", "..", ".", ""],
)
def test_the_reason_cannot_steer_the_lookup_outside_the_trash(tmp_path, entry):
    trash = tmp_path / "trash"
    trash.mkdir()
    # Plant a file where a traversal WOULD resolve, so a missing guard passes.
    (tmp_path / "escape.tar.gz").write_bytes(b"x")
    (trash / "sub").mkdir()
    (trash / "sub" / "dir.tar.gz").write_bytes(b"x")
    (trash / ".tar.gz").write_bytes(b"x")
    wt = {"path": str(tmp_path / "gone"), "locked": f"archived by the reaper -> {entry}; recover with --recover"}
    assert w._is_reaper_archive(wt, trash) is False


def test_a_foreign_lock_is_never_an_archive(tmp_path):
    trash = tmp_path / "trash"
    trash.mkdir()
    (trash / "x.tar.gz").write_bytes(b"x")
    for reason in (
        None,
        "",
        "do not touch",
        "claude agent agent-1 (pid 1 start 2)",
        "x; archived by the reaper -> x",
    ):
        assert (
            w._is_reaper_archive({"path": str(tmp_path / "gone"), "locked": reason}, trash) is False
        )


async def test_CONTRACT_the_real_reaper_output_is_recognised_by_the_real_sweep(
    repo, tmp_path, monkeypatch
):
    """Producer -> consumer, with nothing typed by hand in the middle: the real
    `_trash_worktree` archives and locks, and the real `_observe_worktrees`
    must count it as an archive. A change to either spelling fails here."""
    wl = private_module("worktree_lifecycle_zd_contract", _SCRIPT)
    # Both suffixes (the recovery-failed relock too) and the prefix, as constants:
    # only the first suffix is exercised by the drive below.
    assert wl.ARCHIVE_LOCK_PREFIX == w.REAPER_ARCHIVE_LOCK_PREFIX
    assert wl.ARCHIVE_LOCK_SUFFIXES == w.REAPER_ARCHIVE_LOCK_SUFFIXES
    assert wl.ARCHIVE_SUFFIX == w.REAPER_ARCHIVE_SUFFIX
    trash = tmp_path / "trash"
    for name, value in (
        ("TRASH_DIR", trash),
        ("LOG_DIR", tmp_path / "logs"),
        ("TOMBSTONE_INDEX", tmp_path / "tomb.jsonl"),
        ("BOARD_CACHE", tmp_path / "board.json"),
    ):
        monkeypatch.setattr(wl, name, value)

    wt_path = tmp_path / "reaped"
    _git(repo, "worktree", "add", "-q", "-b", "reaped", str(wt_path))
    (wt_path / "untracked.txt").write_text("keep me\n")
    wt = next(x for x in wl._list_worktrees(repo) if x["path"] == str(wt_path))
    assert wl._trash_worktree(wt, repo, lane="unmerged") is True
    assert not wt_path.exists()
    assert list(trash.glob("*.tar.gz")), "the reaper did not produce a tarball"

    out = await w._observe_worktrees(str(repo), budget_s=60, trash_dir=trash)
    assert out["archived"] == 1, out
    assert out["errors"] == []
    assert out["held"] == set()
