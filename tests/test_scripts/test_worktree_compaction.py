"""Compacting a clean, unmerged archive to a pinned commit and a metadata stub.

An archive qualifies only when deleting its tarball loses nothing: every file in
it matches its recorded head byte for byte, nothing in its registration (index,
reflog, per-worktree refs) holds anything that head and the repository's refs do
not, and its head is pinned under ``refs/archived/`` before anything is removed.
These tests use real git repositories and the real reaper, so the archive,
sidecar and lock reason compaction reads are the ones the reaper writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import private_module

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "worktree_lifecycle.py"

wl = private_module("worktree_lifecycle_compaction", _SCRIPT)


# The fixture's own git never follows a caller's repository or index; the code
# under test still sees them, which test_an_inherited_index_variable relies on.
_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check, env=env
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the operator's real ~/.genesis."""
    for name in ("TRASH_DIR", "LOG_DIR", "TOMBSTONE_INDEX", "BOARD_CACHE"):
        monkeypatch.setattr(wl, name, tmp_path / f"isolated-{name.lower()}")
    for var in (wl.RETENTION_ENV, wl.STALE_CLAIM_ENV, wl.COMPACTION_ENV):
        monkeypatch.delenv(var, raising=False)


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


def _sidecar(entry: str) -> Path:
    return wl.TRASH_DIR / f"{entry}.meta.json"


def _stub(entry: str) -> Path:
    return wl.TRASH_DIR / f"{entry}{wl.COMPACTED_SUFFIX}"


def _edit_sidecar(entry: str, **fields) -> None:
    meta = json.loads(_sidecar(entry).read_text())
    meta.update(fields)
    _sidecar(entry).write_text(json.dumps(meta))


def _backdate(entry: str, days: float) -> None:
    _edit_sidecar(entry, trashed_at=(datetime.now(UTC) - timedelta(days=days)).isoformat())


