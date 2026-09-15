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
import tarfile
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


# ─── reachability: recovery when the branch is gone ──────────────────────────


def test_recovery_rebuilds_a_real_worktree_after_its_branch_was_deleted(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """Recovery rebuilds a usable worktree even when the branch is gone.

    This USED to be the ordinary case: `autonomy/executor/worktree_mgr.py` runs
    `git branch -D` after reaping, so the branch named in the metadata was
    routinely absent by recovery time. Archiving now LOCKS the registration as
    its history anchor, and git refuses to delete a branch a registration still
    uses — so the executor's delete fails and the branch usually survives. The
    scenario is therefore rarer, and the test forces it explicitly below.

    It is still worth pinning, because it is reachable whenever someone clears
    up by hand, and the failure it guards is nasty: the old code fell through to
    moving a plain directory back and returned True, leaving a tree whose `.git`
    points at a pruned admin dir, so `git status` fails inside a "successful"
    recovery.

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

    # FORCE the branch-gone state. It is no longer reachable by accident: the
    # archive LOCKS the registration as its history anchor, a locked
    # registration survives `prune`, and git refuses to delete a branch a
    # registration still uses. So this scenario now takes a deliberate unlock +
    # prune + delete -- which is exactly what a human clearing up by hand would
    # do, and is the only route by which recovery can still meet a missing
    # branch. The assertion below is unchanged and is what this test is for.
    assert _git(repo, "worktree", "unlock", str(wt)).returncode == 0
    _git(repo, "worktree", "prune")
    assert _git(repo, "branch", "-D", "feature/gone").returncode == 0
    assert "feature/gone" not in _git(repo, "branch", "--list").stdout

    # Select the ARCHIVE explicitly. `next(trash.iterdir())` is ordered by the
    # filesystem, and the entry also has a sidecar `.meta.json` beside it — so
    # this picked the sidecar in CI and the tarball locally, which is a flaky
    # test rather than a flaky product. Caught by CI, not by me.
    name = next(trash.glob("*.tar.gz")).name[: -len(".tar.gz")]
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

    # Select the ARCHIVE explicitly. `next(trash.iterdir())` is ordered by the
    # filesystem, and the entry also has a sidecar `.meta.json` beside it — so
    # this picked the sidecar in CI and the tarball locally, which is a flaky
    # test rather than a flaky product. Caught by CI, not by me.
    name = next(trash.glob("*.tar.gz")).name[: -len(".tar.gz")]
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


# ─── round-4: the archive must not eat the worktree's own files ──────────────


def test_a_worktree_owning_a_trash_meta_json_does_not_lose_it(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """`.trash_meta.json` is not a reserved name, and the worktree may own one.

    The same class as the `.dirty.patch` collision fixed in the previous round,
    at the second archive-time write that was missed: `Path.rename` replaces the
    destination silently, so the worktree's own file was destroyed in the
    worktree AND in the archive — the archive is made from the moved directory,
    so there is no surviving copy anywhere.

    OURS must keep the canonical name, because recovery locates entries by it
    (including archives already on disk), so the assertion is that the user's
    bytes survive under some other name — not that ours moved aside.
    """
    wt = tmp_path / "wt-meta-collide"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/metacollide", str(wt))
    mine = '{"this": "is the worktree owner s own file"}\n'
    (wt / ".trash_meta.json").write_text(mine)

    trash = tmp_path / "trash-meta"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb-meta.jsonl")
    entry = {
        "path": str(wt), "branch": "feature/metacollide", "head": "", "detached": False,
    }
    assert wl._trash_worktree(entry, repo) is True

    archive = next(trash.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as tf:
        names = tf.getnames()
        preserved = [n for n in names if ".trash_meta.json.from-worktree-" in n]
        assert preserved, (
            f"the worktree's own .trash_meta.json was destroyed; archive holds {names[:10]}"
        )
        assert tf.extractfile(preserved[0]).read().decode() == mine, (
            "the preserved file is not the worktree's original bytes"
        )
        canonical = [n for n in names if n.endswith("/.trash_meta.json")]
        assert canonical, "our own metadata must still be at the canonical name"
        ours = json.loads(tf.extractfile(canonical[0]).read())
        assert ours.get("name") == trash_entry_name(archive), (
            "the canonical file must be OURS, not the worktree's"
        )


def trash_entry_name(archive: Path) -> str:
    return archive.name.split(".tar.gz")[0]


def test_a_worktree_path_with_a_newline_is_parsed_whole(repo: Path, tmp_path: Path) -> None:
    """A newline is legal in a Unix path and porcelain puts it INSIDE the value.

    Splitting the listing on lines invents a truncated path that matches nothing
    on disk, so that worktree is either invisible or acted on under a name that
    is not its own. `-z` makes records NUL-terminated instead.
    """
    weird = tmp_path / "wt\nnewline"
    added = _git(repo, "worktree", "add", "--quiet", "-b", "feature/nl", str(weird))
    if added.returncode != 0:
        pytest.skip(f"filesystem rejects a newline in a path: {added.stderr.strip()}")

    paths = [w["path"] for w in wl._list_worktrees(repo)]
    assert str(weird) in paths, f"the newline path was not parsed whole; got {paths}"


def test_a_non_utf8_worktree_path_does_not_crash_enumeration(repo: Path, tmp_path: Path) -> None:
    """A path is bytes, not text, and decoding it strictly raises.

    Under `text=True` a non-UTF-8 path raised UnicodeDecodeError BEFORE the
    failure normalisation could turn it into a WorktreeScanError — so
    `--report-json` died with a traceback rather than reporting a scan failure,
    which is the one outcome the error type exists to prevent.
    """
    raw = str(tmp_path).encode() + b"/wt-bad-\xff"
    try:
        os.mkdir(raw)
        os.rmdir(raw)
    except (OSError, ValueError):
        pytest.skip("filesystem rejects non-UTF-8 path bytes")

    added = _git(
        repo,
        "worktree",
        "add",
        "--quiet",
        "-b",
        "feature/bad",
        raw.decode("utf-8", "surrogateescape"),
    )
    if added.returncode != 0:
        pytest.skip(f"git refused the non-UTF-8 path: {added.stderr.strip()}")

    worktrees = wl._list_worktrees(repo)  # must not raise
    assert any("wt-bad-" in w["path"] for w in worktrees), (
        "the non-UTF-8 worktree vanished from the listing instead of crashing, "
        "which is the other way to get this wrong"
    )


def test_a_long_archive_name_still_yields_a_usable_scratch_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """Archivable-but-unrecoverable is the worst asymmetry available here.

    A 231-character basename produces a valid `.tar.gz`, and prepending
    `.extract-` to it exceeded the 255-byte component limit — so `mkdir` raised
    ENAMETOOLONG and the archive could never be opened. Recovery must not be able
    to fail on a name that archiving accepted.
    """
    monkeypatch.setattr(wl, "TRASH_DIR", tmp_path / "trash")
    (tmp_path / "trash").mkdir()
    # 240 + ".tar.gz" = 247, a LEGAL archive name. Prefixing ".extract-" (9)
    # gives 256, one over the limit — the exact asymmetry: creatable, then
    # unopenable. An earlier version used 231 and produced a 247-byte scratch
    # name that fit, so the mutation survived and the test proved nothing.
    long_name = "w" * 240 + ".tar.gz"
    assert len(long_name.encode()) == 247, "precondition: a legal archive name"
    assert len(long_name.encode()) + len(".extract-") > 255, (
        "precondition: the NAIVE derivation would exceed the component limit"
    )

    # Calls the PRODUCTION derivation. An earlier version recomputed the formula
    # here and therefore passed against the broken implementation too; mutation
    # caught it, and that is why this goes through wl.
    scratch = wl._scratch_dir_for(Path(long_name))
    assert len(scratch.name.encode()) <= 255, (
        f"scratch component is {len(scratch.name.encode())} bytes, so an archive "
        "that was creatable could never be opened"
    )
    scratch.mkdir()  # must not raise ENAMETOOLONG
    assert scratch.is_dir()


def test_the_invoking_shell_counts_as_a_user_of_its_own_worktree(tmp_path: Path) -> None:
    """The one process certainly using a worktree was the one guaranteed unseen.

    Self and parent were excluded so the reaper would not see itself. But the
    documented hand-run happens FROM a worktree, so its own cwd and its shell's
    are in the directory under consideration — and entering a directory refreshes
    no mtime, so an idle 14-day-old worktree passes the staleness test while
    somebody is standing in it.

    Driven by actually changing this process's cwd rather than by simulating one,
    because the defect was about which pids are skipped.
    """
    target = tmp_path / "standing-here"
    target.mkdir()
    before = os.getcwd()
    try:
        os.chdir(target)
        found = wl._find_processes_in_dir(str(target))
    finally:
        os.chdir(before)
    assert os.getpid() in found, (
        "the running process was not counted as a user of its own cwd, so the "
        "reaper would archive the directory its invoker is standing in"
    )


def test_an_unrelated_directory_still_reports_no_users(tmp_path: Path) -> None:
    """The control: counting self must not make every directory look occupied."""
    empty = tmp_path / "nobody-here"
    empty.mkdir()
    assert wl._find_processes_in_dir(str(empty)) == []


def test_recovery_does_not_nest_when_worktree_add_half_succeeds(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """`git worktree add` can CREATE its destination and then fail.

    A `post-checkout` hook exiting non-zero is the reproducible case, and it is
    the one that matters: the directory now exists, so the fallback's
    `shutil.move` places the whole archive INSIDE it as `<path>/<name>/...` while
    recovery reports success. Everything present, nothing where recovery said.

    An earlier version of this test pre-created the destination, which can never
    reach the fallback — `_recover` returns False at the "original path already
    exists" check long before it. That test passed for a reason unrelated to the
    guard, and mutation is what exposed it. The destination here is created by
    GIT, mid-recovery, exactly as the finding describes.
    """
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head

    trash = tmp_path / "trash-half"
    trash.mkdir()
    entry = trash / "wt-half"
    entry.mkdir()
    (entry / "archived-file.txt").write_text("from the archive\n")
    original = tmp_path / "never-created-yet"
    (entry / ".trash_meta.json").write_text(
        json.dumps(
            {
                "original_path": str(original),
                "branch": "",
                "commit": head,
                "detached": True,
            }
        )
    )
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    assert not original.exists(), "precondition: git must be the one to create it"

    result = wl._recover("wt-half", repo)

    # The archive must NEVER end up one level down, whatever the verdict.
    nested = original / "wt-half"
    assert not nested.exists(), (
        f"the archive was nested at {nested} — recovery relocated everything "
        "one level deeper than it reported"
    )
    # Refusing is the correct outcome here: git had already checked files out
    # into the destination, so it is not a directory we may clear. The important
    # property is that a refusal is LOUD and LOSSLESS — reported as failure, with
    # the archive still intact in the trash for another attempt.
    assert result is False, "a refusal must be reported as failure, not success"
    assert (entry / "archived-file.txt").exists(), (
        "the archive was consumed by a recovery that did not complete"
    )


def test_the_archived_registration_is_locked_and_survives_a_prune(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The anchor has to be one every other caller RESPECTS, not merely one
    nobody has cleared yet.

    Leaving the registration unlocked anchors nothing durable: three callers in
    this repo run `git worktree prune` on their own schedule
    (`autonomy/executor/worktree_mgr.py` on task-worktree creation,
    `contribution/pr_opener.py` per contribution run, and this module's own
    recovery), and `git gc` clears such registrations by itself once the archive
    passes `gc.worktreePruneExpire` — three months by default. Any one of them
    would silently de-anchor the archive and let a later gc collect the commits
    it points at.

    MEASURED on git 2.43 while writing this: an UNLOCKED sibling archive was
    de-anchored by exactly this prune and its commit was then collected, while
    the locked one survived. Locking also clears the `prunable` porcelain
    marker, which is what keeps the zero-drop sweep from holding an archived
    worktree's findings open forever.
    """
    wt = tmp_path / "wt-anchored"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(wt))
    (wt / "w.txt").write_text("unique\n")
    _git(wt, "add", "w.txt")
    _git(wt, "commit", "--quiet", "-m", "unique work")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    trash = tmp_path / "trash"
    monkeypatch.setattr(wl, "TRASH_DIR", trash)
    monkeypatch.setattr(wl, "TOMBSTONE_INDEX", tmp_path / "tomb.jsonl")
    entry = {"path": str(wt), "branch": "", "head": sha, "detached": True}
    assert wl._trash_worktree(entry, repo) is True

    listing = _git(repo, "worktree", "list", "--porcelain").stdout
    assert "locked" in listing, "the archived registration was not locked"

    # The whole point: a prune must not be able to take the anchor away.
    _git(repo, "worktree", "prune")
    assert str(wt) in _git(repo, "worktree", "list", "--porcelain").stdout, (
        "a plain `git worktree prune` removed the archive's only anchor"
    )
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")
    assert _git(repo, "cat-file", "-e", sha).returncode == 0, (
        "the archived commit was collected — the anchor did not hold"
    )
