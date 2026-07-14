"""Tests for scripts/git_repair.py — operator-invoked corrupt-.git repair.

The tool is stdlib-only and dry-run-by-default. These tests use real ``file://``
origin fixtures (no network) and reproduce the outage fingerprint (all-NUL loose
objects / zeroed config). The load-bearing safety invariants under test:
  * dry-run mutates NOTHING (.git fingerprint identical before/after);
  * corrupt loose objects are MOVED, not overwritten (they are mode 0444);
  * ``git fetch --refetch`` (not a plain fetch) is what backfills a quarantined
    object — proven by the repaired object becoming readable again;
  * the last-resort re-clone NEVER swaps ``.git`` itself (prints steps only) and
    always enumerates the linked worktrees a swap would orphan;
  * exit codes: 0 healthy · 1 residual/dry-run-with-issues · 2 aborted.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "git_repair.py"
_spec = importlib.util.spec_from_file_location("git_repair", _SCRIPT)
gr = importlib.util.module_from_spec(_spec)
sys.modules["git_repair"] = gr
_spec.loader.exec_module(gr)


# ─── fixtures / helpers ──────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """A bare ``file://`` origin + a work clone with real history (loose objs)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "s@s")
    _git(work, "config", "user.name", "s")
    for i in range(3):
        (work / f"f{i}.txt").write_text(f"l{i}\n")
        _git(work, "add", f"f{i}.txt")
        _git(work, "commit", "-q", "-m", f"c{i}")
    (work / "extra.txt").write_text("payload\n")
    _git(work, "add", "extra.txt")
    _git(work, "commit", "-q", "-m", "c-loose")
    _git(work, "push", "-q", "origin", "main")
    return origin, work


def _loose_blob(work: Path) -> Path:
    sha = _git(work, "rev-parse", "HEAD:extra.txt").strip()
    return work / ".git" / "objects" / sha[:2] / sha[2:]


def _zero(path: Path) -> None:
    """Overwrite a file with NUL of the same length (mimics the block-zeroing);
    chmod first because git loose objects are created read-only (0444)."""
    n = path.stat().st_size
    os.chmod(path, 0o644)
    path.write_bytes(b"\x00" * n)


def _fingerprint(git_dir: Path) -> list[tuple[str, int, str]]:
    out = []
    for p in sorted(git_dir.rglob("*")):
        if p.is_file():
            out.append(
                (
                    str(p.relative_to(git_dir)),
                    p.stat().st_size,
                    hashlib.sha1(p.read_bytes()).hexdigest(),
                )
            )
    return out