def _archive(
    repo: Path,
    tmp_path: Path,
    name: str,
    *,
    lane: str = "unmerged",
    branch: str | None = None,
    setup=None,
    age: float = 40,
) -> dict:
    """Archive a worktree holding one commit only it reaches, through the REAL
    reaper. ``setup(wt)`` runs after that commit and before archiving."""
    wt = tmp_path / name
    if branch:
        _git(repo, "worktree", "add", "-q", "-b", branch, str(wt))
    else:
        _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    (wt / f"{name}.txt").write_text("only here\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"only-{name}")
    if setup is not None:
        setup(wt)
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    rec = next(x for x in wl._list_worktrees(repo) if x["path"] == str(wt))
    assert wl._trash_worktree(rec, repo, lane=lane) is True
    tarball = next(wl.TRASH_DIR.glob(f"{name}-*.tar.gz"))
    entry = tarball.name[: -len(".tar.gz")]
    _backdate(entry, age)
    return {"path": wt, "head": head, "entry": entry, "archive": tarball}


def _registered(repo: Path) -> dict[str, str | None]:
    out = _git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0")
    regs: dict[str, str | None] = {}
    cur = None
    for line in out:
        if line.startswith("worktree "):
            cur = line[len("worktree ") :]
            regs[cur] = None
        elif line.startswith("locked") and cur is not None:
            regs[cur] = line[len("locked ") :] if line != "locked" else ""
    return regs


def _commit_exists(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _gc_now(repo: Path) -> None:
    _git(
        repo,
        "-c",
        "gc.reflogExpire=now",
        "-c",
        "gc.reflogExpireUnreachable=now",
        "-c",
        "gc.worktreePruneExpire=now",
        "reflog",
        "expire",
        "--expire=now",
        "--expire-unreachable=now",
        "--all",
    )
    _git(repo, "-c", "gc.worktreePruneExpire=now", "gc", "--prune=now", "-q")


def _pin_ref(entry: str) -> str:
    digest = hashlib.sha256(entry.encode()).hexdigest()[:10]
    return f"refs/archived/{entry}-{digest}"


def _ref(repo: Path, ref: str) -> str | None:
    r = _git(repo, "rev-parse", "--verify", "-q", ref, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _admin(repo: Path, entry: str) -> Path:
    anchors = [a for a in wl._archive_anchors(repo) if a["entry"] == entry]
    assert len(anchors) == 1
    return anchors[0]["admin"]


def _compact(repo: Path, **kw) -> dict:
    kw.setdefault("days", 30)
    return wl._compact_trash(repo, **kw)


# ─── the acceptance case ─────────────────────────────────────────────────────


def test_compact_then_gc_then_recover_detached_at_the_pinned_head(repo, tmp_path):
    """The whole mechanism on a commit nothing but the archive reaches: after
    compaction the tarball, sidecar and anchor are gone, a stub and a pin are
    left, `gc` keeps the commit, and recovery recreates the worktree DETACHED at
    exactly that commit with a clean status."""
    a = _archive(repo, tmp_path, "solo")
    _gc_now(repo)
    assert _commit_exists(repo, a["head"]), "control: the anchor keeps the commit"

    counts = _compact(repo)

    assert counts["compacted"] == 1, counts
    assert not a["archive"].exists()
    assert not _sidecar(a["entry"]).exists()
    stub = json.loads(_stub(a["entry"]).read_text())
    assert stub["format"] == wl.COMPACTED_FORMAT
    assert stub["pin"] == _pin_ref(a["entry"]) and stub["head"] == a["head"]
    assert stub["lane"] == "unmerged" and stub["original_path"] == str(a["path"])
    assert str(a["path"]) not in _registered(repo), "the anchor must be pruned"
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]

    _gc_now(repo)
    assert _commit_exists(repo, a["head"]), "the pin did not keep the commit alive"

    report: dict = {}
    assert wl._recover(a["entry"], repo, report) is True
    assert not report.get("incomplete"), report
    assert _git(a["path"], "rev-parse", "HEAD").stdout.strip() == a["head"]
    assert _git(a["path"], "symbolic-ref", "-q", "HEAD", check=False).returncode != 0, (
        "recovery must be detached"
    )
    assert _git(a["path"], "status", "--porcelain").stdout == ""
    assert (a["path"] / "solo.txt").read_text() == "only here\n"
    assert _stub(a["entry"]).exists(), "recovery keeps the stub, like an archive"
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"], "and the pin"


def test_recovery_ignores_a_branch_that_moved_after_compaction(repo, tmp_path):
    a = _archive(repo, tmp_path, "moved", branch="feat")
    assert _compact(repo)["compacted"] == 1
    _git(repo, "update-ref", "refs/heads/feat", _git(repo, "rev-parse", "main").stdout.strip())
    report: dict = {}
    assert wl._recover(a["entry"], repo, report) is True
    assert _git(a["path"], "rev-parse", "HEAD").stdout.strip() == a["head"]
    assert _git(a["path"], "symbolic-ref", "-q", "HEAD", check=False).returncode != 0


def test_a_compacted_entry_is_listed_and_never_expired(repo, tmp_path, capsys):
    a = _archive(repo, tmp_path, "listed")
    assert _compact(repo)["compacted"] == 1
    wl._list_trash(repo)
    out = capsys.readouterr().out
    assert a["entry"] + wl.COMPACTED_SUFFIX in out and "pinned" in out
    counts = wl._expire_trash(repo, days=1, allow_network=False)
    assert counts["expired"] == 0
    assert _stub(a["entry"]).exists()
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]


def test_a_stub_holds_its_name_against_a_later_archive(repo, tmp_path):
    a = _archive(repo, tmp_path, "named")
    assert _compact(repo)["compacted"] == 1
    assert wl._trash_name_taken(wl.TRASH_DIR / a["entry"])
    assert wl._trash_name_taken_excluding_claim(wl.TRASH_DIR / a["entry"])


def test_dry_run_changes_nothing(repo, tmp_path):
    a = _archive(repo, tmp_path, "dry")

    def snapshot():
        files = {p.name: p.read_bytes() for p in wl.TRASH_DIR.iterdir() if p.is_file()}
        refs = _git(repo, "for-each-ref").stdout
        return files, refs, _registered(repo)

    before = snapshot()
    counts = _compact(repo, dry_run=True)
    assert counts["would_compact"] == 1 and counts["compacted"] == 0
    assert snapshot() == before
    assert (
        not (wl.TRASH_DIR / wl.LIFECYCLE_LOCK_NAME).exists() or wl.LIFECYCLE_LOCK_NAME in before[0]
    ), "a dry run takes no lock"
    assert a["archive"].exists()


def test_the_kill_switch_compacts_nothing(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "killed")
    monkeypatch.setenv(wl.COMPACTION_ENV, "off")
    assert _compact(repo)["compacted"] == 0
    assert a["archive"].exists()


def test_cli_compact_trash_dry_run(repo, tmp_path, monkeypatch, capsys):
    a = _archive(repo, tmp_path, "cli")
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py", "--compact-trash", "--dry-run"])
    assert wl.main() == 0
    assert "WOULD COMPACT " + a["entry"] in capsys.readouterr().out
    monkeypatch.setattr(
        sys, "argv", ["worktree_lifecycle.py", "--compact-trash", "--compact-after-days", "60"]
    )
    assert wl.main() == 0
    assert a["archive"].exists(), "40 days is not older than 60"


# ─── every gate keeps ────────────────────────────────────────────────────────


def _stage_then_revert(wt: Path) -> None:
    (wt / "seed.txt").write_text("staged\n")
    _git(wt, "add", "seed.txt")
    (wt / "seed.txt").write_text("seed\n")


def _orphan_in_reflog(wt: Path) -> None:
    (wt / "gone.txt").write_text("x\n")
    _git(wt, "add", "gone.txt")
    _git(wt, "commit", "-qm", "gone")
    _git(wt, "reset", "-q", "--hard", "HEAD~1")


def _delete_tracked(wt: Path) -> None:
    """A tracked file gone from disk that `git status` does not report
    (skip-worktree), so the reaper records the worktree clean and writes no
    patch: only the archive's own file list shows the deletion."""
    _git(wt, "update-index", "--skip-worktree", "seed.txt")
    (wt / "seed.txt").unlink()
    assert _git(wt, "status", "--porcelain").stdout == "", "fixture: status must read clean"


def _extra_file(wt: Path) -> None:
    (wt / "extra.txt").write_text("untracked\n")


_GATES = [
    (
        "tracked file deleted",
        {"setup": _delete_tracked},
        None,
        "lacks tracked",
    ),
    ("merged lane", {"lane": "merged"}, None, "lane"),
    (
        "dirty",
        {},
        lambda a, repo: _edit_sidecar(a["entry"], had_uncommitted_changes=True),
        "uncommitted",
    ),
    ("young", {"age": 5}, None, None),
    (
        "unresolvable head",
        {},
        lambda a, repo: _edit_sidecar(a["entry"], head="f" * 40, commit="f" * 40),
        "resolve",
    ),
    (
        "extra file",
        {"setup": _extra_file},
        lambda a, repo: _edit_sidecar(a["entry"], had_uncommitted_changes=False),
        "not in head",
    ),
    (
        "staged index",
        {"setup": _stage_then_revert},
        lambda a, repo: _edit_sidecar(a["entry"], had_uncommitted_changes=False),
        "index",
    ),
    ("orphan reflog commit", {"setup": _orphan_in_reflog}, None, "reflog"),
    (
        "scratch dir",
        {},
        lambda a, repo: wl._scratch_dir_for(a["archive"]).mkdir(),
        "extraction",
    ),
    ("twin dir", {}, lambda a, repo: (wl.TRASH_DIR / a["entry"]).mkdir(), "directory"),
    ("anchor path present", {}, lambda a, repo: a["path"].mkdir(), "exists"),
    (
        "per-worktree ref",
        {},
        lambda a, repo: _git(
            repo,
            "--git-dir",
            str(_admin(repo, a["entry"])),
            "update-ref",
            "refs/bisect/bad",
            a["head"],
        ),
        "refs",
    ),
    (
        "pin taken",
        {},
        lambda a, repo: _git(
            repo,
            "update-ref",
            _pin_ref(a["entry"]),
            _git(repo, "rev-parse", "main").stdout.strip(),
        ),
        "pin",
    ),
    (
        "anchor head moved",
        {"branch": "b"},
        # Same tree, new commit: the index still matches, so only the HEAD gate can fire.
        lambda a, repo: _git(
            repo,
            "update-ref",
            "refs/heads/b",
            _git(repo, "commit-tree", "b^{tree}", "-p", "b", "-m", "moved").stdout.strip(),
        ),
        "registration's HEAD is not the recorded head",
    ),
]


@pytest.mark.parametrize(
    ("label", "archive_kw", "after", "reason"), _GATES, ids=[g[0] for g in _GATES]
)
def test_every_gate_keeps_the_archive(repo, tmp_path, label, archive_kw, after, reason):
    a = _archive(repo, tmp_path, "gate", **archive_kw)
    if after is not None:
        after(a, repo)
    counts = _compact(repo)
    assert counts["compacted"] == 0, label
    assert a["archive"].exists() and _sidecar(a["entry"]).exists(), label
    assert not _stub(a["entry"]).exists(), label
    if reason is not None:
        assert reason in counts["kept"][a["entry"]], (label, counts["kept"])
    regs = _registered(repo)
    if label != "anchor path present":
        assert regs.get(str(a["path"])), f"{label}: the anchor must stay locked"


def test_two_anchors_keep_the_archive(repo, tmp_path):
    a = _archive(repo, tmp_path, "twice")
    other = tmp_path / "other"
    _git(repo, "worktree", "add", "-q", "--detach", str(other))
    other.rename(tmp_path / "other.gone")
    _git(repo, "worktree", "lock", "--reason", wl._archive_lock_reason(a["entry"]), str(other))
    counts = _compact(repo)
    assert counts["compacted"] == 0 and "anchor" in counts["kept"][a["entry"]]


def test_an_entry_name_that_is_no_ref_is_kept(repo, tmp_path):
    a = _archive(repo, tmp_path, "bad..name")
    counts = _compact(repo)
    assert counts["compacted"] == 0 and "ref name" in counts["kept"][a["entry"]]


def test_a_legacy_directory_entry_is_left_alone(repo, tmp_path):
    legacy = wl.TRASH_DIR / "legacy-20260101"
    legacy.mkdir(parents=True)
    (legacy / ".trash_meta.json").write_text(json.dumps({"lane": "unmerged"}))
    _archive(repo, tmp_path, "real")
    counts = _compact(repo)
    assert counts["compacted"] == 1
    assert legacy.is_dir() and not _stub("legacy-20260101").exists()


# ─── crashes converge, and expiry never takes the pin ────────────────────────


def _assert_pin_survives_expiry(repo, a) -> None:
    wl._expire_trash(repo, days=1, allow_network=False)
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"], "expiry deleted the pin"


def test_crash_after_the_pin(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "c1")
    real = wl._atomic_write_text

    def boom(path, *args, **kw):
        if str(path).endswith(wl.COMPACTED_SUFFIX):
            raise OSError("disk full")
        return real(path, *args, **kw)

    monkeypatch.setattr(wl, "_atomic_write_text", boom)
    assert _compact(repo)["compacted"] == 0
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]
    assert a["archive"].exists() and _registered(repo)[str(a["path"])]
    _assert_pin_survives_expiry(repo, a)
    monkeypatch.setattr(wl, "_atomic_write_text", real)
    assert _compact(repo)["compacted"] == 1, "a pin already at the head is reused"


