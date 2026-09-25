from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "lib" / "deploy_checkout.sh"

_INHERITED_PREFIXES = ("GENESIS_", "GIT_")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_INHERITED_PREFIXES)
    }
    env.update(overrides)
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _repo(tmp_path: Path, *, branch: str = "main") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("initial\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _run_helper(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{HELPER}"\n{script}'],
        capture_output=True,
        text=True,
        env=_clean_env(**env),
    )


def test_resolve_uses_the_fetch_remote_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/public/stable", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/public/HEAD", "refs/remotes/public/stable")
    _git(repo, "update-ref", "refs/remotes/origin/other", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/other")

    result = _run_helper(
        f'genesis_resolve_deploy_branch "{repo}" public',
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "stable"


def test_resolve_honors_explicit_env_override(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = _run_helper(
        f'genesis_resolve_deploy_branch "{repo}" public',
        GENESIS_DEPLOY_BRANCH="release/test",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "release/test"


def test_resolve_rejects_invalid_branch_name(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = _run_helper(
        f'genesis_resolve_deploy_branch "{repo}" public',
        GENESIS_DEPLOY_BRANCH="../escape",
    )

    assert result.returncode != 0
    assert "invalid Genesis deploy branch" in result.stderr


def test_assert_accepts_primary_checkout_on_deploy_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path, branch="main")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode == 0, result.stderr


def test_assert_refuses_non_deploy_branch_before_mutation(tmp_path: Path) -> None:
    repo = _repo(tmp_path, branch="feature")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode != 0
    assert "refusing to deploy from branch 'feature'" in result.stderr
    assert "Deploy branch: main" in result.stderr


def test_assert_refuses_detached_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "--detach", "HEAD")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode != 0
    assert "must not run from detached HEAD" in result.stderr


def test_assert_refuses_linked_worktree_at_arbitrary_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree = tmp_path / "outside-docs" / "linked"
    _git(repo, "worktree", "add", str(worktree), "-b", "linked")

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{worktree}" main',
    )

    assert result.returncode != 0
    assert "must not run from a linked worktree" in result.stderr
    assert "Run from the primary checkout" in result.stderr


def test_assert_refuses_non_git_directory(tmp_path: Path) -> None:
    repo = tmp_path / "not-a-repo"
    repo.mkdir()

    result = _run_helper(
        f'genesis_assert_deploy_checkout "{repo}" main',
    )

    assert result.returncode != 0
    assert "must run from a Git checkout" in result.stderr
