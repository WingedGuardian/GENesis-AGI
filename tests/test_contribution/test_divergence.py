"""Unit tests for genesis.contribution.divergence.

Uses subprocess mocking to avoid touching real git repos. One
integration-style test creates a throwaway git repo to verify
the happy path against a real `git merge-tree`.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from genesis.contribution import divergence


def _make_mock_result(returncode: int, stdout: str, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_clean_merge_reports_clean():
    """Successful git merge-tree (rc=0) → clean result."""
    mock_ret = _make_mock_result(0, "a" * 40 + "\n")
    with patch("subprocess.run", return_value=mock_ret):
        r = divergence.check_divergence("a" * 40, "b" * 40)
    assert r.clean is True
    assert r.conflict_files == []


def test_conflict_parses_files():
    """Non-zero rc with a tree sha + file list parses correctly."""
    stdout = (
        "a" * 40 + "\n"
        "src/foo.py\n"
        "src/bar.py\n"
        "\n"
        "CONFLICT (content): Merge conflict in src/foo.py\n"
    )
    mock_ret = _make_mock_result(1, stdout)
    with patch("subprocess.run", return_value=mock_ret):
        r = divergence.check_divergence("a" * 40, "b" * 40)
    assert r.clean is False
    assert r.conflict_files == ["src/foo.py", "src/bar.py"]
    assert "src/foo.py" in r.message
    assert "src/bar.py" in r.message
    assert "git pull" in r.message  # actionable instruction


def test_many_conflict_files_truncated_in_message():
    files = [f"file{i}.py" for i in range(10)]
    stdout = "a" * 40 + "\n" + "\n".join(files) + "\n"
    mock_ret = _make_mock_result(1, stdout)
    with patch("subprocess.run", return_value=mock_ret):
        r = divergence.check_divergence("a" * 40, "b" * 40)
    assert r.clean is False
    assert len(r.conflict_files) == 10
    assert "+5 more" in r.message


def test_conflict_without_parseable_output():
    mock_ret = _make_mock_result(1, "", stderr="fatal: bad revision")
    with patch("subprocess.run", return_value=mock_ret):
        r = divergence.check_divergence("a" * 40, "b" * 40)
    assert r.clean is False
    assert "fatal: bad revision" in r.message


def test_timeout_reported():
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)
    with patch("subprocess.run", side_effect=raise_timeout):
        r = divergence.check_divergence("a" * 40, "b" * 40)
    assert r.clean is False
    assert "timed out" in r.message


def test_git_missing_reported():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        r = divergence.check_divergence("a" * 40, "b" * 40)
    assert r.clean is False
    assert "git binary not found" in r.message


def test_integration_clean_merge(tmp_path):
    """Real git repo: fork adds a file, upstream untouched → clean."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True,
            text=True, check=True,
        )

    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    run("add", "README.md")
    run("commit", "-q", "-m", "base")
    base_sha = run("rev-parse", "HEAD").stdout.strip()

    (repo / "fix.py").write_text("print('fix')\n")
    run("add", "fix.py")
    run("commit", "-q", "-m", "fix")
    fix_sha = run("rev-parse", "HEAD").stdout.strip()

    # Merge fix_sha onto base_sha — trivial, should be clean
    r = divergence.check_divergence(fix_sha, base_sha, repo_path=repo)
    assert r.clean is True, f"expected clean merge, got: {r.message}"