def test_crash_after_the_stub(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "c2")
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda *_a, **_k: False)
    assert _compact(repo)["compacted"] == 0
    assert _stub(a["entry"]).exists() and a["archive"].exists()
    _assert_pin_survives_expiry(repo, a)
    assert a["archive"].exists(), "expiry must keep the tarball of an unmerged entry"
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    counts = _compact(repo)
    assert counts["resumed"] == 1, counts
    assert not a["archive"].exists() and not _sidecar(a["entry"]).exists()
    assert str(a["path"]) not in _registered(repo)


def test_crash_after_the_anchor(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "c3")
    real = wl._delete_stored
    monkeypatch.setattr(wl, "_delete_stored", lambda *_a, **_k: False)
    assert _compact(repo)["compacted"] == 0
    assert str(a["path"]) not in _registered(repo) and a["archive"].exists()
    _assert_pin_survives_expiry(repo, a)
    assert a["archive"].exists()
    monkeypatch.setattr(wl, "_delete_stored", real)
    counts = _compact(repo)
    assert counts["resumed"] == 1, counts
    assert not a["archive"].exists() and not _sidecar(a["entry"]).exists()
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]


def test_crash_after_the_tarball_leaves_only_a_sidecar_to_finish(repo, tmp_path):
    a = _archive(repo, tmp_path, "c4")
    assert _compact(repo)["compacted"] == 1
    _sidecar(a["entry"]).write_text("{}")
    counts = _compact(repo)
    assert counts["resumed"] == 1 and not _sidecar(a["entry"]).exists()


def test_resume_never_deletes_a_tarball_it_did_not_record(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "swap")
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda *_a, **_k: False)
    assert _compact(repo)["compacted"] == 0
    # Same bytes, a different file: written beside it, then renamed over it, so
    # the inode cannot be reused.
    other = a["archive"].with_name("other.bin")
    other.write_bytes(a["archive"].read_bytes())
    os.replace(other, a["archive"])
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    counts = _compact(repo)
    assert counts["resumed"] == 0 and a["archive"].exists()
    assert _registered(repo)[str(a["path"])], "and never touches its anchor"


