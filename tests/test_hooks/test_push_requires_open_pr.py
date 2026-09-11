"""Tests for the push-without-PR gate in git_push_guard.py.

The defect this closes: a branch could be pushed to the PUBLIC remote and left
without a PR indefinitely. `ci.yml` fires on `push: [main]` and
`pull_request: [main]`, so such a branch matches NEITHER trigger and receives no
CI at all — which means the leak scan never runs on it. MEASURED on this install:
10 public branches were in that state at once, and `gh run list` for one of them
returned empty, confirming it had never been scanned.

WHY THE GATE SITS ON THE RE-PUSH ARM, which is the load-bearing design decision:
blocking a FIRST push for "no PR" would deadlock publishing outright. A PR cannot
be opened for a branch that is not on the remote yet, and `gh pr create` does not
push for you (MEASURED, gh 2.98.0: non-interactively it aborts with "you must
first push the current branch" because its implicit push needs a prompt). So the
first push keeps its approval dialog, and every push AFTER it requires the PR to
exist — bounding a branch's PR-less window to a single push instead of forever.

The deadlock case is tested explicitly below; without it, a green suite would say
nothing about whether anyone can still publish.

Remote plumbing: the published fixture uses the guard's own PUSH ALLOWLIST rather
than a real remote. The obvious offline trick — `url.<local>.insteadOf` pointing a
github-looking url at a local bare repo — does not work here, and the reason is
worth recording: `git remote get-url --push` APPLIES insteadOf rewriting, so the
guard would see the local path and correctly decide the destination is not public.
The allowlist is the guard's own "already on the remote" path, so the re-push arm
is exercised for real, offline and deterministic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"

_PUBLIC_URL = "https://github.com/testowner/testrepo.git"
_PUBLIC_SLUG = "testowner/testrepo"

_spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
gpg = importlib.util.module_from_spec(_spec)
sys.modules["git_push_guard"] = gpg
_spec.loader.exec_module(gpg)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def published(tmp_path: Path, monkeypatch):
    """A repo whose branch the guard already considers published.

    Reached via the PUSH ALLOWLIST rather than a real remote, deliberately. The
    guard classifies a destination by `git remote get-url --push`, and that call
    applies `insteadOf` rewriting — so redirecting a github-looking url to a local
    bare repo (the obvious way to fake this offline) makes the guard see the LOCAL
    path and correctly conclude the destination is not public. The allowlist is the
    guard's own offline "already on the remote" path, so using it exercises the
    real re-push arm with no network and no misdirection.
    """
    genesis_home = tmp_path / "genesis-home"
    genesis_home.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(genesis_home))

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "s@s")
    _git(repo, "config", "user.name", "s")
    _git(repo, "remote", "add", "origin", _PUBLIC_URL)
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "checkout", "-q", "-b", "feat/published")

    sys.path.insert(0, str(_GUARD.parent))
    import push_allowlist

    os.environ["GENESIS_HOME"] = str(genesis_home)
    push_allowlist.record({_PUBLIC_URL}, "feat/published")
    assert push_allowlist.is_recorded({_PUBLIC_URL}, "feat/published"), (
        "fixture precondition: the branch must read as already-published"
    )
    return repo


def _run(command: str, cwd: Path, *, open_pr: str | None, public: str = _PUBLIC_SLUG):
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "CLAUDE_TOOL_INPUT",
            "GENESIS_CC_SESSION",
            "_TEST_GH_OPEN_PR",
            "_TEST_CANONICAL_PUBLIC_REPO",
        )
    }
    env["_TEST_CANONICAL_PUBLIC_REPO"] = public
    if os.environ.get("GENESIS_HOME"):
        env["GENESIS_HOME"] = os.environ["GENESIS_HOME"]
    if open_pr is not None:
        env["_TEST_GH_OPEN_PR"] = open_pr
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "cwd": str(cwd)},
        }
    )
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=payload,
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


# ─── the gate ────────────────────────────────────────────────────────────────


def test_repush_to_a_pr_less_public_branch_is_blocked(published):
    """The defect itself: a published branch with no PR must not take more work."""
    r = _run("git push", published, open_pr="0")
    assert r.returncode == 2, f"expected a block, got {r.returncode}: {r.stderr}"
    assert "no open PR" in r.stderr
    assert "gh pr create" in r.stderr, "a block must name its remedy"


def test_repush_is_allowed_once_the_pr_exists(published):
    """The PR-fix loop — push, review, push again — must stay frictionless."""
    r = _run("git push", published, open_pr="1")
    assert r.returncode == 0, f"expected allow, got {r.returncode}: {r.stderr}"
    assert "no open PR" not in r.stderr


def test_a_first_push_is_never_blocked_by_this_gate(tmp_path):
    """THE DEADLOCK CASE. A branch not yet on the remote cannot have a PR.

    If this gate fired here, publishing would be impossible: you cannot open a PR
    for an unpushed branch, and `gh pr create` will not push it for you. The
    first push must still reach the approval dialog (exit 0 with an `ask`), never
    a block.
    """
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "s@s")
    _git(repo, "config", "user.name", "s")
    _git(repo, "config", f"url.{bare.as_uri()}.insteadOf", _PUBLIC_URL)
    _git(repo, "remote", "add", "origin", _PUBLIC_URL)
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "checkout", "-q", "-b", "feat/never-pushed")

    r = _run("git push", repo, open_pr="0")
    assert r.returncode != 2, (
        f"a first push must not be blocked — publishing would deadlock. stderr: {r.stderr}"
    )


def test_an_undeterminable_pr_state_fails_open(published):
    """gh unreachable must not halt every push on the box.

    A miss costs one branch briefly lacking a PR; a false block stops all work.
    The empty seam value forces the undeterminable branch.
    """
    r = _run("git push", published, open_pr="")
    assert r.returncode != 2, f"must fail OPEN when the PR state is unknown: {r.stderr}"


def test_a_private_remote_is_untouched(published):
    """The gate is scoped to the public repo; the DR remote must be unaffected."""
    r = _run("git push", published, open_pr="0", public="someone/else")
    assert r.returncode != 2, f"a non-public destination must not be gated: {r.stderr}"


def test_an_undeterminable_public_repo_does_not_block(published):
    """If we cannot identify the destination, we do not refuse to publish to it."""
    r = _run("git push", published, open_pr="0", public="")
    assert r.returncode != 2


def test_the_override_releases_a_deliberate_pr_less_branch(published):
    """The block must not be a trap for a branch deliberately kept PR-less."""
    r = _run("git push  # no-pr-ack", published, open_pr="0")
    assert r.returncode != 2, f"the ack must release the block: {r.stderr}"


def test_gh_pr_create_is_never_caught_by_this_gate(published):
    """Catching the remedy would deadlock recovery from the block itself.

    Structural rather than special-cased: the gate only inspects `git push`
    segments, so a create cannot reach it. Asserted because the failure mode —
    blocking the one command the block tells you to run — is unrecoverable.
    """
    r = _run("gh pr create --title x --body y", published, open_pr="0")
    assert "no open PR" not in r.stderr


# ─── the helpers, where the classification actually happens ──────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/o/r.git", "o/r"),
        ("https://github.com/o/r", "o/r"),
        ("git@github.com:o/r.git", "o/r"),  # scp-like: no "://", invisible to a URL parser
        ("ssh://git@github.com/o/r.git", "o/r"),
        ("git@gitlab.com:o/r.git", None),  # not github
        ("https://github.com.evil/o/r", None),  # host-prefix confusion
        ("/plain/local/path", None),
        ("", None),
    ],
)
def test_remote_url_slug(url, expected):
    assert gpg._remote_url_slug(url) == expected


def test_urls_target_public_repo_needs_a_real_match(monkeypatch):
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", _PUBLIC_SLUG)
    assert gpg._urls_target_public_repo({_PUBLIC_URL}) is True
    assert gpg._urls_target_public_repo({f"git@github.com:{_PUBLIC_SLUG}.git"}) is True
    assert gpg._urls_target_public_repo({"https://github.com/other/repo.git"}) is False
    assert gpg._urls_target_public_repo(set()) is False


def test_urls_target_public_repo_is_false_when_public_is_unknown(monkeypatch):
    """Unknown destination → do not gate. Never refuse on a condition never established."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "")
    assert gpg._urls_target_public_repo({_PUBLIC_URL}) is False


@pytest.mark.parametrize(
    ("seam", "expected"),
    [("1", True), ("true", True), ("0", False), ("no", False), ("", None)],
)
def test_branch_has_open_pr_seam_is_three_valued(monkeypatch, seam, expected):
    """False and None must stay distinct — the gate blocks on one and not the other."""
    monkeypatch.setenv("_TEST_GH_OPEN_PR", seam)
    assert gpg._branch_has_open_pr("any-branch") is expected
