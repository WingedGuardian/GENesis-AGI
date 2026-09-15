"""Regressions for the third review round on the archiving reaper.

Six findings, grouped by what actually breaks rather than by file position:

  * **Confidentiality** — a reaped worktree is a verbatim copy of someone's
    working tree and routinely holds a 0600 `.env` or key. Archiving it under a
    normal umask republished it at 0644 inside a 0755 directory.
  * **Durability** — the archive was verified through the page cache and the
    source deleted without ever forcing either the data or the rename to disk.
  * **Reachability** — two ways the anchor that keeps archived commits alive
    could be absent (a ref-illegal tag name) or unusable (a deleted branch on
    recovery), plus a nested worktree the parent's anchor does not cover.
  * **Honesty of the board** — a failed enumeration was indistinguishable from
    an empty one, so a broken scan published "no worktrees exist".

Each case pairs the defect with a control that moves the other way, because
every one of these is a property where asserting only the good case would pass
against code that does nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worktree_lifecycle.py"
_spec = importlib.util.spec_from_file_location("worktree_lifecycle_r3", _SCRIPT)
wl = importlib.util.module_from_spec(_spec)
sys.modules["worktree_lifecycle_r3"] = wl
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


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# ─── confidentiality ─────────────────────────────────────────────────────────


def test_the_archive_and_its_directory_are_private(repo: Path, tmp_path: Path, monkeypatch) -> None:
    """A worktree's secrets must not become world-readable by being archived.

    Driven under an explicitly LAX umask (022), which is the whole point: the
    old code created the tarball with a bare `tarfile.open` and the directory
    with a bare `mkdir`, so both inherited whatever the umask allowed. Setting
    the umask here is what makes this a real test rather than one that passes
    because the CI process happened to run at 077.
    """
    old_umask = os.umask(0o022)
    try:
        wt = tmp_path / "wt-secrets"
        _git(repo, "worktree", "add", "--quiet", "-b", "feature/secrets", str(wt))
        secret = wt / ".env"
        secret.write_text("API_KEY=not-a-real-key\n")
        secret.chmod(0o600)

        trash = tmp_path / "trash"
        monkeypatch.setattr(wl, "TRASH_DIR", trash)
        monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
        entry = {"path": str(wt), "branch": "feature/secrets", "head": "", "detached": False}
        assert wl._trash_worktree(entry, repo) is True

        assert _mode(trash) == 0o700, (
            f"trash dir is {oct(_mode(trash))}; a 0755 directory lets any local "
            "account list and read the archives inside it"
        )
        archives = list(trash.glob("*.tar.gz"))
        assert archives, "precondition: the entry was archived"
        for a in archives:
            assert _mode(a) == 0o600, (
                f"{a.name} is {oct(_mode(a))}; the archive contains a 0600 .env "
                "and must be no more readable than its contents"
            )
    finally:
        os.umask(old_umask)


def test_the_recovery_patch_is_private_even_when_compression_fails(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The patch is the case that ESCAPES the archive, so its own mode matters.

    When compression fails the entry stays an uncompressed directory, and the
    `.dirty.patch` — a verbatim diff of uncommitted work, which can hold a
    secret that was staged but never committed — sits in the open. Compression
    is forced to fail here so the patch is examined where it is actually exposed.
    """
    old_umask = os.umask(0o022)
    try:
        wt = tmp_path / "wt-patch"
        _git(repo, "worktree", "add", "--quiet", "-b", "feature/patch", str(wt))
        (wt / "README.md").write_text("an uncommitted tracked modification\n")

        trash = tmp_path / "trash"
        monkeypatch.setattr(wl, "TRASH_DIR", trash)
        monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
        monkeypatch.setattr(
            wl,
            "_compress_entry",
            lambda *a, **k: None,  # compression "fails"
        )
        entry = {"path": str(wt), "branch": "feature/patch", "head": "", "detached": False}
        assert wl._trash_worktree(entry, repo) is True

        moved = next(trash.glob("wt-patch*"))
        assert moved.is_dir(), "precondition: this entry stayed uncompressed"
        patch = moved / ".dirty.patch"
        assert patch.exists(), "precondition: a recovery patch was written"
        assert _mode(patch) == 0o600, (
            f"the recovery patch is {oct(_mode(patch))} and holds uncommitted diff content"
        )
    finally:
        os.umask(old_umask)


# ─── durability ──────────────────────────────────────────────────────────────