def _run_tool(repo: Path, *args: str, home: Path) -> subprocess.CompletedProcess:
    """Invoke the tool as a subprocess with HOME redirected so its capture/log
    side effects land under the test tmp dir, not the real ~/tmp."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )


def _fsck_clean(repo: Path) -> bool:
    r = subprocess.run(
        ["git", "-C", str(repo), "fsck", "--full", "--no-progress"], capture_output=True, text=True
    )
    bad = [
        ln
        for ln in r.stderr.splitlines()
        if "corrupt" in ln or "missing" in ln or ln.startswith(("error:", "fatal:"))
    ]
    return r.returncode == 0 and not bad


# ─── diagnosis (pure) ────────────────────────────────────────────────────────


def test_diagnose_healthy(repos):
    _origin, work = repos
    d = gr.diagnose(work)
    assert d.healthy, d.render()
    assert not d.corrupt_objects


def test_diagnose_detects_zeroed_object(repos):
    _origin, work = repos
    blob = _loose_blob(work)
    _zero(blob)
    d = gr.diagnose(work)
    assert not d.healthy
    assert blob.resolve() in d.corrupt_objects
    # the fast zero-scan and fsck both flag it
    names = {c.name: c.ok for c in d.checks}
    assert names["no zeroed loose objects"] is False
    assert names["fsck --full clean"] is False


def test_scan_zeroed_only_flags_all_nul(repos):
    _origin, work = repos
    # a valid loose object must NOT be flagged
    assert gr._scan_zeroed_loose(work / ".git") == []
    _zero(_loose_blob(work))
    assert len(gr._scan_zeroed_loose(work / ".git")) == 1


# ─── URL resolution chain ────────────────────────────────────────────────────


def test_resolve_url_prefers_existing_config(repos, monkeypatch):
    origin, work = repos
    monkeypatch.delenv("GENESIS_REPO_URL", raising=False)
    assert gr._resolve_url(work, remote_url="X", capture=None) == str(origin)


def test_resolve_url_falls_back_to_arg_then_env(repos, tmp_path, monkeypatch):
    origin, work = repos
    _zero(work / ".git" / "config")  # existing config now unreadable
    monkeypatch.delenv("GENESIS_REPO_URL", raising=False)
    assert gr._resolve_url(work, remote_url="ARG_URL", capture=None) == "ARG_URL"
    monkeypatch.setenv("GENESIS_REPO_URL", "ENV_URL")
    assert gr._resolve_url(work, remote_url=None, capture=None) == "ENV_URL"


def test_resolve_url_none_when_unresolvable(repos, monkeypatch):
    _origin, work = repos
    _zero(work / ".git" / "config")
    monkeypatch.delenv("GENESIS_REPO_URL", raising=False)
    assert gr._resolve_url(work, remote_url=None, capture=None) is None


# ─── dry-run mutates nothing ─────────────────────────────────────────────────


def test_dry_run_mutates_nothing(repos, tmp_path):
    _origin, work = repos
    _zero(_loose_blob(work))
    before = _fingerprint(work / ".git")
    r = _run_tool(work, home=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr  # issues found, dry-run
    after = _fingerprint(work / ".git")
    assert before == after  # .git byte-identical — nothing moved/written


# ─── apply repairs ───────────────────────────────────────────────────────────


def test_apply_repairs_corrupt_object(repos, tmp_path):
    _origin, work = repos
    blob = _loose_blob(work)
    _zero(blob)
    r = _run_tool(work, "--apply", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _fsck_clean(work)
    # object is readable again and the corrupt one was quarantined (moved aside)
    assert list((work / ".git" / "RECOVERY-corrupt-objects").glob("*/*"))


def test_apply_repairs_zeroed_config(repos, tmp_path):
    origin, work = repos
    _zero(work / ".git" / "config")
    r = _run_tool(work, "--apply", "--remote-url", str(origin), home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(work, "config", "--get", "remote.origin.url").strip() == str(origin)
    assert _fsck_clean(work)


def test_zeroed_config_apply_aborts_without_url(repos, tmp_path, monkeypatch):
    _origin, work = repos
    _zero(work / ".git" / "config")
    # no --remote-url, no env, no readable config → exit 2 abort
    env_home = tmp_path
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(work), "--apply"],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "GENESIS_REPO_URL"}
        | {"HOME": str(env_home)},
    )
    assert r.returncode == 2
    assert "no origin URL" in (r.stdout + r.stderr)


def test_healthy_repo_exit_zero(repos, tmp_path):
    _origin, work = repos
    r = _run_tool(work, home=tmp_path)
    assert r.returncode == 0
    assert "HEALTHY" in r.stdout


def test_idempotent_second_apply(repos, tmp_path):
    _origin, work = repos
    _zero(_loose_blob(work))
    assert _run_tool(work, "--apply", home=tmp_path).returncode == 0
    r2 = _run_tool(work, "--apply", home=tmp_path)
    assert r2.returncode == 0
    assert "HEALTHY" in r2.stdout


# ─── linked-worktree safety ──────────────────────────────────────────────────


def test_refuses_when_pointed_at_linked_worktree(repos, tmp_path):
    _origin, work = repos
    wt = tmp_path / "wt"
    _git(work, "worktree", "add", "-q", str(wt), "-b", "sidebr")
    # a linked worktree's .git is a FILE → tool must abort (exit 2), not operate
    r = _run_tool(wt, home=tmp_path)
    assert r.returncode == 2
    assert "linked worktree" in (r.stdout + r.stderr).lower()


def test_guided_reclone_lists_worktrees_and_never_swaps(repos, tmp_path, capsys):
    origin, work = repos
    wt = tmp_path / "wt"
    _git(work, "worktree", "add", "-q", str(wt), "-b", "sidebr")
    git_before = _fingerprint(work / ".git")
    gr.HOME = tmp_path  # redirect the temp clone destination
    gr.guided_reclone(work, str(origin), apply=True)
    out = capsys.readouterr().out
    # printed the swap runbook, listed the linked worktree, never swapped .git
    assert "RUN THESE STEPS BY HAND" in out
    assert str(wt) in out
    assert "worktree add" in out
    assert (work / ".git").is_dir()
    assert _fingerprint(work / ".git") == git_before  # main .git untouched


def test_guided_reclone_dry_run_does_not_clone(repos, tmp_path, capsys):
    origin, work = repos
    gr.HOME = tmp_path
    gr.guided_reclone(work, str(origin), apply=False)
    out = capsys.readouterr().out
    assert "WOULD CLONE" in out


# ─── origin reachability is a precondition, NOT a health check (Codex P2) ─────


def test_healthy_repo_with_unreachable_origin_is_healthy(repos, tmp_path):
    _origin, work = repos
    # break origin reachability WITHOUT touching local .git integrity
    _git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    d = gr.diagnose(work)
    assert d.origin_reachable is False
    assert d.healthy  # local git is intact → healthy despite an unreachable origin
    r = _run_tool(work, home=tmp_path)
    assert r.returncode == 0
    assert "HEALTHY" in r.stdout


# ─── corrupt packed-refs repair (Codex P1) ───────────────────────────────────


def test_diagnose_flags_corrupt_packed_refs(repos):
    _origin, work = repos
    _git(work, "pack-refs", "--all")  # move refs into packed-refs (prunes loose)
    _zero(work / ".git" / "packed-refs")
    d = gr.diagnose(work)
    assert d.packed_refs_corrupt
    assert not d.healthy


def test_apply_repairs_zeroed_packed_refs(repos, tmp_path):
    _origin, work = repos
    _git(work, "pack-refs", "--all")
    pr = work / ".git" / "packed-refs"
    assert pr.is_file()
    _zero(pr)
    # sanity: git itself is now broken on this repo
    assert (
        subprocess.run(["git", "-C", str(work), "for-each-ref"], capture_output=True).returncode
        != 0
    )
    r = _run_tool(work, "--apply", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _fsck_clean(work)
    # branch tip restored (from remote ref) + corrupt packed-refs quarantined
    assert _git(work, "rev-parse", "--verify", "refs/heads/main").strip()
    assert list((work / ".git" / "RECOVERY-corrupt-objects").glob("*/packed-refs"))


def test_packed_refs_tip_restored_from_reflog_when_origin_down(repos, tmp_path):
    """With packed-refs corrupt AND origin unreachable, the branch tip must still
    be recoverable from the reflog (the offline path)."""
    _origin, work = repos
    want = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "pack-refs", "--all")
    _git(work, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # origin down
    _zero(work / ".git" / "packed-refs")
    r = _run_tool(work, "--apply", home=tmp_path)
    # tip restored from reflog even though refetch was impossible
    assert _git(work, "rev-parse", "--verify", "refs/heads/main").strip() == want
    assert r.returncode == 0, r.stdout + r.stderr
