"""Regression tests for scripts/check_hook_versions_complete.sh (PR-4).

The checker walks the FULL git history of every tracked hook and fails if any
shipped version's sha256 is absent from .genesis-hook-versions — the gap that
wedges a community install (sync-hooks.sh mis-labels an unrecorded hash as
"user-modified" and never updates it). These tests build hermetic throwaway
repos so they never depend on the real repo's history, plus one guard that the
real tree stays complete.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_hook_versions_complete.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy(_SCRIPT, path / "scripts" / "check_hook_versions_complete.sh")


def _write_hook(path: Path, name: str, body: str) -> str:
    """Write a hook version, commit it, return its sha256."""
    f = path / "scripts" / "hooks" / name
    f.write_text(body)
    _git(path, "add", f"scripts/hooks/{name}")
    _git(path, "commit", "-q", "-m", f"hook {name}: {body[:8]}", "--no-verify")
    return hashlib.sha256(body.encode()).hexdigest()


def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "scripts/check_hook_versions_complete.sh"],
        cwd=path,
        capture_output=True,
        text=True,
    )


def test_detects_unrecorded_version(tmp_path: Path) -> None:
    """Two versions ship but only the latest is recorded → exit 1, names the gap."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    old_hash = _write_hook(repo, "pre-commit", "#v1\n")
    new_hash = _write_hook(repo, "pre-commit", "#v2\n")
    # Ledger records ONLY the current version — the intermediate one is the gap.
    (repo / ".genesis-hook-versions").write_text(f"pre-commit:{new_hash}\n")

    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert old_hash in result.stderr  # the missing version is reported
    assert new_hash not in result.stderr  # the recorded one is not flagged


def test_passes_when_all_recorded(tmp_path: Path) -> None:
    """Every shipped version recorded → exit 0."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    h1 = _write_hook(repo, "pre-commit", "#v1\n")
    h2 = _write_hook(repo, "pre-commit", "#v2\n")
    (repo / ".genesis-hook-versions").write_text(f"pre-commit:{h1}\npre-commit:{h2}\n")

    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_skips_on_shallow_clone(tmp_path: Path) -> None:
    """A shallow clone can't see history → skip (exit 0), never a false pass/fail."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    _write_hook(origin, "pre-commit", "#v1\n")
    _write_hook(origin, "pre-commit", "#v2\n")
    # Record NOTHING — on a full clone this would fail; a shallow clone must skip.
    (origin / ".genesis-hook-versions").write_text("# empty\n")
    # Stage the ledger and the (copied-but-uncommitted) checker script explicitly,
    # so the depth-1 clone's tip carries both.
    _git(origin, "add", ".genesis-hook-versions", "scripts/check_hook_versions_complete.sh")
    _git(origin, "commit", "-q", "-m", "ledger", "--no-verify")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "1", "-q", origin.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )
    # The clone's script came from origin's tip; ensure it's present.
    assert (shallow / "scripts" / "check_hook_versions_complete.sh").is_file()

    result = _run(shallow)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "shallow" in result.stdout.lower()


def test_fails_closed_on_unreadable_history(tmp_path: Path) -> None:
    """A version whose blob object is UNREADABLE (partial clone / corruption) must
    ERROR, not be silently skipped as 'path absent' → the backstop would false-pass.

    Deterministic: commit two versions, record BOTH (so a healthy repo PASSES),
    then delete the loose object for the historical v1 blob. The tree still names
    the path (ls-tree succeeds) but `git show` on the blob fails → fail closed.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    h1 = _write_hook(repo, "pre-commit", "#v1\n")
    h2 = _write_hook(repo, "pre-commit", "#v2\n")
    # Record BOTH versions: on a healthy repo this PASSES. The only way it can exit
    # non-zero is the fail-closed read-failure path (not a real ledger gap).
    (repo / ".genesis-hook-versions").write_text(f"pre-commit:{h1}\npre-commit:{h2}\n")

    # Locate v1's blob object and delete it (loose in a fresh repo — no repack yet).
    blob = subprocess.run(
        ["git", "rev-parse", "HEAD~1:scripts/hooks/pre-commit"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    obj = repo / ".git" / "objects" / blob[:2] / blob[2:]
    if not obj.is_file():
        pytest.skip("v1 blob is packed, not loose — cannot deterministically remove it")
    obj.unlink()

    # Sanity: the tree still names the path, but the blob is now unreadable.
    assert (
        subprocess.run(
            ["git", "show", "HEAD~1:scripts/hooks/pre-commit"], cwd=repo, capture_output=True
        ).returncode
        != 0
    )

    result = _run(repo)
    assert result.returncode == 1, (
        f"expected fail-closed, got {result.returncode}: {result.stdout}{result.stderr}"
    )
    assert "verify" in result.stderr.lower() or "unreadable" in result.stderr.lower()


def test_real_tree_is_complete() -> None:
    """The actual repo's ledger records every shipped hook version (skip if shallow)."""
    is_shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if is_shallow == "true":
        pytest.skip("shallow clone — completeness is enforced by the fetch-depth:0 CI job")
    result = subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