# ─── recovery of a stub ──────────────────────────────────────────────────────


def test_recovery_dry_run_describes_and_writes_nothing(repo, tmp_path, capsys):
    a = _archive(repo, tmp_path, "preview")
    assert _compact(repo)["compacted"] == 1
    before = sorted(p.name for p in wl.TRASH_DIR.iterdir())
    assert wl._recover(a["entry"], repo, {}, dry_run=True) is True
    out = capsys.readouterr()
    assert "WOULD" in out.out and a["head"][:8] in out.out
    assert sorted(p.name for p in wl.TRASH_DIR.iterdir()) == before
    assert not a["path"].exists()


def test_a_lost_pin_is_re_created_by_the_next_run(repo, tmp_path):
    a = _archive(repo, tmp_path, "repin")
    assert _compact(repo)["compacted"] == 1
    _git(repo, "update-ref", "-d", _pin_ref(a["entry"]))
    counts = _compact(repo)
    assert counts["repinned"] == 1 and counts["errors"] == 0, counts
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]


def test_a_lost_pin_whose_commit_was_collected_comes_back_from_the_bundle(repo, tmp_path):
    """The case the bundle exists for: a refs repair drops the pin, gc then
    collects the commit, and the bundle still brings it back."""
    a = _archive(repo, tmp_path, "rebundle")
    assert _compact(repo)["compacted"] == 1
    stub = json.loads(_stub(a["entry"]).read_text())
    assert stub["bundle"] and (wl.TRASH_DIR / stub["bundle"]).is_file()
    _git(repo, "update-ref", "-d", _pin_ref(a["entry"]))
    _gc_now(repo)
    assert not _commit_exists(repo, a["head"]), "control: nothing else held the commit"
    report: dict = {}
    assert wl._recover(a["entry"], repo, report) is True
    assert not report.get("incomplete")
    assert _git(a["path"], "rev-parse", "HEAD").stdout.strip() == a["head"]
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]


def test_a_collected_commit_with_no_bundle_is_an_error_not_silence(
    repo, tmp_path, monkeypatch, capsys
):
    a = _archive(repo, tmp_path, "gone")
    assert _compact(repo)["compacted"] == 1
    stub = json.loads(_stub(a["entry"]).read_text())
    (wl.TRASH_DIR / stub["bundle"]).unlink()
    _git(repo, "update-ref", "-d", _pin_ref(a["entry"]))
    _gc_now(repo)
    counts = _compact(repo)
    assert counts["errors"] == 1 and counts["repinned"] == 0, counts
    wl._list_trash(repo)
    assert "PIN MISSING" in capsys.readouterr().out
    assert wl._recover(a["entry"], repo, {}) is False
    assert not a["path"].exists()
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    monkeypatch.setattr(sys, "argv", ["worktree_lifecycle.py", "--compact-trash"])
    assert wl.main() == 2, "a lost commit must fail the run"


def test_an_altered_bundle_holds_the_finish(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "altered")
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda *_a, **_k: False)
    assert _compact(repo)["compacted"] == 0
    stub = json.loads(_stub(a["entry"]).read_text())
    (wl.TRASH_DIR / stub["bundle"]).write_bytes(b"not a bundle")
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    counts = _compact(repo)
    assert counts["resumed"] == 0 and counts["held"] == 1, counts
    assert a["archive"].exists() and _registered(repo)[str(a["path"])]


def test_a_head_already_in_main_needs_no_bundle(repo, tmp_path):
    def merge_into_main(wt):
        _git(repo, "merge", "-q", "--ff-only", _git(wt, "rev-parse", "HEAD").stdout.strip())

    a = _archive(repo, tmp_path, "inmain", setup=merge_into_main)
    assert _compact(repo)["compacted"] == 1
    stub = json.loads(_stub(a["entry"]).read_text())
    assert stub["bundle"] == "" and stub["bundle_base"] == "refs/heads/main"


def test_expiry_never_deletes_a_stubs_pin(repo, tmp_path):
    """A same-named archive beside a stub (a hand copy, a restore) used to take
    the stub's pin with it when it expired."""
    a = _archive(repo, tmp_path, "owned")
    assert _compact(repo)["compacted"] == 1
    assert wl._delete_archive_refs(repo, a["entry"]) == []
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]


def test_a_malformed_stub_is_an_error_and_does_not_stop_the_run(repo, tmp_path):
    a = _archive(repo, tmp_path, "fine")
    wl.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    _stub("broken-20260101").write_text(
        json.dumps({"format": wl.COMPACTED_FORMAT, "name": "broken-20260101"})
    )
    counts = _compact(repo)
    assert counts["errors"] == 1 and counts["compacted"] == 1, counts
    assert not a["archive"].exists()


def test_a_crash_between_unlock_and_prune_leaves_no_registration(repo, tmp_path, monkeypatch):
    a = _archive(repo, tmp_path, "leak")
    real = wl._prune_anchor
    monkeypatch.setattr(wl, "_prune_anchor", lambda *_a, **_k: False)
    assert _compact(repo)["compacted"] == 0
    assert str(a["path"]) in _registered(repo) and _registered(repo)[str(a["path"])] is None
    monkeypatch.setattr(wl, "_prune_anchor", real)
    counts = _compact(repo)
    assert counts["resumed"] == 1, counts
    assert str(a["path"]) not in _registered(repo)
    assert not a["archive"].exists()


