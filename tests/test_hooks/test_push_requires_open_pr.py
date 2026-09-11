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
    """A branch that is GENUINELY on a remote, offline.

    Two mechanisms, and both are needed for different reasons:

    * `url.<local>.insteadOf` redirects the transport to a local bare repo, so
      the branch really is pushed and `git ls-remote` really finds it. That
      matters because the gate confirms presence LIVE before blocking — an
      allowlist record alone is not enough, and deliberately so.
    * `_TEST_FORCE_PUBLIC_REMOTE` satisfies the public-scoping conjunct, because
      `git remote get-url --push` EXPANDS the insteadOf rewrite and would
      otherwise report the local path (correctly concluding "not public") and
      skip the gate by the wrong conjunct.

    Using only the first is how a test passes while proving nothing.
    """
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis-home"))
    (tmp_path / "genesis-home").mkdir()

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "s@s")
    _git(repo, "config", "user.name", "s")
    _git(repo, "config", f"url.{bare.as_uri()}.insteadOf", _PUBLIC_URL)
    _git(repo, "remote", "add", "origin", _PUBLIC_URL)
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "checkout", "-q", "-b", "feat/published")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "work")
    _git(repo, "push", "-q", "origin", "feat/published")

    assert _git(repo, "ls-remote", "origin", "feat/published").strip(), (
        "fixture precondition: the branch must really be on the remote"
    )
    return repo


