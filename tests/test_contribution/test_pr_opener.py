"""Unit + integration tests for genesis.contribution.pr_opener.

The non-dry-run tests mock `gh` subprocess calls but run real `git`
against a throwaway repo topology so the worktree + cherry-pick + push
flow is exercised end-to-end against a local bare repo acting as the
fake fork.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from genesis.contribution import pr_opener
from genesis.contribution.findings import (
    Finding,
    FindingKind,
    InstallInfo,
    ReviewResult,
    SanitizerResult,
    Severity,
    VersionGateResult,
)

# ---------- fixtures ----------


@pytest.fixture
def install():
    return InstallInfo(
        install_id="12345678-abcd-abcd-abcd-1234567890ab",
        created_at="2026-04-11T00:00:00+00:00",
    )


@pytest.fixture
def clean_sanitizer():
    return SanitizerResult(ok=True, scanners_run=["detect-secrets", "portability"])


@pytest.fixture
def version_match_result():
    return VersionGateResult(
        already_fixed=False, confidence=0, version_match=True,
    )


@pytest.fixture
def review_pass():
    return ReviewResult(
        available=True, reviewer="codex", passed=True,
        finding_count=0, summary="no issues",
    )


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def throwaway_repo_topology(tmp_path: Path):
    """A realistic git topology for integration tests.

    Layout:
        tmp_path/
          upstream.git/           — bare repo acting as "upstream public"
          fork.git/               — bare repo acting as contributor's fork
          contrib/                — contributor's clone (with the fix)

    Returns a dict with paths + SHAs needed by the tests.
    """
    upstream = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    contrib = tmp_path / "contrib"

    _git("init", "--bare", "-b", "main", str(upstream), cwd=tmp_path)
    _git("init", "--bare", "-b", "main", str(fork), cwd=tmp_path)

    # Seed the upstream with an initial commit via a helper clone.
    seed = tmp_path / "seed"
    _git("clone", str(upstream), str(seed), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=seed)
    _git("config", "user.name", "T", cwd=seed)
    (seed / "README.md").write_text("upstream\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "chore: seed", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    upstream_sha = _git(
        "rev-parse", "HEAD", cwd=seed,
    ).stdout.strip()

    # Contributor clones upstream, then makes a fix commit ON TOP of it.
    _git("clone", str(upstream), str(contrib), cwd=tmp_path)
    _git("config", "user.email", "c@example.com", cwd=contrib)
    _git("config", "user.name", "C", cwd=contrib)
    (contrib / "parser.py").write_text("def tokenize(t):\n    return t.split()\n")
    _git("add", "parser.py", cwd=contrib)
    _git("commit", "-m", "fix(parser): handle empty input", cwd=contrib)
    source_sha = _git("rev-parse", "HEAD", cwd=contrib).stdout.strip()

    return {
        "upstream_bare": upstream,
        "fork_bare": fork,
        "contrib": contrib,
        "upstream_sha": upstream_sha,
        "source_sha": source_sha,
    }


# ---------- build_pr_body / branch_name ----------


def test_build_pr_body_has_mandatory_fields(install, clean_sanitizer, version_match_result, review_pass):
    body = pr_opener.build_pr_body(
        install=install,
        source_sha="abc123def456",
        subject="fix(parser): handle empty input",
        version_display="3.0.0a1@abc123d",
        version_gate=version_match_result,
        sanitizer=clean_sanitizer,
        review=review_pass,
    )
    assert "fix(parser): handle empty input" in body
    assert "3.0.0a1@abc123d" in body
    assert "abc123def456" in body
    assert "12345678" in body  # install id prefix
    assert "matches upstream HEAD" in body
    assert "codex=pass" in body
    assert "detect-secrets" in body


def test_build_pr_body_version_behind(install, clean_sanitizer, review_pass):
    vg = VersionGateResult(
        already_fixed=False,
        confidence=0,
        version_match=False,
        upstream_commit_count=5,
    )
    body = pr_opener.build_pr_body(
        install=install,
        source_sha="abc123",
        subject="fix: x",
        version_display="3.0.0a1@abc123d",
        version_gate=vg,
        sanitizer=clean_sanitizer,
        review=review_pass,
    )
    assert "5 commits behind" in body


def test_build_pr_body_review_unavailable(install, clean_sanitizer, version_match_result):
    review = ReviewResult(available=False)
    body = pr_opener.build_pr_body(
        install=install,
        source_sha="abc123",
        subject="fix: x",
        version_display="3.0.0a1@abc123d",
        version_gate=version_match_result,
        sanitizer=clean_sanitizer,
        review=review,
    )
    assert "unavailable" in body
    assert "Review:" in body


def test_branch_name_format(install):
    name = pr_opener.branch_name(install, "abcdef1234567890")
    assert name == "community/12345678-abcdef1"


# ---------- create_pr: early returns ----------


def test_create_pr_refuses_unclean_sanitizer(install, version_match_result, review_pass):
    dirty = SanitizerResult(
        ok=False,
        findings=[Finding(
            kind=FindingKind.SECRET, severity=Severity.BLOCK, message="k",
        )],
    )
    r = pr_opener.create_pr(
        install=install,
        source_sha="abcdef1234",
        subject="fix",
        version_display="3.0.0a1@abc",
        version_gate=version_match_result,
        sanitizer=dirty,
        review=review_pass,
    )
    assert r.ok is False
    assert "sanitizer" in r.error.lower()


def test_create_pr_missing_gh_binary(install, clean_sanitizer, version_match_result, review_pass):
    with patch("genesis.contribution.pr_opener.shutil.which", return_value=None):
        r = pr_opener.create_pr(
            install=install,
            source_sha="abcdef1234",
            subject="fix",
            version_display="3.0.0a1@abc",
            version_gate=version_match_result,
            sanitizer=clean_sanitizer,
            review=review_pass,
        )
    assert r.ok is False
    assert "gh" in r.error.lower()


def test_create_pr_dry_run(install, clean_sanitizer, version_match_result, review_pass):
    with patch("genesis.contribution.pr_opener.shutil.which", return_value="/fake/gh"):
        r = pr_opener.create_pr(
            install=install,
            source_sha="abcdef1",
            subject="fix(x): y",
            version_display="3.0.0a1@abc",
            version_gate=version_match_result,
            sanitizer=clean_sanitizer,
            review=review_pass,
            dry_run=True,
        )
    assert r.ok is True
    assert r.branch.startswith("community/12345678-")
    assert "fix(x): y" in r.body


def test_create_pr_rejects_missing_upstream_sha(install, clean_sanitizer, version_match_result, review_pass):
    with patch("genesis.contribution.pr_opener.shutil.which", return_value="/fake/gh"):
        r = pr_opener.create_pr(
            install=install,
            source_sha="abcdef1234",
            subject="fix",
            version_display="3.0.0a1@abc",
            version_gate=version_match_result,
            sanitizer=clean_sanitizer,
            review=review_pass,
            upstream_sha=None,
            dry_run=False,
        )
    assert r.ok is False
    assert "upstream_sha" in r.error.lower()


def test_create_pr_rejects_non_hex_upstream_sha(install, clean_sanitizer, version_match_result, review_pass):
    with patch("genesis.contribution.pr_opener.shutil.which", return_value="/fake/gh"):
        r = pr_opener.create_pr(
            install=install,
            source_sha="abcdef1234",
            subject="fix",
            version_display="3.0.0a1@abc",
            version_gate=version_match_result,
            sanitizer=clean_sanitizer,
            review=review_pass,
            upstream_sha="--output=/tmp/x",
            dry_run=False,
        )
    assert r.ok is False
    assert "upstream_sha" in r.error.lower()


# ---------- helper units ----------


def test_repo_name_from_target_ok():
    assert pr_opener._repo_name_from_target("WingedGuardian/GENesis-AGI") == "GENesis-AGI"


def test_repo_name_from_target_invalid():
    with pytest.raises(ValueError, match="owner/repo"):
        pr_opener._repo_name_from_target("not-a-slug")


def test_fork_url():
    assert (
        pr_opener._fork_url("alice", "WingedGuardian/GENesis-AGI")
        == "https://github.com/alice/GENesis-AGI.git"
    )


def test_ensure_fork_short_circuits_when_fork_exists(monkeypatch):
    monkeypatch.setattr(pr_opener, "_fork_exists", lambda login, repo, target_repo=None: True)
    called = []
    monkeypatch.setattr(
        pr_opener.subprocess, "run",
        lambda *a, **k: called.append(a) or pytest.fail("should not POST"),
    )
    ok, msg = pr_opener._ensure_fork("owner/repo", "alice")
    assert ok is True
    assert msg == ""


def test_ensure_fork_posts_and_polls(monkeypatch):
    calls = {"exists": 0}

    def fake_exists(login, repo, target_repo=None):
        # The pre-flight name-collision check (target_repo=None) must
        # report False, otherwise we'd get a "name collision" error
        # before the POST runs.
        if target_repo is None:
            return False
        calls["exists"] += 1
        return calls["exists"] > 2  # exists on the 3rd check

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(pr_opener, "_fork_exists", fake_exists)
    monkeypatch.setattr(pr_opener.subprocess, "run", lambda *a, **k: FakeProc())
    monkeypatch.setattr(pr_opener.time, "sleep", lambda s: None)  # no real wait

    ok, msg = pr_opener._ensure_fork("owner/repo", "alice")
    assert ok is True
    assert calls["exists"] >= 3


def test_ensure_fork_surfaces_api_error(monkeypatch):
    monkeypatch.setattr(pr_opener, "_fork_exists", lambda login, repo, target_repo=None: False)

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "rate limited"

    monkeypatch.setattr(pr_opener.subprocess, "run", lambda *a, **k: FakeProc())
    ok, msg = pr_opener._ensure_fork("owner/repo", "alice")
    assert ok is False
    assert "rate limited" in msg


def test_ensure_fork_refuses_name_collision_with_non_fork(monkeypatch):
    """Codex I1 regression: a personal repo with the same name as the
    target's repo, but NOT a fork of it, must NOT be treated as a fork.
    Otherwise pushing into it and opening a cross-account PR fails opaquely.
    """
    def fake_exists(login, repo, target_repo=None):
        # Strict check: not a fork of target. Loose check: yes, exists.
        return target_repo is None

    monkeypatch.setattr(pr_opener, "_fork_exists", fake_exists)
    ok, msg = pr_opener._ensure_fork("WingedGuardian/GENesis-AGI", "alice")
    assert ok is False
    assert "not a fork" in msg.lower() or "is not" in msg.lower()


def test_ensure_fork_rejects_bad_target():
    ok, msg = pr_opener._ensure_fork("not-a-slug", "alice")
    assert ok is False
    assert "owner/repo" in msg


def test_ensure_sha_reachable_present(throwaway_repo_topology):
    """A SHA already in local objects returns ok=True immediately."""
    topo = throwaway_repo_topology
    ok, msg = pr_opener._ensure_sha_reachable(topo["contrib"], topo["source_sha"])
    assert ok is True
    assert msg == ""


def test_ensure_sha_reachable_missing_no_fetch(tmp_path):
    """Bogus SHA + no remote → fetch fails and we surface a clear error."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / "a").write_text("x\n")
    _git("add", "a", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    bogus = "0123456789abcdef0123456789abcdef01234567"
    ok, msg = pr_opener._ensure_sha_reachable(repo, bogus)
    assert ok is False
    assert "unreachable" in msg.lower() or "not reachable" in msg.lower() or "not in local" in msg.lower()


# ---------- integration: real git worktree + cherry-pick + push ----------


def test_prepare_and_push_branch_happy(throwaway_repo_topology, install):
    topo = throwaway_repo_topology
    branch = pr_opener.branch_name(install, topo["source_sha"])
    ok, msg = pr_opener._prepare_and_push_branch(
        repo_path=topo["contrib"],
        upstream_sha=topo["upstream_sha"],
        source_sha=topo["source_sha"],
        branch=branch,
        fork_url=str(topo["fork_bare"]),  # local bare repo as fake fork
    )
    assert ok is True, f"_prepare_and_push_branch failed: {msg}"

    # Verify the branch actually landed in the fake fork with the fix on top.
    ls = subprocess.run(
        ["git", "ls-remote", str(topo["fork_bare"]), f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True,
    )
    assert branch in ls.stdout, f"branch not pushed: {ls.stdout!r}"

    # And the contributor's working repo is untouched — still on main
    head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=topo["contrib"]).stdout.strip()
    assert head == "main"

    # The temp worktree should have been removed (no lingering worktree).
    wt_list = _git("worktree", "list", cwd=topo["contrib"]).stdout
    assert "genesis-contrib-" not in wt_list


def test_prepare_and_push_branch_rejects_non_sha():
    ok, msg = pr_opener._prepare_and_push_branch(
        repo_path=Path("/tmp"),
        upstream_sha="--evil",
        source_sha="abcdef1",
        branch="community/x",
        fork_url="/dev/null",
    )
    assert ok is False
    assert "upstream_sha" in msg


def test_prepare_and_push_branch_idempotent_rerun(throwaway_repo_topology, install):
    """Codex I2 regression: a re-run for the same fix uses --force-with-lease
    to overwrite the contributor's own stale fork branch instead of failing
    with non-fast-forward. Branch name is deterministic across runs."""
    topo = throwaway_repo_topology
    branch = pr_opener.branch_name(install, topo["source_sha"])
    # First run
    ok, msg = pr_opener._prepare_and_push_branch(
        repo_path=topo["contrib"],
        upstream_sha=topo["upstream_sha"],
        source_sha=topo["source_sha"],
        branch=branch,
        fork_url=str(topo["fork_bare"]),
    )
    assert ok is True, f"first run failed: {msg}"
    # Second run with the same inputs — must succeed (force-with-lease).
    ok, msg = pr_opener._prepare_and_push_branch(
        repo_path=topo["contrib"],
        upstream_sha=topo["upstream_sha"],
        source_sha=topo["source_sha"],
        branch=branch,
        fork_url=str(topo["fork_bare"]),
    )
    assert ok is True, f"re-run failed: {msg}"
    # Worktree pruned afterwards.
    wt_list = _git("worktree", "list", cwd=topo["contrib"]).stdout
    assert "genesis-contrib-" not in wt_list


def test_prepare_and_push_branch_prunes_stale_worktrees(throwaway_repo_topology, install):
    """Codex C1 mitigation: stale `genesis-contrib-*` ghost worktree
    entries from prior crashed runs get pruned at the start so they
    don't accumulate in the contributor's main repo."""
    topo = throwaway_repo_topology
    contrib = topo["contrib"]

    # Manufacture a ghost: add a worktree, then rmtree the dir behind
    # git's back so the entry is left dangling.
    ghost_dir = Path(__file__).parent / "_ghost_should_not_exist"
    if ghost_dir.exists():
        import shutil as _sh
        _sh.rmtree(str(ghost_dir))
    import tempfile as _tf
    ghost = Path(_tf.mkdtemp(prefix="genesis-contrib-ghost-"))
    _git("worktree", "add", "--detach", str(ghost), topo["upstream_sha"], cwd=contrib)
    import shutil as _sh
    _sh.rmtree(str(ghost))
    # Confirm the ghost is registered before we run the fix.
    pre = _git("worktree", "list", cwd=contrib).stdout
    assert "genesis-contrib-ghost-" in pre

    # Real run — the prune at the top of _prepare_and_push_branch should
    # scrub the ghost entry as a side effect.
    branch = pr_opener.branch_name(install, topo["source_sha"])
    ok, msg = pr_opener._prepare_and_push_branch(
        repo_path=contrib,
        upstream_sha=topo["upstream_sha"],
        source_sha=topo["source_sha"],
        branch=branch,
        fork_url=str(topo["fork_bare"]),
    )
    assert ok is True, f"failed: {msg}"
    post = _git("worktree", "list", cwd=contrib).stdout
    assert "genesis-contrib-ghost-" not in post, (
        f"ghost worktree not pruned: {post}"
    )


def test_prepare_and_push_branch_cherry_pick_conflict(tmp_path, install):
    """A conflicting cherry-pick must clean up and surface a clear error."""
    # Build a topology where the contrib's fix touches a file that
    # upstream has evolved past, causing a merge conflict.
    repo = tmp_path / "repo"
    bare = tmp_path / "bare.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("clone", str(bare), str(repo), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)

    # Common base
    (repo / "x.py").write_text("base\n")
    _git("add", "x.py", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    _git("push", "origin", "main", cwd=repo)
    base_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    # Upstream evolves
    (repo / "x.py").write_text("upstream rewrite\n")
    _git("add", "x.py", cwd=repo)
    _git("commit", "-m", "chore: rewrite", cwd=repo)
    _git("push", "origin", "main", cwd=repo)
    upstream_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    # Contributor rewinds to base and makes a conflicting fix
    _git("checkout", base_sha, cwd=repo)
    (repo / "x.py").write_text("contrib fix\n")
    _git("add", "x.py", cwd=repo)
    _git("commit", "-m", "fix: conflict", cwd=repo)
    source_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    branch = pr_opener.branch_name(install, source_sha)
    ok, msg = pr_opener._prepare_and_push_branch(
        repo_path=repo,
        upstream_sha=upstream_sha,
        source_sha=source_sha,
        branch=branch,
        fork_url=str(bare),
    )
    assert ok is False
    assert "cherry-pick" in msg.lower()
    # Cleanup happened
    wt_list = _git("worktree", "list", cwd=repo).stdout
    assert "genesis-contrib-" not in wt_list


def test_create_pr_full_happy_path_with_mocked_gh(
    throwaway_repo_topology, install, clean_sanitizer, version_match_result,
    review_pass, monkeypatch,
):
    """End-to-end: real git worktree + cherry-pick + push, mocked gh pr create."""
    topo = throwaway_repo_topology

    monkeypatch.setattr(pr_opener, "_check_gh_available", lambda: "/usr/bin/gh")
    monkeypatch.setattr(pr_opener, "_check_gh_auth", lambda: (True, "ok"))
    monkeypatch.setattr(pr_opener, "_gh_current_user", lambda: "alice")
    monkeypatch.setattr(pr_opener, "resolve_target_repo", lambda rp: "WingedGuardian/GENesis-AGI")
    monkeypatch.setattr(pr_opener, "_ensure_fork", lambda t, user: (True, ""))
    monkeypatch.setattr(pr_opener, "_fork_url", lambda user, target: str(topo["fork_bare"]))

    # Intercept ONLY the final `gh pr create` subprocess call so the real
    # git commands still run through the normal subprocess.run.
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "create"]:
            class FakeProc:
                returncode = 0
                stdout = "https://github.com/WingedGuardian/GENesis-AGI/pull/42\n"
                stderr = ""
            return FakeProc()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(pr_opener.subprocess, "run", fake_run)

    r = pr_opener.create_pr(
        install=install,
        source_sha=topo["source_sha"],
        subject="fix(parser): handle empty input",
        version_display="3.0.0a1@abc1234",
        version_gate=version_match_result,
        sanitizer=clean_sanitizer,
        review=review_pass,
        upstream_sha=topo["upstream_sha"],
        target_repo="WingedGuardian/GENesis-AGI",
        repo_path=topo["contrib"],
        dry_run=False,
    )
    assert r.ok is True, f"create_pr failed: {r.error}"
    assert r.url == "https://github.com/WingedGuardian/GENesis-AGI/pull/42"
    assert r.branch.startswith("community/12345678-")

    # Branch actually made it to the fake fork.
    ls = subprocess.run(
        ["git", "ls-remote", str(topo["fork_bare"]), f"refs/heads/{r.branch}"],
        capture_output=True, text=True, check=True,
    )
    assert r.branch in ls.stdout

    # Contributor's repo untouched.
    assert _git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=topo["contrib"],
    ).stdout.strip() == "main"