def test_recovery_dry_run_accepts_this_entrys_own_relock(repo, tmp_path, capsys):
    """The preview must agree with the real run, which refuses only a
    registration ANOTHER archive holds."""
    a = _archive(repo, tmp_path, "relocked")
    assert _compact(repo)["compacted"] == 1
    _git(repo, "worktree", "add", "-q", "--detach", str(a["path"]), a["head"])
    tmp = tmp_path / "relocked.gone"
    a["path"].rename(tmp)
    reason = wl._archive_lock_reason(a["entry"], recovery_failed=True)
    _git(repo, "worktree", "lock", "--reason", reason, str(a["path"]))
    assert wl._recover(a["entry"], repo, {}, dry_run=True) is True
    assert "WOULD RECREATE" in capsys.readouterr().out
    assert wl._recover(a["entry"], repo, {}) is True
    assert _git(a["path"], "rev-parse", "HEAD").stdout.strip() == a["head"]


def test_recovery_refuses_an_occupied_path(repo, tmp_path):
    a = _archive(repo, tmp_path, "occupied")
    assert _compact(repo)["compacted"] == 1
    a["path"].mkdir()
    assert wl._recover(a["entry"], repo, {}) is False
    assert list(a["path"].iterdir()) == []


def test_journal_records_compacting_then_compacted_and_never_deleted(repo, tmp_path):
    a = _archive(repo, tmp_path, "journal")
    assert _compact(repo)["compacted"] == 1
    rows = [json.loads(x) for x in wl.TOMBSTONE_INDEX.read_text().splitlines()]
    events = [r["event"] for r in rows if r.get("event") and r.get("name") == a["entry"]]
    assert events == ["compacting", "compacted"]
    assert a["entry"] not in wl._deleted_names()


def test_a_failed_completion_record_is_retried_once(repo, tmp_path, monkeypatch):
    """The journal is the only record that a compaction finished, and a stub with
    nothing left on disk is visited for no other reason: a failed `compacted`
    append holds the run and the next run writes it, exactly once."""
    a = _archive(repo, tmp_path, "unrecorded")
    real = wl._append_record

    def no_completion(record):
        return False if record.get("event") == "compacted" else real(record)

    monkeypatch.setattr(wl, "_append_record", no_completion)
    first = _compact(repo)
    assert first["compacted"] == 0 and first["held"] == 1, first
    assert not a["archive"].exists(), "the physical steps still ran"

    def events():
        rows = [json.loads(x) for x in wl.TOMBSTONE_INDEX.read_text().splitlines()]
        return [r["event"] for r in rows if r.get("event") and r.get("name") == a["entry"]]

    assert events() == ["compacting"]
    monkeypatch.setattr(wl, "_append_record", real)
    assert _compact(repo)["resumed"] == 1
    assert events() == ["compacting", "compacted"]
    again = _compact(repo)
    assert again["resumed"] == 0 and again["held"] == 0, again
    assert events() == ["compacting", "compacted"], "the retry must not duplicate"


def test_a_recovered_registration_is_never_taken_for_a_leftover(repo, tmp_path):
    """Recovery recreates the registration under the anchor's old admin name and
    path. Moved aside with new detached work, it must survive the next run: the
    stub's pin does not hold that work, the registration's HEAD does."""
    a = _archive(repo, tmp_path, "revived")
    assert _compact(repo)["compacted"] == 1
    assert wl._recover(a["entry"], repo, {}) is True
    (a["path"] / "later.txt").write_text("made after recovery\n")
    _git(a["path"], "add", "-A")
    _git(a["path"], "commit", "-qm", "after recovery")
    later = _git(a["path"], "rev-parse", "HEAD").stdout.strip()
    a["path"].rename(tmp_path / "revived.moved")

    counts = _compact(repo)

    assert counts["resumed"] == 0 and counts["held"] == 0, counts
    assert str(a["path"]) in _registered(repo), "the recovered registration was pruned"
    _git(repo, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    _git(repo, "gc", "--prune=now", "-q")
    assert _commit_exists(repo, later)


def _held_then_reattached(repo, tmp_path, monkeypatch, name):
    """A compaction that stops before its anchor is removed, then a recovery,
    which REATTACHES the tree under that same registration (admin directory and
    all), then new detached work, then the checkout moved aside."""
    a = _archive(repo, tmp_path, name)
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda anchor: False)
    assert _compact(repo)["held"] == 1
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    admin = _admin(repo, a["entry"])
    assert (admin / wl.COMPACTION_MARK_FILE).is_file(), "control: the anchor carries the mark"
    report: dict = {}
    assert wl._recover(a["entry"], repo, report) is True
    assert report.get("mode") == "reattached", report
    (a["path"] / "later.txt").write_text("made after recovery\n")
    _git(a["path"], "add", "-A")
    _git(a["path"], "commit", "-qm", "after recovery")
    later = _git(a["path"], "rev-parse", "HEAD").stdout.strip()
    return a, admin, later