@pytest.fixture
def recorded_but_absent(tmp_path: Path, monkeypatch):
    """The allowlist says "already pushed"; the remote has never seen it.

    push_allowlist's documented 90-day TRUST WINDOW honors a record even after
    the remote branch is deleted. This fixture is that state.
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
    return repo


def _run(
    command: str,
    cwd: Path,
    *,
    open_pr: str | None,
    public: str = _PUBLIC_SLUG,
    force_public: bool = False,
):
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "CLAUDE_TOOL_INPUT",
            "GENESIS_CC_SESSION",
            "_TEST_GH_OPEN_PR",
            "_TEST_CANONICAL_PUBLIC_REPO",
            "_TEST_FORCE_PUBLIC_REMOTE",
        )
    }
    env["_TEST_CANONICAL_PUBLIC_REPO"] = public
    if force_public:
        env["_TEST_FORCE_PUBLIC_REMOTE"] = "1"
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
    r = _run("git push", published, force_public=True, open_pr="0")
    assert r.returncode == 2, f"expected a block, got {r.returncode}: {r.stderr}"
    assert "no open PR" in r.stderr
    assert "gh pr create" in r.stderr, "a block must name its remedy"


def test_repush_is_allowed_once_the_pr_exists(published):
    """The PR-fix loop — push, review, push again — must stay frictionless."""
    r = _run("git push", published, force_public=True, open_pr="1")
    assert r.returncode == 0, f"expected allow, got {r.returncode}: {r.stderr}"
    assert "no open PR" not in r.stderr


def test_a_first_push_is_never_blocked_by_this_gate(tmp_path, monkeypatch):
    """THE DEADLOCK CASE. A branch not yet on the remote cannot have a PR.

    If this gate fired here, publishing would be impossible: you cannot open a PR
    for an unpushed branch, and `gh pr create` will not push it for you. The
    first push must still reach the approval dialog, never a block.

    `_TEST_FORCE_PUBLIC_REMOTE` is set deliberately. The first version of this
    test used `url.<local>.insteadOf` to fake a public remote — and
    `git remote get-url --push` EXPANDS that rewrite, so the guard saw a local
    path, the public-scoping conjunct returned False, and the gate was skipped
    before ever reaching the conjuncts this test claims to certify. It passed
    while testing nothing. Forcing the scoping conjunct TRUE means the block can
    only be withheld by the first-push protections, which is the actual claim.
    """
    monkeypatch.setenv("_TEST_FORCE_PUBLIC_REMOTE", "1")
    repo = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "s@s")
    _git(repo, "config", "user.name", "s")
    _git(repo, "remote", "add", "origin", _PUBLIC_URL)
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "checkout", "-q", "-b", "feat/never-pushed")

    r = _run("git push", repo, open_pr="0", force_public=True)
    assert r.returncode != 2, (
        f"a first push must not be blocked — publishing would deadlock. stderr: {r.stderr}"
    )


def test_a_stale_allowlist_entry_cannot_block_an_absent_branch(recorded_but_absent):
    """The allowlist says "already pushed"; the remote says otherwise. Trust the remote.

    `push_allowlist` documents a 90-day TRUST WINDOW: a recorded entry is honored
    even when the remote branch is GONE. That acceptance was written for an arm
    that merely SKIPPED A PROMPT, and it is not acceptable as the premise of a
    hard block — a branch name re-created inside that window would be refused
    with a remedy it cannot run, because `gh pr create` needs the branch to be on
    the remote.

    The `published` fixture records the branch in the allowlist but never pushes
    it anywhere, which is exactly that state.
    """
    r = _run("git push", recorded_but_absent, open_pr="0", force_public=True)
    assert r.returncode != 2, (
        "a branch absent from the remote must not be blocked — the printed remedy "
        f"would be unrunnable. stderr: {r.stderr}"
    )


def test_an_undeterminable_pr_state_fails_open(published):
    """gh unreachable must not halt every push on the box.

    A miss costs one branch briefly lacking a PR; a false block stops all work.
    The empty seam value forces the undeterminable branch.
    """
    r = _run("git push", published, force_public=True, open_pr="")
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
    r = _run("git push  # no-pr-ack", published, force_public=True, open_pr="0")
    assert r.returncode != 2, f"the ack must release the block: {r.stderr}"


def test_gh_pr_create_is_never_caught_by_this_gate(published):
    """Catching the remedy would deadlock recovery from the block itself.

    A LONE create cannot reach the gate structurally — it is a different segment
    population. The COMPOUND form is the one that needed a guard, and it is the
    one a reader of the block message would naturally type: the message says to
    run `gh pr create` and then push, and chaining them is the obvious reading.
    A PreToolUse block discards the WHOLE compound, so blocking it would throw
    away the fix along with the push, and the operator would never learn the
    create had not run either.
    """
    lone = _run("gh pr create --title x --body y", published, force_public=True, open_pr="0")
    assert "no open PR" not in lone.stderr

    for chained in (
        "gh pr create --fill && git push",
        "gh pr create --fill; git push",
    ):
        r = _run(chained, published, force_public=True, open_pr="0")
        assert r.returncode != 2, (
            f"the block prints this as the remedy; blocking it discards the fix too: "
            f"{chained} -> {r.stderr[:160]}"
        )


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


# ─── the bypasses an adversarial review found, now pinned ────────────────────


@pytest.mark.parametrize(
    ("command", "label"),
    [
        ("echo hello  # no-pr-ack\ngit push", "an unrelated earlier segment"),
        ("git status  # no-pr-ack && git push", "an unrelated segment in a chain"),
        ("git push && echo done  # no-pr-ack", "a LATER segment"),
        ("cat > /dev/null <<'EOF'\nnotes\n# no-pr-ack\nEOF\ngit push", "a HEREDOC BODY"),
    ],
)
def test_the_sigil_only_counts_on_the_push_segment(published, command, label):
    """A sigil anywhere else in the command must not waive the gate.

    The first version scanned the WHOLE command string, so every case here
    silently allowed the push. The heredoc one is not contrived: any command that
    writes a PR body or a review note MENTIONING the sigil and then pushes would
    have waived the gate without anyone intending it.
    """
    r = _run(command, published, force_public=True, open_pr="0")
    assert r.returncode == 2, f"{label} must not waive the gate: {r.stderr[:200]}"


def test_a_dry_run_is_not_blocked(published):
    """A dry run publishes nothing, so it cannot create the no-CI gap.

    It is also the command someone reaches for precisely when a push is being
    refused, which makes blocking it actively unhelpful.
    """
    for cmd in ("git push --dry-run", "git push -n"):
        r = _run(cmd, published, force_public=True, open_pr="0")
        assert r.returncode != 2, f"{cmd} must not be blocked: {r.stderr[:200]}"


def test_deleting_the_branch_is_never_blocked(published):
    """Deleting a PR-less branch is the correct remediation, not a violation."""
    for cmd in (
        "git push origin --delete feat/published",
        "git push origin :feat/published",
        "git push -d origin feat/published",
    ):
        r = _run(cmd, published, force_public=True, open_pr="0")
        assert r.returncode != 2, f"{cmd} must not be blocked: {r.stderr[:200]}"


@pytest.mark.parametrize(
    ("stdout", "rc", "expected"),
    [
        ('[{"number": 1}]', 0, True),
        ("[]", 0, False),
        ("", 0, None),  # rc=0 with NO output is silence, not "there is no PR"
        ("null", 0, None),  # outside gh's documented list contract
        ('{"number": 1}', 0, None),  # an object, not a list
        ("[]", 1, None),  # non-zero exit
        ("not json", 0, None),
    ],
)
def test_gh_output_that_is_not_an_answer_fails_open(monkeypatch, stdout, rc, expected):
    """Only a well-formed LIST is an answer; everything else must be None.

    `json.loads(stdout or "[]")` turned both an empty stdout and a literal
    `null` into False — a BLOCK derived from something gh never said.
    """
    monkeypatch.delenv("_TEST_GH_OPEN_PR", raising=False)

    class _R:
        returncode = rc

    _R.stdout = stdout
    monkeypatch.setattr(gpg.subprocess, "run", lambda *a, **k: _R())
    assert gpg._branch_has_open_pr("some-branch") is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("ssh://git@github.com:22/o/r.git", "o/r"),  # port
        ("https://github.com:443/o/r.git", "o/r"),
        ("https://GitHub.com/o/r.git", "o/r"),  # host case (RFC 3986)
        ("git@GITHUB.COM:o/r.git", "o/r"),  # genesis:verified-generic — a git SSH url, not a person
        ("https://github.com/O/R.git", "o/r"),  # owner/repo case
        ("github.com:o/r.git", "o/r"),  # scp-like with no user
        ("https://github.com/o/r.git/", "o/r"),  # trailing slash after .git
        ("https://github.com/o/r.git#frag", "o/r"),
        ("https://github.com/o/r.git?x=1", "o/r"),
        # genesis:verified-generic — a synthetic userinfo form, not a credential
        ("https://x-access-token:tok@github.com/o/r.git", "o/r"),
        ("/plain/local/path", None),
        ("../relative/path", None),
        ("https://gitlab.com/o/r.git", None),
        ("https://github.com.evil.io/o/r", None),
    ],
)
def test_remote_url_slug_handles_the_real_shape_population(url, expected):
    """Six of these silently misclassified under a hand-rolled split.

    Every failure was fail-OPEN — the gate simply never fired — which is why
    none of them would have surfaced as a complaint.
    """
    assert gpg._remote_url_slug(url) == expected