def test_create_pr_target_repo_unresolvable(
    install, clean_sanitizer, version_match_result, review_pass, monkeypatch,
):
    monkeypatch.setattr(pr_opener, "_check_gh_available", lambda: "/usr/bin/gh")
    monkeypatch.setattr(pr_opener, "resolve_target_repo", lambda rp: None)
    r = pr_opener.create_pr(
        install=install,
        source_sha="abcdef1234",
        subject="fix",
        version_display="v",
        version_gate=version_match_result,
        sanitizer=clean_sanitizer,
        review=review_pass,
        upstream_sha="abcdef1234",
        target_repo=None,
        dry_run=False,
    )
    assert r.ok is False
    assert "target repo" in r.error.lower()


def test_create_pr_gh_pr_create_failure(
    throwaway_repo_topology, install, clean_sanitizer, version_match_result,
    review_pass, monkeypatch,
):
    topo = throwaway_repo_topology
    monkeypatch.setattr(pr_opener, "_check_gh_available", lambda: "/usr/bin/gh")
    monkeypatch.setattr(pr_opener, "_check_gh_auth", lambda: (True, "ok"))
    monkeypatch.setattr(pr_opener, "_gh_current_user", lambda: "alice")
    monkeypatch.setattr(pr_opener, "resolve_target_repo", lambda rp: "W/R")
    monkeypatch.setattr(pr_opener, "_ensure_fork", lambda t, user: (True, ""))
    monkeypatch.setattr(pr_opener, "_fork_url", lambda user, target: str(topo["fork_bare"]))

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "create"]:
            class FakeProc:
                returncode = 1
                stdout = ""
                stderr = "label community-submission does not exist"
            return FakeProc()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(pr_opener.subprocess, "run", fake_run)

    r = pr_opener.create_pr(
        install=install,
        source_sha=topo["source_sha"],
        subject="fix: x",
        version_display="v",
        version_gate=version_match_result,
        sanitizer=clean_sanitizer,
        review=review_pass,
        upstream_sha=topo["upstream_sha"],
        target_repo="W/R",
        repo_path=topo["contrib"],
        dry_run=False,
    )
    assert r.ok is False
    assert "label" in r.error