def _survives(repo, a, later) -> None:
    a["path"].rename(a["path"].with_name(a["path"].name + ".moved"))
    _compact(repo)
    assert str(a["path"]) in _registered(repo), "the reattached registration was pruned"
    _git(repo, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    _git(repo, "gc", "--prune=now", "-q")
    assert _commit_exists(repo, later), "the work made after recovery was collected"


def test_a_reattached_registration_is_never_taken_for_a_leftover(repo, tmp_path, monkeypatch):
    a, admin, later = _held_then_reattached(repo, tmp_path, monkeypatch, "reattached")
    assert not (admin / wl.COMPACTION_MARK_FILE).exists(), "reattach must retire the mark"
    _survives(repo, a, later)


def test_a_moved_registration_is_never_pruned_even_if_it_carries_the_mark(
    repo, tmp_path, monkeypatch
):
    """The mark is a claim, not proof: pruning also requires that the
    registration holds nothing beyond the pinned head, read at the moment of
    removal. Here the mark is put back by hand after the reattach."""
    a, admin, later = _held_then_reattached(repo, tmp_path, monkeypatch, "remarked")
    stub = json.loads(_stub(a["entry"]).read_text())
    (admin / wl.COMPACTION_MARK_FILE).write_text(stub["anchor_token"])
    _survives(repo, a, later)


def test_a_stub_without_an_anchor_token_stays_recoverable_and_holds_cleanup(
    repo, tmp_path, monkeypatch
):
    """A stub written before the token existed is still audited and recoverable;
    only the registration cleanup, which needs the token, is held."""
    a = _archive(repo, tmp_path, "tokenless")
    real = wl._prune_anchor
    monkeypatch.setattr(wl, "_prune_anchor", lambda *_a, **_k: False)
    assert _compact(repo)["compacted"] == 0
    monkeypatch.setattr(wl, "_prune_anchor", real)
    stub = json.loads(_stub(a["entry"]).read_text())
    stub.pop("anchor_token")
    _stub(a["entry"]).write_text(json.dumps(stub))
    assert str(a["path"]) in _registered(repo), "control: the leftover registration is there"

    counts = _compact(repo)

    assert counts["errors"] == 0 and counts["held"] == 1, counts
    assert str(a["path"]) in _registered(repo), "a token-less stub must not prune"
    _git(repo, "update-ref", "-d", _pin_ref(a["entry"]))
    assert _compact(repo)["repinned"] == 1, "the audit still repairs its pin"
    assert wl._recover(a["entry"], repo, {}) is True


class _Killed(BaseException):
    """A kill: nothing in the code under test may catch it."""


def test_a_recovery_killed_after_its_unlock_leaves_no_mark(repo, tmp_path, monkeypatch):
    """The reattach retires compaction's mark BEFORE it unlocks. A recovery
    killed straight after the unlock leaves a live registration without the
    mark, so a checkout moved aside with no new work is still never pruned."""
    a = _archive(repo, tmp_path, "killed")
    real_unlock = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda anchor: False)
    assert _compact(repo)["held"] == 1
    monkeypatch.setattr(wl, "_unlock_anchor", real_unlock)
    admin = _admin(repo, a["entry"])
    real_git = wl._run_git

    def unlock_then_die(cwd, args, *rest, **kw):
        out = real_git(cwd, args, *rest, **kw)
        if args[:2] == ["worktree", "unlock"]:
            raise _Killed
        return out

    monkeypatch.setattr(wl, "_run_git", unlock_then_die)
    with pytest.raises(_Killed):
        wl._recover(a["entry"], repo, {})
    monkeypatch.setattr(wl, "_run_git", real_git)
    assert _registered(repo)[str(a["path"])] is None, "control: the unlock ran"
    assert not (admin / wl.COMPACTION_MARK_FILE).exists(), "the mark outlived the unlock"

    a["path"].rename(tmp_path / "killed.moved")
    _compact(repo)
    assert str(a["path"]) in _registered(repo), "the revived registration was pruned"


def _held_compaction(repo, tmp_path, monkeypatch, name):
    """An archive whose compaction stopped with its anchor still locked."""
    a = _archive(repo, tmp_path, name)
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda anchor: False)
    assert _compact(repo)["held"] == 1
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    return a, _admin(repo, a["entry"])


def _nothing_lost(repo, a) -> None:
    assert str(a["path"]) in _registered(repo), "the registration was pruned"
    _gc_now(repo)
    assert _commit_exists(repo, a["head"])
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]


def test_a_recovery_killed_after_placing_the_tree_is_never_pruned(repo, tmp_path, monkeypatch):
    """Killed straight after the tree is placed: the registration is still
    locked but no longer marked, so no later run takes it for its anchor, even
    once the checkout is moved aside with no new work."""
    a, admin = _held_compaction(repo, tmp_path, monkeypatch, "placed")
    real_place = wl._place_tree

    def place_then_die(*args, **kw):
        real_place(*args, **kw)
        raise _Killed

    monkeypatch.setattr(wl, "_place_tree", place_then_die)
    with pytest.raises(_Killed):
        wl._recover(a["entry"], repo, {})
    monkeypatch.setattr(wl, "_place_tree", real_place)
    assert a["path"].is_dir(), "control: the tree was placed"
    assert not (admin / wl.COMPACTION_MARK_FILE).exists(), "the mark outlived the placement"

    assert _compact(repo)["held"] == 1
    a["path"].rename(tmp_path / "placed.moved")
    counts = _compact(repo)

    assert counts["resumed"] == 0 and counts["held"] == 1, counts
    assert a["archive"].exists()
    _nothing_lost(repo, a)


def test_a_recovery_killed_before_placing_the_tree_loses_nothing(repo, tmp_path, monkeypatch):
    """Killed after the mark is retired and before the tree is placed: the
    registration is locked, unmarked and directory-less. Compaction holds it,
    and a second recovery still brings the tree back."""
    a, admin = _held_compaction(repo, tmp_path, monkeypatch, "unplaced")
    real_place = wl._place_tree

    def die(*args, **kw):
        raise _Killed

    monkeypatch.setattr(wl, "_place_tree", die)
    with pytest.raises(_Killed):
        wl._recover(a["entry"], repo, {})
    monkeypatch.setattr(wl, "_place_tree", real_place)
    assert not a["path"].exists(), "control: nothing was placed"
    assert not (admin / wl.COMPACTION_MARK_FILE).exists(), "control: the mark was retired"

    counts = _compact(repo)

    assert counts["resumed"] == 0 and counts["held"] == 1, counts
    assert a["archive"].exists()
    _nothing_lost(repo, a)
    report: dict = {}
    assert wl._recover(a["entry"], repo, report) is True
    assert report.get("mode") == "reattached", report
    assert _git(a["path"], "rev-parse", "HEAD").stdout.strip() == a["head"]