def test_the_archive_is_synced_before_the_source_is_destroyed(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """Order, not merely presence: fsync must happen BEFORE the rmtree.

    Asserting "fsync was called" would pass against code that synced after
    deleting the source, which is exactly the ordering that loses data. So the
    two events are recorded on one timeline and compared.

    `os.replace` makes the published name atomically VISIBLE and says nothing
    about what survives a power loss; reading the tarball back only proves the
    bytes are in the page cache. Without the sync, a crash can replay the source
    deletion while losing the archive.
    """
    events: list[str] = []
    real_fsync = os.fsync
    real_rmtree = wl.shutil.rmtree

    def spy_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def spy_rmtree(path, *a, **k):
        events.append("rmtree")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(wl.shutil, "rmtree", spy_rmtree)

    wt = tmp_path / "wt-durable"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/durable", str(wt))
    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(wt), "branch": "feature/durable", "head": "", "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    assert "rmtree" in events, "precondition: the source directory was removed"
    assert "fsync" in events, "the archive was never forced to stable storage"
    assert events.index("fsync") < events.index("rmtree"), (
        f"fsync must precede rmtree; observed order was {events}"
    )
    # At least two syncs: the archive file, and the directory holding the rename.
    assert events[: events.index("rmtree")].count("fsync") >= 2, (
        "both the archive FILE and the containing DIRECTORY must be synced — "
        "syncing only the file leaves the publishing rename undurable"
    )


def test_fsync_path_tolerates_a_directory_and_a_missing_path(tmp_path: Path) -> None:
    """Directories are the case that needs O_DIRECTORY, and must not raise.

    Also the failure path: a filesystem that refuses to sync a directory must
    degrade to the durability we had before, never turn archiving into an error.
    """
    d = tmp_path / "adir"
    d.mkdir()
    f = tmp_path / "afile"
    f.write_text("x")
    wl._fsync_path(d)
    wl._fsync_path(f)
    wl._fsync_path(tmp_path / "does-not-exist")  # must not raise


# ─── reachability: the anchor ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        ".hidden-20260915",
        "has space-20260915",
        "tilde~name",
        "caret^name",
        "colon:name",
        "question?name",
        "star*name",
        "bracket[name",
        "back\\slash",
        "dot..dot",
        "ends.lock",
        "trailing.",
        "-leading-dash",
        "..",
        ".",
    ],
)
def test_every_anchor_name_is_a_legal_ref(repo: Path, name: str) -> None:
    """The anchor is the ONLY thing keeping an archived commit reachable.

    MEASURED with `git check-ref-format`: every name in this list is a legal
    directory name and an ILLEGAL ref. When tagging failed the archive still
    registered, so the anchor was silently absent and a later prune could orphan
    the very commits it existed to protect.

    Validated against git itself rather than against a regex of my own, because
    a hand-written rule would only prove the encoder agrees with my belief about
    ref syntax — which is the belief that was wrong.
    """
    anchor = wl._ref_safe_anchor(name)
    result = _git(repo, "check-ref-format", f"refs/tags/{anchor}")
    assert result.returncode == 0, (
        f"{name!r} encoded to {anchor!r}, which git rejects: {result.stderr.strip()}"
    )


def test_anchors_stay_distinct_for_names_that_slugify_alike(repo: Path) -> None:
    """The control for the encoder: collapsing is not enough, it must be injective.

    `a b` and `a-b` both reduce to the same slug. Without the digest they would
    share one tag, and the second worktree archived would silently overwrite the
    first one's anchor — losing the ref for commits that are still only reachable
    through it. That is the same data loss the anchor exists to prevent, arriving
    by a different route.
    """
    a = wl._ref_safe_anchor("a b")
    b = wl._ref_safe_anchor("a-b")
    assert a != b, f"distinct worktree names collided on one anchor: {a}"
    for anchor in (a, b):
        assert _git(repo, "check-ref-format", f"refs/tags/{anchor}").returncode == 0


