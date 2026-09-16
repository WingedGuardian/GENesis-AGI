"""Regressions for the review P2s on the archiving reaper.

Grouped by MECHANISM rather than by line, because three of them are one thing
said three ways: ``--report-json`` promises a machine-readable document and a
non-mutating read, and three separate paths broke one half of that promise. The
rest are archive/recover correctness — each one a way the trash can hold work
that ``--list-trash`` or ``--recover`` cannot reach, which for a module
contracted to delete nothing is the same failure as deleting it.

Every case pairs the defect with a control that moves the other way.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worktree_lifecycle.py"
_spec = importlib.util.spec_from_file_location("worktree_lifecycle_p2", _SCRIPT)
wl = importlib.util.module_from_spec(_spec)
sys.modules["worktree_lifecycle_p2"] = wl
_spec.loader.exec_module(wl)


def _moved_worktree(trash: Path, prefix: str, unpack_to: Path) -> Path:
    """The archived worktree under ``trash`` whose name starts with ``prefix``.

    WHY THIS IS NOT ``next(trash.glob(prefix + "*"))``: the reaper writes TWO
    entries per worktree — ``<name>.tar.gz`` and the ``<name>.meta.json``
    sidecar — so that glob matches both and ``next()`` takes whichever the
    filesystem happens to yield first. Directory order is not defined and
    differs between filesystems, so the same test picks the archive on one box
    and the sidecar on another; opening the sidecar as a gzip stream fails with
    ``tarfile.ReadError: not a gzip file``.

    MEASURED: that is precisely how this suite failed in CI while passing
    locally — 150 local passes, one CI failure, on a run whose log truncated
    before the failure and named nothing. The production code already selects
    by suffix (``worktree_lifecycle.ARCHIVE_SUFFIX``); the tests did not.

    Returns the directory to inspect, unpacking the archive when the worktree
    was archived rather than left as a directory.
    """
    matches = sorted(trash.glob(prefix + "*"))
    assert matches, f"nothing under {trash} matches {prefix!r} — the reaper wrote nothing"
    directories = [m for m in matches if m.is_dir()]
    if directories:
        assert len(directories) == 1, f"ambiguous directories for {prefix!r}: {directories}"
        return directories[0]
    archives = [m for m in matches if m.name.endswith(wl.ARCHIVE_SUFFIX)]
    assert len(archives) == 1, (
        f"expected exactly one {wl.ARCHIVE_SUFFIX} for {prefix!r}, got {archives} "
        f"(all matches: {[m.name for m in matches]})"
    )
    with tarfile.open(archives[0], "r:gz") as tf:
        tf.extractall(unpack_to, filter="tar")
    inner = sorted(p for p in unpack_to.iterdir())
    assert len(inner) == 1, f"archive for {prefix!r} unpacked to {inner}"
    return inner[0]



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


# ─── the archive must not destroy what it is archiving ───────────────────────


def test_an_existing_dirty_patch_file_is_not_overwritten(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """`.dirty.patch` is not a reserved name, and the worktree may own one.

    The recovery patch used to be written unconditionally, so a worktree holding
    its own untracked `.dirty.patch` lost it — from the worktree AND from the
    archive, since the archive is made from the moved directory. For a module
    whose contract is that it deletes nothing, silently replacing a user's file
    IS the contract breaking.
    """
    wt = tmp_path / "wt-collide"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/collide", str(wt))
    (wt / ".dirty.patch").write_text("MY OWN FILE — not the reaper's\n")
    (wt / "README.md").write_text("a tracked modification\n")  # forces a patch

    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(wt), "branch": "feature/collide", "head": "", "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    moved = _moved_worktree(tmp_path / "trash", "wt-collide", tmp_path / "unpacked")

    original = (moved / ".dirty.patch").read_text()
    assert original == "MY OWN FILE — not the reaper's\n", (
        "the worktree's own .dirty.patch was replaced by the recovery patch"
    )
    alts = list(moved.glob(".dirty.patch.archived-*"))
    assert alts, "and the recovery patch must still have been saved, under another name"


def test_a_worktree_with_no_collision_still_gets_the_plain_name(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The control. Without it, always suffixing would pass the test above."""
    wt = tmp_path / "wt-plain"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/plain", str(wt))
    (wt / "README.md").write_text("a tracked modification\n")

    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash2")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb2.jsonl")
    entry = {"path": str(wt), "branch": "feature/plain", "head": "", "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    moved = _moved_worktree(tmp_path / "trash2", "wt-plain", tmp_path / "unpacked2")
    assert (moved / ".dirty.patch").exists()
    assert not list(moved.glob(".dirty.patch.archived-*"))


def test_an_untracked_only_worktree_is_recorded_as_uncommitted(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The tombstone is the durable greppable index, so a wrong value is wrong
    for as long as the archive lasts.

    `_dirty_patch` runs `git diff HEAD`, which sees TRACKED changes only — so a
    worktree whose only uncommitted content is an UNTRACKED file produced an
    empty patch and a tombstone claiming nothing was uncommitted. That is the
    wrong answer for precisely the case archives exist for: an untracked file is
    the one thing no branch and no commit protects.
    """
    wt = tmp_path / "wt-untracked"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/untracked", str(wt))
    (wt / "scratch-notes.md").write_text("unreferenced work\n")  # untracked only

    tomb = tmp_path / "tomb3.jsonl"
    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash3")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tomb)
    entry = {"path": str(wt), "branch": "feature/untracked", "head": "", "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    row = json.loads(tomb.read_text().strip().splitlines()[-1])
    assert row["had_uncommitted_changes"] is True, (
        "an untracked-only worktree was indexed as having nothing uncommitted"
    )
    assert row["had_tracked_patch"] is False, "and the patch field stays honest"


def test_a_pristine_worktree_is_recorded_as_clean(repo: Path, tmp_path: Path, monkeypatch) -> None:
    """The control: without it, hardcoding True would pass the test above."""
    wt = tmp_path / "wt-clean"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/clean", str(wt))
    tomb = tmp_path / "tomb4.jsonl"
    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash4")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tomb)
    entry = {"path": str(wt), "branch": "feature/clean", "head": "", "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    row = json.loads(tomb.read_text().strip().splitlines()[-1])
    assert row["had_uncommitted_changes"] is False


# ─── the trash must not hide what it holds ───────────────────────────────────


def test_a_dot_prefixed_archive_is_still_listed_and_recoverable(
    tmp_path: Path, monkeypatch
) -> None:
    """A blanket hidden-file filter hid real archives.

    A worktree whose basename legitimately starts with a dot is archived under a
    dot-prefixed name, and the old filter excluded both it and its `.tar.gz` —
    `--list-trash` omitted it and `_recover` reported no match while the archive
    sat there. Hiding a recoverable archive is the same class of failure as
    deleting it.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)

    hidden = trash / ".claude-scratch-2026-09-14"
    hidden.mkdir()
    (hidden / ".trash_meta.json").write_text("{}")

    names = [stored.name for stored, _ in wl._iter_trash_entries()]
    assert ".claude-scratch-2026-09-14" in names

    # CONTROL: our OWN sidecar/staging artifacts must still be skipped, or the
    # listing fills with metadata files that are not entries.
    (trash / "something.meta.json").write_text("{}")
    (trash / ".something.meta.staging").write_text("{}")
    names = [stored.name for stored, _ in wl._iter_trash_entries()]
    assert "something.meta.json" not in names
    assert ".something.meta.staging" not in names


def test_uncompressed_entries_count_toward_the_trash_total(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`0 MB archived` while gigabytes sit in the trash is worst exactly when it
    is most likely: compression failing under storage pressure, which is when
    the number is being read to decide whether there is a problem."""
    trash = tmp_path / "trash-size"
    trash.mkdir()
    monkeypatch.setattr(wl, "TRASH_DIR", trash)

    entry = trash / "leftover-2026-09-14"
    entry.mkdir()
    (entry / ".trash_meta.json").write_text(json.dumps({"lane": "merged", "branch": "x"}))
    (entry / "payload.bin").write_bytes(b"\0" * (3 * 1024 * 1024))  # 3 MB

    wl._list_trash()
    out = capsys.readouterr().out
    assert "0 MB archived" not in out, "an uncompressed entry was counted as zero bytes"
    assert "3 MB archived" in out or "3.0M" in out


# ─── recovery must not refuse what it is designed to handle ──────────────────


def test_both_link_filter_errors_are_handled_not_just_absolute(tmp_path: Path) -> None:
    """`data_filter` raises two different exceptions for two link shapes.

    AbsoluteLinkError for `/abs/target`, LinkOutsideDestinationError for a
    RELATIVE escape like `../../shared/secrets.env`. Catching only the first let
    the second reach the outer handler and fail the whole recovery — even though
    the fallback exists precisely to recreate such links safely.

    Asserted against tarfile itself, because the claim is about which exception
    the stdlib raises, not about our handling of an exception we invented.
    """
    assert issubclass(tarfile.AbsoluteLinkError, tarfile.FilterError)
    assert issubclass(tarfile.LinkOutsideDestinationError, tarfile.FilterError)
    assert tarfile.AbsoluteLinkError is not tarfile.LinkOutsideDestinationError

    src = _SCRIPT.read_text()
    idx = src.find("except (tarfile.AbsoluteLinkError")
    assert idx != -1, "the recover fallback must catch both link-filter errors"
    assert "LinkOutsideDestinationError" in src[idx : idx + 200]