def test_a_mark_that_cannot_be_retired_places_nothing(repo, tmp_path, monkeypatch):
    """The mark is removed before placement or not at all: when it cannot be,
    recovery stops with the registration still locked and marked."""
    a, admin = _held_compaction(repo, tmp_path, monkeypatch, "stuckmark")
    mark = admin / wl.COMPACTION_MARK_FILE
    mark.unlink()
    mark.mkdir()  # unlink() now fails with an OSError that is not "not found"
    report: dict = {}

    assert wl._recover(a["entry"], repo, report) is False

    assert report.get("incomplete") is True
    assert not a["path"].exists(), "the tree was placed although the mark stayed"
    assert _registered(repo)[str(a["path"])] == wl._archive_lock_reason(a["entry"])
    assert a["archive"].exists()


def test_a_held_branch_anchor_finishes_after_its_branch_is_deleted(repo, tmp_path, monkeypatch):
    """A locked anchor's HEAD moves only with the branch it names. Once the pin
    holds the head, a deleted branch must not hold the compaction for ever; the
    anchor's index, refs and reflog are still checked against the head."""
    a = _archive(repo, tmp_path, "brgone", branch="feat-gone")
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda anchor: False)
    assert _compact(repo)["held"] == 1
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    _git(repo, "update-ref", "-d", "refs/heads/feat-gone")

    counts = _compact(repo)

    assert counts["resumed"] == 1 and counts["held"] == 0, counts
    assert not a["archive"].exists()
    assert str(a["path"]) not in _registered(repo)
    assert _ref(repo, _pin_ref(a["entry"])) == a["head"]
    _gc_now(repo)
    assert _commit_exists(repo, a["head"])
    assert wl._recover(a["entry"], repo, {}) is True
    assert _git(a["path"], "rev-parse", "HEAD").stdout.strip() == a["head"]


def test_a_locked_anchor_with_a_commit_only_its_reflog_reaches_is_held(repo, tmp_path, monkeypatch):
    """The HEAD check is dropped for a locked anchor, never the reflog check: a
    commit only the anchor's reflog reaches keeps it."""
    a = _archive(repo, tmp_path, "reflogged")
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda anchor: False)
    assert _compact(repo)["held"] == 1
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    admin = _admin(repo, a["entry"])
    tree = _git(repo, "rev-parse", f"{a['head']}^{{tree}}").stdout.strip()
    extra = _git(
        repo, "commit-tree", tree, "-p", a["head"], "-m", "only in the reflog"
    ).stdout.strip()
    _git(repo, "--git-dir", str(admin), "update-ref", "-m", "later", "HEAD", extra)

    counts = _compact(repo)

    assert counts["held"] == 1, counts
    assert a["archive"].exists() and str(a["path"]) in _registered(repo)


def _ignored_user_file(rel: str):
    """Setup: ignore ``rel``'s top directory in a commit, then write a file the
    user made there. Ignored, so the reaper's status check reads the tree clean."""

    def setup(wt: Path) -> None:
        top = rel.split("/")[0]
        (wt / ".gitignore").write_text(f"{top}/\n")
        _git(wt, "add", ".gitignore")
        _git(wt, "commit", "-qm", f"ignore {top}")
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text("only copy\n")

    return setup


@pytest.mark.parametrize(
    "rel",
    ["node_modules/private-notes.txt", ".venv/notes.txt", "config/.generated/keep.txt"],
)
def test_an_ignored_file_only_the_archive_holds_keeps_it(repo, tmp_path, rel):
    """Expiry's allowlist (caches it may drop) is not compaction's: a file under
    an ignored directory is in no commit and no bundle, so deleting the tarball
    would lose its only copy."""
    a = _archive(repo, tmp_path, "ignored", setup=_ignored_user_file(rel))
    with __import__("tarfile").open(a["archive"]) as tf:
        assert any(m.name.endswith(rel) for m in tf), "control: the archive holds it"

    counts = _compact(repo)

    assert counts["compacted"] == 0, counts
    assert a["archive"].exists()
    assert rel in counts["kept"][a["entry"]], counts["kept"]