def test_a_ref_hostile_worktree_name_still_gets_a_real_anchor(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """End to end: archive a worktree whose basename is ref-illegal.

    Drives `_trash_worktree` rather than the encoder, because the defect was not
    in an encoder (there wasn't one) — it was that the raw basename reached
    `git tag`. A test of the helper alone would pass while the call site still
    passed the raw name.
    """
    wt = tmp_path / ".hidden-wt"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/hidden", str(wt))
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(wt), "branch": "feature/hidden", "head": sha, "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    pointing = _git(repo, "for-each-ref", "--points-at", sha).stdout
    assert wl._DETACHED_ANCHOR_PREFIX in pointing, (
        "a ref-illegal basename produced no anchor at all; the commit is now "
        f"reachable only through whatever else happens to point at it: {pointing!r}"
    )


# ─── reachability: recovery when the branch is gone ──────────────────────────


def test_recovery_rebuilds_a_real_worktree_after_its_branch_was_deleted(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The executor deletes the branch, so this is the ORDINARY recovery case.

    `autonomy/executor/worktree_mgr.py` runs `git branch -D` after reaping, so by
    recovery time the branch named in the metadata is routinely gone. The old
    code fell straight through to moving a plain directory back and returned
    True — leaving a tree whose `.git` points at a pruned admin dir, so
    `git status` fails inside a "successful" recovery.

    The assertion is therefore on git USABILITY, not on the return value or on
    the files being present. Both of those were already true when this was broken.
    """
    wt = tmp_path / "wt-gone"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/gone", str(wt))
    (wt / "work.txt").write_text("committed work\n")
    _git(wt, "add", "work.txt")
    _git(wt, "commit", "--quiet", "-m", "work")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(wt), "branch": "feature/gone", "head": sha, "detached": False}
    assert wl._trash_worktree(entry, repo) is True

    _git(repo, "worktree", "prune")
    assert _git(repo, "branch", "-D", "feature/gone").returncode == 0
    assert "feature/gone" not in _git(repo, "branch", "--list").stdout

    name = next(trash.iterdir()).name.split(".tar.gz")[0]
    assert wl._recover(name, repo) is True

    restored = Path(str(wt))
    assert restored.exists(), "precondition: something was put back"
    status = _git(restored, "status", "--porcelain")
    assert status.returncode == 0, (
        "the recovered tree is not a usable git worktree — this is the defect: "
        f"_recover returned True anyway. git said: {status.stderr.strip()}"
    )
    head = _git(restored, "rev-parse", "HEAD").stdout.strip()
    assert head == sha, f"recovered at {head[:8]}, expected the archived {sha[:8]}"


def test_recovery_still_restores_a_branch_that_survives(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The control. Without it, always detaching would pass the test above.

    A surviving branch must come back ON that branch, not detached.
    """
    wt = tmp_path / "wt-kept"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/kept", str(wt))
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(wt), "branch": "feature/kept", "head": sha, "detached": False}
    assert wl._trash_worktree(entry, repo) is True
    _git(repo, "worktree", "prune")

    name = next(trash.iterdir()).name.split(".tar.gz")[0]
    assert wl._recover(name, repo) is True

    restored = Path(str(wt))
    branch = _git(restored, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "feature/kept", (
        f"a surviving branch must be checked out, not detached; got {branch!r}"
    )


# ─── reachability: a nested worktree ─────────────────────────────────────────


def test_a_worktree_containing_another_worktree_is_not_moved(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """Moving the parent strands the nested tree, and the anchor is the wrong sha.

    The parent's anchor tags the PARENT's commit. A nested detached worktree's
    per-worktree HEAD is the only ref keeping ITS commits reachable, and a prune
    after the move drops it — so the archive would preserve the wrong history
    while the nested work is collected.
    """
    parent = tmp_path / "wt-parent"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/parent", str(parent))
    nested = parent / "inner"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(nested))
    assert nested.exists(), "precondition: the nested worktree was created"

    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(parent), "branch": "feature/parent", "head": "", "detached": False}
    assert wl._trash_worktree(entry, repo) is False, (
        "the parent was moved even though it contains a registered worktree"
    )
    assert parent.exists() and nested.exists(), "and nothing may have been moved"


def test_a_worktree_with_no_nested_worktree_is_still_moved(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The control: the nesting check must not refuse everything.

    A plain subdirectory that is NOT a registered worktree is not a reason to
    skip — otherwise the guard would block every worktree with any nested
    directory, which is all of them.
    """
    wt = tmp_path / "wt-solo"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/solo", str(wt))
    (wt / "subdir").mkdir()
    (wt / "subdir" / "f.txt").write_text("not a worktree\n")

    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(wt), "branch": "feature/solo", "head": "", "detached": False}
    assert wl._trash_worktree(entry, repo) is True


# ─── honesty of the board ────────────────────────────────────────────────────


def test_a_failed_enumeration_raises_instead_of_reading_as_empty(repo: Path, monkeypatch) -> None:
    """ "I could not look" and "there is nothing" must not be the same value.

    Both were an empty list, which is the entire bug: a timed-out or erroring
    `git worktree list` produced exactly what a healthy repo with no linked
    worktrees produces.
    """

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(wl.subprocess, "run", boom)
    with pytest.raises(wl.WorktreeScanError):
        wl._list_worktrees(repo)


def test_a_healthy_repo_with_no_linked_worktrees_still_returns_empty(
    repo: Path,
) -> None:
    """The control that keeps the distinction meaningful.

    If the failure case raised AND the genuine-empty case raised, callers would
    just learn to catch and ignore it, and nothing would improve.
    """
    assert wl._list_worktrees(repo) == []


def test_report_json_publishes_nothing_when_the_scan_fails(
    repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """A broken scan must not print a valid-looking empty board.

    Driven through `main` rather than by calling the classifier, because the
    defect is in what the COMMAND publishes: the empty list was already correct
    at the point it was produced, and only became a lie when it was rendered as
    a board and exited 0.
    """
    board = tmp_path / "board.json"
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(
        wl,
        "classify_all",
        lambda *a, **k: (_ for _ in ()).throw(wl.WorktreeScanError("git worktree list exited 128")),
    )
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py", "--report-json"])

    rc = wl.main()
    out = capsys.readouterr()
    assert rc != 0, "a failed scan must not exit 0"
    assert out.out.strip() == "", (
        f"nothing may be printed on stdout — a consumer parses this: {out.out[:120]!r}"
    )
    assert "could not enumerate" in out.err
    assert not board.exists()


def test_report_json_still_emits_a_document_on_a_healthy_scan(
    repo: Path, monkeypatch, capsys
) -> None:
    """The control: the failure path must not have broken the success path."""
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(wl, "classify_all", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py", "--report-json", "--no-network"])
    rc = wl.main()
    out = capsys.readouterr()
    assert rc == 0
    assert json.loads(out.out) == []
