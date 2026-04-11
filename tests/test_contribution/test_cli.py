"""Unit + e2e tests for genesis.contribution.cli."""
from __future__ import annotations

import argparse
import json
import subprocess
from unittest.mock import AsyncMock

import pytest

from genesis.contribution import cli
from genesis.contribution.findings import (
    DivergenceResult,
    Finding,
    FindingKind,
    ReviewResult,
    SanitizerResult,
    Severity,
    VersionGateResult,
)
from genesis.contribution.pr_opener import PRCreationResult


@pytest.fixture
def default_args(tmp_path, monkeypatch):
    """Base argparse namespace with safe defaults for a dry-run."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis_home"))
    return argparse.Namespace(
        sha="abc123",
        identify=False,
        list=False,
        repo=str(tmp_path),
        upstream_sha="upstream1",
        target_repo=None,
        yes=True,
        dry_run=True,
        skip_review=True,
        allow_non_fix=False,
        func=cli.run,
    )


@pytest.fixture
def happy_commit():
    return cli.CommitInfo(
        sha="abc123",
        subject="fix(parser): handle empty input",
        body="body",
        diff=(
            "diff --git a/src/parser.py b/src/parser.py\n"
            "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n+pass\n"
        ),
    )


def test_list_empty_pending(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    args = argparse.Namespace(list=True, sha=None, func=cli.run)
    rc = cli.run(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no pending offers" in out


def test_list_shows_markers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    pending = tmp_path / "pending-offers"
    pending.mkdir()
    (pending / "abc.json").write_text(json.dumps({
        "sha": "abcdef1234567890",
        "subject": "fix: x",
        "created_at": "2026-04-11T00:00:00Z",
    }))
    args = argparse.Namespace(list=True, sha=None, func=cli.run)
    rc = cli.run(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "abcdef123456" in out
    assert "fix: x" in out


def test_list_corrupt_marker_tolerated(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    pending = tmp_path / "pending-offers"
    pending.mkdir()
    (pending / "bad.json").write_text("not json")
    args = argparse.Namespace(list=True, sha=None, func=cli.run)
    rc = cli.run(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "unreadable" in out


def test_missing_sha_errors():
    args = argparse.Namespace(list=False, sha=None, func=cli.run)
    rc = cli.run(args)
    assert rc == 2


def test_non_fix_commit_blocked(default_args, happy_commit, monkeypatch):
    """MVP bug-fix-only enforcement."""
    happy_commit.subject = "feat: add new thing"
    monkeypatch.setattr(cli, "read_commit", lambda *a, **k: happy_commit)
    rc = cli.run(default_args)
    assert rc == 2


def test_non_fix_with_override(default_args, happy_commit, monkeypatch):
    happy_commit.subject = "refactor: x"
    default_args.allow_non_fix = True
    _install_happy_mocks(monkeypatch, happy_commit)
    rc = cli.run(default_args)
    assert rc == 0


def _install_happy_mocks(monkeypatch, commit):
    """Wire up mocks for a successful dry-run e2e."""
    monkeypatch.setattr(cli, "read_commit", lambda *a, **k: commit)
    monkeypatch.setattr(cli, "fetch_upstream_head", lambda **k: "upstream1")
    monkeypatch.setattr(cli, "read_install_sha", lambda *a, **k: "install1")
    monkeypatch.setattr(
        cli, "check_divergence",
        lambda *a, **k: DivergenceResult(clean=True, message="clean"),
    )
    monkeypatch.setattr(
        cli, "check_version_gate",
        AsyncMock(return_value=VersionGateResult(
            already_fixed=False, confidence=0, version_match=True,
        )),
    )
    monkeypatch.setattr(
        cli, "scan_diff",
        lambda *a, **k: SanitizerResult(ok=True, scanners_run=["portability"]),
    )
    monkeypatch.setattr(
        cli, "run_review_chain",
        lambda *a, **k: ReviewResult(available=False),
    )
    monkeypatch.setattr(
        cli, "format_version_string",
        lambda *a, **k: "3.0.0a1@abc1234",
    )
    monkeypatch.setattr(
        cli, "create_pr",
        lambda **k: PRCreationResult(
            ok=True, branch="community/12345678-abc1234",
            body="body", url=None, error="dry-run: no PR created",
        ),
    )


def test_happy_path_dry_run(default_args, happy_commit, monkeypatch, capsys):
    _install_happy_mocks(monkeypatch, happy_commit)
    rc = cli.run(default_args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "step 1" in out
    assert "step 8" in out
    assert "community/" in out


def test_divergence_blocks(default_args, happy_commit, monkeypatch):
    _install_happy_mocks(monkeypatch, happy_commit)
    monkeypatch.setattr(
        cli, "check_divergence",
        lambda *a, **k: DivergenceResult(
            clean=False, conflict_files=["x.py"],
            message="conflicts in x.py",
        ),
    )
    rc = cli.run(default_args)
    assert rc == 3


def test_version_gate_blocks(default_args, happy_commit, monkeypatch):
    _install_happy_mocks(monkeypatch, happy_commit)
    monkeypatch.setattr(
        cli, "check_version_gate",
        AsyncMock(return_value=VersionGateResult(
            already_fixed=True, confidence=90, matched_sha="def456",
            reasoning="duplicate of def456",
        )),
    )
    rc = cli.run(default_args)
    assert rc == 4


def test_sanitizer_blocks(default_args, happy_commit, monkeypatch, capsys):
    _install_happy_mocks(monkeypatch, happy_commit)
    monkeypatch.setattr(
        cli, "scan_diff",
        lambda *a, **k: SanitizerResult(
            ok=False,
            findings=[Finding(
                kind=FindingKind.SECRET, severity=Severity.BLOCK,
                message="api key", scanner="detect-secrets",
            )],
        ),
    )
    rc = cli.run(default_args)
    assert rc == 5
    err = capsys.readouterr().err
    assert "api key" in err


def test_user_declines_consent(default_args, happy_commit, monkeypatch, capsys):
    _install_happy_mocks(monkeypatch, happy_commit)
    default_args.yes = False
    # Non-TTY stdin → auto-decline
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = cli.run(default_args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "non-interactive" in out


def test_pr_creation_failure(default_args, happy_commit, monkeypatch):
    _install_happy_mocks(monkeypatch, happy_commit)
    monkeypatch.setattr(
        cli, "create_pr",
        lambda **k: PRCreationResult(ok=False, error="no auth"),
    )
    rc = cli.run(default_args)
    assert rc == 6


def test_merge_commit_rejected_codex_p1(tmp_path):
    """Codex review P1 regression: merge commits must be rejected at
    read_commit time. `git show --format= <merge-sha>` produces an
    empty diff for clean merges, which would otherwise pass through
    the pipeline as if there was nothing to sanitize."""
    repo = tmp_path / "r"
    repo.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True,
            text=True, check=True,
        )

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "a.py").write_text("base\n")
    run("add", "a.py")
    run("commit", "-q", "-m", "base")
    run("checkout", "-q", "-b", "side")
    (repo / "b.py").write_text("side\n")
    run("add", "b.py")
    run("commit", "-q", "-m", "side")
    run("checkout", "-q", "main")
    (repo / "c.py").write_text("main\n")
    run("add", "c.py")
    run("commit", "-q", "-m", "main")
    run("merge", "-q", "--no-ff", "side", "-m", "merge")
    merge_sha = run("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(RuntimeError, match="merge commit"):
        cli.read_commit(merge_sha, repo_path=repo)


def test_read_commit_invokes_git(tmp_path):
    """Real git repo: read_commit returns subject + diff."""
    repo = tmp_path / "r"
    repo.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True,
            text=True, check=True,
        )

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "a.py").write_text("x = 1\n")
    run("add", "a.py")
    run("commit", "-q", "-m", "fix: test")
    sha = run("rev-parse", "HEAD").stdout.strip()
    info = cli.read_commit(sha, repo_path=repo)
    assert info.sha == sha
    assert info.subject == "fix: test"
    assert "a.py" in info.diff


def test_true_end_to_end_dry_run_happy_path(tmp_path, monkeypatch, capsys):
    """True end-to-end: real git repo, real read_commit, real divergence,
    real scan_diff, real build_pr_body, real format_version_string. Mocks
    ONLY the external surfaces: LLM call, upstream HEAD lookup, detect-secrets
    presence, and gh availability. No per-step cli.* function patching.

    This is the happy-path orchestrator walk that was missing from the
    per-step coverage — if anything in the real call graph breaks (argument
    threading, return-type contract, print layout), this test catches it.
    """
    import json as _json
    from unittest.mock import AsyncMock

    from genesis.contribution import pr_opener, sanitize, version_gate

    # Real throwaway repo with a single fix commit.
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "parser.py").write_text("def tokenize(t):\n    return t.split()\n")
    git("add", "parser.py")
    git("commit", "-m", "fix(parser): handle empty input")
    sha = git("rev-parse", "HEAD").stdout.strip()

    # Mark the "install version" so format_version_string() has something
    # stable to print.
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "genesis"\nversion = "3.0.0a1"\n'
    )
    (repo / ".genesis-source-commit").write_text(sha + "\n")

    # Isolate genesis home so identity doesn't touch the real install file.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis_home"))

    # Make scan_diff believe detect-secrets is installed (without running it).
    # NB: `sanitize.shutil` and `pr_opener.shutil` are the same module object,
    # so we can't patch which() globally to "only detect-secrets" — gh would
    # disappear. Route both modules' which() through a per-name predicate.
    def fake_which(name):
        if name == "detect-secrets":
            return "/fake/detect-secrets"
        if name == "gh":
            return "/fake/gh"
        return None

    monkeypatch.setattr(sanitize.shutil, "which", fake_which)
    monkeypatch.setattr(pr_opener.shutil, "which", fake_which)
    original_run = sanitize.subprocess.run

    def sanitize_fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "detect-secrets":
            import subprocess as _sp
            return _sp.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return original_run(cmd, *a, **k)

    monkeypatch.setattr(sanitize.subprocess, "run", sanitize_fake_run)

    # Mock the version-gate LLM path so no real network call is made.
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-selection")
    monkeypatch.setattr(
        version_gate, "_call_llm",
        AsyncMock(return_value=_json.dumps({
            "already_fixed": False, "confidence": 10,
            "matched_commit_sha": None, "reasoning": "not upstream",
        })),
    )
    # fetch_upstream_head in cli.py will try ls-remote origin — force
    # it to short-circuit by passing --upstream-sha explicitly.
    upstream_sha = sha  # version-match path short-circuits cleanly

    args = argparse.Namespace(
        sha=sha,
        identify=False,
        list=False,
        repo=str(repo),
        upstream_sha=upstream_sha,
        target_repo=None,
        yes=True,
        dry_run=True,           # don't actually fork + push + gh pr create
        skip_review=True,       # don't shell out to codex
        allow_non_fix=False,
        func=cli.run,
    )

    rc = cli.run(args)
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert rc == 0, f"expected rc=0, got {rc}; stdout:\n{out}\nstderr:\n{err}"

    # Walk the 8 steps in order — if cli.py reordered a step, this catches it
    for step in [
        "step 1 — read commit",
        "step 2 — identity",
        "step 3 — divergence check",
        "step 4 — sanitizer",
        "step 5 — version gate",
        "step 6 — adversarial review",
        "step 7 — consent",
        "step 8 — open PR",
    ]:
        assert step in out, f"missing step in output: {step}"

    # Real build_pr_body output should mention the subject + branch name.
    assert "fix(parser): handle empty input" in out
    assert "community/" in out
    assert "dry-run" in out
    # Sanitizer ran at least detect-secrets + portability.
    assert "detect-secrets" in out
    assert "portability" in out


def test_add_parser_registers_subcommand():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli.add_parser(sub)
    args = parser.parse_args(["contribute", "abc123", "--yes"])
    assert args.command == "contribute"
    assert args.sha == "abc123"
    assert args.yes is True