def test_python_bytecode_alone_still_compacts(repo, tmp_path):
    """Bytecode is the one cache every tested worktree carries; it is rebuilt
    from the sources the commit holds, so it never keeps an archive."""

    def setup(wt: Path) -> None:
        (wt / "pkg" / "__pycache__").mkdir(parents=True)
        (wt / "pkg" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0bytecode")
        (wt / ".gitignore").write_text("__pycache__/\n")
        _git(wt, "add", ".gitignore")
        _git(wt, "commit", "-qm", "ignore bytecode")

    _archive(repo, tmp_path, "bytecode", setup=setup)
    assert _compact(repo)["compacted"] == 1


def test_a_second_recovery_never_replaces_a_registration_with_later_work(repo, tmp_path):
    """Recovering a stub recreates the worktree; recovering it AGAIN, after new
    detached work and a move, must not `worktree add --force` over that
    registration: the pin holds only the archived head."""
    a = _archive(repo, tmp_path, "twice")
    assert _compact(repo)["compacted"] == 1
    assert wl._recover(a["entry"], repo, {}) is True
    (a["path"] / "later.txt").write_text("made after recovery\n")
    _git(a["path"], "add", "-A")
    _git(a["path"], "commit", "-qm", "after recovery")
    later = _git(a["path"], "rev-parse", "HEAD").stdout.strip()
    moved = tmp_path / "twice.moved"
    a["path"].rename(moved)

    assert wl._recover(a["entry"], repo, {}, dry_run=True) is False
    assert wl._recover(a["entry"], repo, {}) is False

    assert str(a["path"]) in _registered(repo), "the later registration was replaced"
    _git(repo, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    _git(repo, "gc", "--prune=now", "-q")
    assert _commit_exists(repo, later)
    _git(repo, "worktree", "repair", str(moved))
    assert _git(moved, "rev-parse", "HEAD").stdout.strip() == later


def test_a_second_recovery_over_a_clean_registration_still_works(repo, tmp_path):
    """A registration left at the pinned head with nothing beyond it is
    replaced, as before: refusing it would strand a lossless recovery."""
    a = _archive(repo, tmp_path, "clean2")
    assert _compact(repo)["compacted"] == 1
    assert wl._recover(a["entry"], repo, {}) is True
    a["path"].rename(tmp_path / "clean2.moved")
    assert wl._recover(a["entry"], repo, {}) is True
    assert _git(a["path"], "rev-parse", "HEAD").stdout.strip() == a["head"]


def test_the_pin_is_created_only_when_absent(repo, tmp_path):
    """`update-ref <ref> <new> ""` is create-only (git-update-ref(1)); an existing
    pin at another head is never overwritten."""
    a = _archive(repo, tmp_path, "createonly")
    main = _git(repo, "rev-parse", "main").stdout.strip()
    _git(repo, "update-ref", _pin_ref(a["entry"]), main)
    _compact(repo)
    assert _ref(repo, _pin_ref(a["entry"])) == main
    assert os.path.exists(a["archive"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bundle", "../../outside.bundle"),
        ("bundle", "other-20260101.compacted.bundle"),
        ("bundle_sha256", None),
        ("bundle_sha256", "abc"),
        ("anchor_admin", ".."),
        ("anchor_admin", "a/b"),
        ("original_path", "relative/path"),
        ("original_path", "-x"),
        ("pin", "refs/heads/main"),
        ("head", "HEAD"),
    ],
)
def test_a_stub_field_of_the_wrong_shape_is_not_trusted(repo, tmp_path, field, value):
    """A corrupted or hand-edited stub must not steer a fetch, a deletion or an
    argv: every field a reader uses has one accepted shape."""
    a = _archive(repo, tmp_path, "shape")
    assert _compact(repo)["compacted"] == 1
    stub = json.loads(_stub(a["entry"]).read_text())
    compaction = wl._load_compaction_module()
    assert compaction.valid_stub(wl._ThisModule(), stub, a["entry"]), "control: the real stub"
    stub[field] = value
    assert not compaction.valid_stub(wl._ThisModule(), stub, a["entry"]), (field, value)


def test_the_bundle_is_private(repo, tmp_path):
    a = _archive(repo, tmp_path, "private")
    assert _compact(repo)["compacted"] == 1
    stub = json.loads(_stub(a["entry"]).read_text())
    assert (wl.TRASH_DIR / stub["bundle"]).stat().st_mode & 0o077 == 0
    assert _stub(a["entry"]).stat().st_mode & 0o077 == 0


def test_a_symlinked_bundle_is_never_fetched(repo, tmp_path):
    a = _archive(repo, tmp_path, "linked")
    assert _compact(repo)["compacted"] == 1
    stub = json.loads(_stub(a["entry"]).read_text())
    bundle = wl.TRASH_DIR / stub["bundle"]
    moved = tmp_path / "moved.bundle"
    bundle.rename(moved)
    bundle.symlink_to(moved)
    _git(repo, "update-ref", "-d", _pin_ref(a["entry"]))
    _gc_now(repo)
    counts = _compact(repo)
    assert counts["errors"] == 1 and counts["repinned"] == 0, counts


def test_the_fixture_git_ignores_a_callers_repository_variables(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repo"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere.index"))
    assert _git(repo, "rev-parse", "--absolute-git-dir").stdout.strip() == str(repo / ".git")
    assert _git(repo, "ls-files").stdout.split() == ["seed.txt"]


def test_an_inherited_index_variable_does_not_steer_the_gates(repo, tmp_path, monkeypatch):
    """A caller's GIT_INDEX_FILE must not make the index gate read another index."""
    _archive(repo, tmp_path, "envvar")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere.index"))
    assert _compact(repo)["compacted"] == 1


def test_a_locked_anchor_whose_detached_head_moved_without_a_reflog_is_held(
    repo, tmp_path, monkeypatch
):
    """A held compaction's locked anchor whose detached HEAD was written straight
    to a new commit, with no reflog entry, still holds: the commit is reachable
    only from that registration, so pruning it would let gc collect it."""
    a = _archive(repo, tmp_path, "rawhead")
    real = wl._unlock_anchor
    monkeypatch.setattr(wl, "_unlock_anchor", lambda *_a, **_k: False)
    assert _compact(repo)["held"] == 1
    monkeypatch.setattr(wl, "_unlock_anchor", real)
    admin = _admin(repo, a["entry"])
    tree = _git(repo, "rev-parse", f"{a['head']}^{{tree}}").stdout.strip()
    extra = _git(repo, "commit-tree", tree, "-p", a["head"], "-m", "anchor-only").stdout.strip()
    (admin / "HEAD").write_text(extra + "\n")  # a raw write: no reflog entry
    counts = _compact(repo)
    assert counts["resumed"] == 0 and counts["held"] == 1, counts
    _gc_now(repo)
    assert _commit_exists(repo, extra), "the anchor-only commit was collected"


def test_a_second_recovery_over_another_archives_anchor_names_that_archive(repo, tmp_path, capsys):
    """Recover A, commit, the path is archived again as B (B's locked anchor now
    sits at the path), then recover A again: the refusal names archive B to
    recover instead, never advice to remove B's anchor, its history's only hold."""
    a = _archive(repo, tmp_path, "twice")
    assert _compact(repo)["compacted"] == 1
    assert wl._recover(a["entry"], repo, {}) is True
    (a["path"] / "b.txt").write_text("b\n")
    _git(a["path"], "add", "-A")
    _git(a["path"], "commit", "-qm", "b")
    rec = next(x for x in wl._list_worktrees(repo) if x["path"] == str(a["path"]))
    assert wl._trash_worktree(rec, repo, lane="unmerged") is True
    capsys.readouterr()
    assert wl._recover(a["entry"] + ".compacted.json", repo, {}) is False
    err = capsys.readouterr().err
    assert "registered to another archive" in err, err
    assert "worktree remove" not in err, err
