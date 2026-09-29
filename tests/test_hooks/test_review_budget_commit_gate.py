"""Commit-side enforcement of the shared distinct-reviewed-head budget."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "review_enforcement_commit.py"
STATE = ROOT / "scripts" / "review_state.py"
CODEX = "chatgpt-codex-connector[bot]"
HEADS = tuple(f"{i:x}" * 40 for i in range(1, 7))

# The hook imports `review_deadline` and `review_scope` at MODULE scope, resolving
# them from its own `scripts/` when run as a subprocess (sys.path[0] is the script's
# directory). Loading it by spec instead gives it pytest's sys.path, where neither
# resolves — so without this insert the module raises ModuleNotFoundError AT
# COLLECTION. It collected anyway in a whole-directory run only because 32-33 sibling
# modules do the same insert at import time (AST-resolved count; a grep for the
# literal idiom undercounts, since most go through a variable) and most sort earlier.
# That meant the file could NOT be run TARGETED — `pytest tests/test_hooks/test_x.py`,
# the form this project tells every session to use — which is how a suite silently
# stops running. Explicit here, like its siblings, so importability does not depend
# on collection order.
#
# `scripts/` only, deliberately: the hook self-inserts its own `hooks/` dir at module
# scope (review_enforcement_commit.py:26), which runs during exec_module below, so a
# `hooks` insert here would be dead. MEASURED — with `scripts/` alone and the hooks
# dir asserted absent, the module imports and `review_deadline` resolves from THIS
# worktree. Keep it ROOT-relative: hardcoding an absolute main-checkout path makes
# replay_guard_corpus.py raise SystemExit over a cross-tree bare-name import, in a
# different test directory, with an error naming neither file.
sys.path.insert(0, str(ROOT / "scripts"))

import review_budget  # noqa: E402 — needs the insert above.

#: Derived, never spelled. The gate messages interpolate these, so hardcoding "four"
#: or "two" in an assertion would relocate into the tests exactly the prose-vs-constant
#: drift this change removes from the code.
_HEAD_LIMIT = review_budget.STANDING_REVIEWED_HEAD_LIMIT
_GATE_LIMIT = review_budget.GATE_DISCOVERY_ROUND_LIMIT

_spec = importlib.util.spec_from_file_location("commit_budget_guard", HOOK)
_guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_guard)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "-c", "init.defaultBranch=main", "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "f.py").write_text("value = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    _git(path, "checkout", "-qb", "feature/review-budget")
    (path / "f.py").write_text("value = 2\n")
    _git(path, "add", "-A")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".genesis").mkdir(parents=True)
    return path


def _jsonl(rows) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def _evidence(monkeypatch, reviewed: int, *, gate=False, head=HEADS[4]):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "99")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_REPO", "owner/repo")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", head)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", _jsonl({"sha": sha} for sha in HEADS))
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS",
        _jsonl(
            {"login": CODEX, "commit_id": sha, "state": "COMMENTED"} for sha in HEADS[:reviewed]
        ),
    )
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    filename = "scripts/review_budget.py" if gate else "src/example.py"
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_FILES",
        json.dumps({"filename": filename, "previous_filename": None}),
    )


def _mark(repo: Path, home: Path) -> None:
    evidence = home / "review.txt"
    evidence.write_text("adversarial review complete\n" + "specific evidence " * 30)
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        [
            sys.executable,
            str(STATE),
            "mark",
            "--agent-output",
            str(evidence),
            "--source",
            "internal",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run(command: str, repo: Path, home: Path, *, dispatched=False):
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": command},
        }
    )
    env = {**os.environ, "HOME": str(home)}
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    else:
        env.pop("GENESIS_CC_SESSION", None)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _decision(result) -> str:
    if result.returncode == 2:
        return "deny"
    if result.stdout.strip():
        payload = json.loads(result.stdout)
        if payload.get("hookSpecificOutput", {}).get("permissionDecision") == "ask":
            return "ask"
    return "allow"


def test_current_branch_status_resolves_cross_repository_pr_identity():
    raw = json.dumps(
        {
            "currentBranch": {
                "number": 42,
                "state": "OPEN",
                "url": "https://github.example/target/project/pull/42",
            }
        }
    )
    assert _guard._current_branch_pr_identity(raw) == ("target/project", 42)


def test_current_branch_status_distinguishes_no_pr_from_malformed_evidence():
    assert _guard._current_branch_pr_identity('{"createdBy": []}') is None
    malformed = _guard._current_branch_pr_identity(
        '{"currentBranch": {"number": 42, "state": "OPEN", "url": "bad"}}'
    )
    assert isinstance(malformed, dict)
    assert malformed["status"] == "unknown"


def test_cloud_lookup_reserves_two_seconds_for_post_lookup_checks(monkeypatch, repo):
    now = [100.0]
    outer = _guard.Deadline.after(9.5, monotonic=lambda: now[0])
    now[0] += 3.0  # Earlier mandatory local probes consume part of the hook budget.
    calls = []

    def consumes_timeout(*args, timeout, **kwargs):
        calls.append(timeout)
        now[0] += timeout
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "currentBranch": {
                        "number": 99,
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/pull/99",
                    }
                }
            ),
        )

    monkeypatch.setattr(_guard.subprocess, "run", consumes_timeout)
    result = _guard._branch_review_budget(str(repo), "feature/review-budget", deadline=outer)

    assert result["status"] == "unknown"
    assert calls == [4.5]
    assert outer.remaining() == 2.0


def test_ordinary_four_heads_asks_for_each_commit(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    first = _run('git commit -m "fix"', repo, home)
    second = _run('git commit -m "fix"', repo, home)
    assert _decision(first) == _decision(second) == "ask"
    assert f"standing authorization ended after {_HEAD_LIMIT}" in first.stdout


def test_legacy_final_sigil_does_not_replace_native_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run('git commit -m "fix"  # final-round-accept', repo, home)
    assert _decision(result) == "ask"


def test_round_six_commit_is_strongly_discouraged(monkeypatch, repo, home):
    _evidence(monkeypatch, 5, head=HEADS[5])
    _mark(repo, home)
    result = _run('git commit -m "more"', repo, home)
    assert _decision(result) == "ask"
    assert "strongly discouraged" in result.stdout


def test_autonomous_commit_is_denied(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run('git commit -m "fix"', repo, home, dispatched=True)
    assert _decision(result) == "deny"
    assert f"standing authorization ended after {_HEAD_LIMIT}" in result.stderr


def test_hard_checks_precede_pending_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    result = _run('git commit --no-verify -m "fix"', repo, home)
    assert _decision(result) == "deny"
    assert "--no-verify" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "one" && git commit --amend -m "two"',
        'git commit -m "one" && git push origin HEAD',
        'git commit -m "one" && gh pr comment 99 --body "@codex review"',
    ],
)
def test_one_approval_cannot_cover_compound_actions(monkeypatch, repo, home, command):
    _evidence(monkeypatch, 4)
    _mark(repo, home)
    result = _run(command, repo, home)
    assert _decision(result) == "deny"
    assert "exactly one fix commit" in result.stderr


def test_docs_skip_and_review_override_still_pass_through_approval(monkeypatch, repo, home):
    _evidence(monkeypatch, 4)
    _git(repo, "reset", "-q", "HEAD", "--", "f.py")
    (repo / "note.md").write_text("documentation\n")
    _git(repo, "add", "note.md")
    docs = _run('git commit -m "docs"', repo, home)
    assert _decision(docs) == "ask"

    _git(repo, "reset", "-q", "HEAD", "--", "note.md")
    _git(repo, "add", "f.py")
    override = _run('git commit -m "fix"  # review-override', repo, home)
    assert _decision(override) == "ask"


def test_unknown_budget_asks_foreground_and_denies_autonomous(monkeypatch, repo, home):
    _evidence(monkeypatch, 0)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "bad-json")
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"', repo, home)) == "ask"
    assert _decision(_run('git commit -m "fix"', repo, home, dispatched=True)) == "deny"


def test_gate_round_two_fix_is_allowed_but_post_confirmation_fix_asks(monkeypatch, repo, home):
    _evidence(monkeypatch, 2, gate=True, head=HEADS[1])
    _mark(repo, home)
    assert _decision(_run('git commit -m "round two fix"', repo, home)) == "allow"

    _evidence(monkeypatch, 3, gate=True, head=HEADS[2])
    after = _run('git commit -m "post confirmation fix"', repo, home)
    assert _decision(after) == "ask"
    assert f"Its {_GATE_LIMIT} discovery rounds plus confirmation are spent" in after.stdout


@pytest.mark.parametrize(
    ("reviewed", "gate", "head", "boundary_claim"),
    [
        # Only the branch where the count IS the ordinary limit may assert the
        # four-head boundary. At five heads that sentence is false, and at the gate
        # limit it is about a different lane entirely.
        pytest.param(4, False, HEADS[4], True, id="ordinary-at-the-terminal-boundary"),
        pytest.param(5, False, HEADS[5], False, id="ordinary-past-it-discouraged"),
        pytest.param(3, True, HEADS[2], False, id="gate-lane-budget-spent"),
    ],
)
def test_every_fix_commit_approval_states_the_terminal_decision(
    monkeypatch, repo, home, reviewed, gate, head, boundary_claim
):
    """A fix commit is protected like a review request, so it owes the same rule.

    All three branches of `_commit_budget_reason` shipped with NO terminal framing
    while the push guard's equivalent had it — the owner reading this prompt was
    told only to "approve this single fix commit", which frames continued round-5
    work as routine at the surface where the decision is actually made. Caught by
    external review on PR #2382, not by that PR's own audit.

    Parametrized across all three branches deliberately. The existing tests above
    pin the SURROUNDING sentences ("standing authorization ended after <limit>",
    "strongly discouraged", "two discovery rounds plus confirmation are spent"),
    every one of which survives if only the terminal framing is deleted — so
    without this test that mutation leaves the file GREEN.

    The NEGATIVE assertions carry as much weight as the positive one, and each was
    a real defect in the first version of this fix:

    * This gate authorizes a fix COMMIT, so no branch may tell the reader their
      approval buys "one further round" — the shared notice's round clause was
      pasted here and contradicted the very next sentence.
    * The four-head boundary claim belongs ONLY in the branch where the count is
      four. The discouraged branch fires at five, where "there is no ordinary round
      5" is simply false, and the gate lane is a different ladder.
    """
    _evidence(monkeypatch, reviewed, gate=gate, head=head)
    _mark(repo, home)
    result = _run('git commit -m "fix"', repo, home)
    assert _decision(result) == "ask"
    assert "MERGE with the outstanding issues accepted and filed" in result.stdout
    assert "SEND IT BACK for rework" in result.stdout
    # A commit gate never authorizes a round, in ANY branch.
    assert "one further round" not in result.stdout, (
        "this approval covers a fix commit, not a review round"
    )
    if boundary_claim:
        assert "ROUND 4 IS TERMINAL" in result.stdout
        assert "there is no ordinary round 5" in result.stdout
    else:
        assert "there is no ordinary round 5" not in result.stdout, (
            "the four-head boundary claim is false in this branch"
        )
        if gate:
            assert "review-gate surface" in result.stdout
        else:
            assert "past the terminal boundary" in result.stdout


def test_proven_no_open_pr_does_not_invent_a_cloud_round(monkeypatch, repo, home):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "none")
    _mark(repo, home)
    assert _decision(_run('git commit -m "local branch"', repo, home)) == "allow"


# ---------------------------------------------------------------------------
# Scope: the budget belongs to the configured PUBLIC repo's open PR, nothing else.
#
# A commit in a scratch repo (no remote, or only a local-path remote) or on a
# branch whose open PR lives in some OTHER repository has no review budget to
# exhaust. Before this scope existed, a failed `gh pr status` in such a repo was
# read as "unreadable history" and the commit ASKED (DENIED when dispatched).
# The fail direction for the public repo itself is unchanged, and is pinned
# below by negative controls.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_scope_inputs(monkeypatch):
    """Pin every ambient input the scope decision reads, so no test depends on
    this install's config or the runner's environment. Empty = undeterminable,
    the pre-existing (engage) behaviour; scope tests set their own value."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "")
    monkeypatch.delenv("GH_REPO", raising=False)


def _failing_gh(tmp_path: Path) -> str:
    """A PATH whose `gh` fails the way `gh pr status` does in a repository gh
    cannot resolve: a message on stderr and exit 1."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\necho 'no git remotes found' >&2\nexit 1\n")
    gh.chmod(0o755)
    return f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"


def _unseamed(monkeypatch, tmp_path: Path) -> None:
    """Drive the REAL lookup path: no PR seam, and a gh that cannot read anything."""
    monkeypatch.delenv("_TEST_REVIEW_BUDGET_PR", raising=False)
    monkeypatch.setenv("PATH", _failing_gh(tmp_path))


def test_repo_with_no_remote_has_no_budget_to_consult(monkeypatch, repo, home, tmp_path):
    """The reported false positive: a throwaway repo with no remote. gh cannot
    resolve any repository, so no open PR can exist for the branch. Allow, in
    the foreground AND in a dispatched session."""
    _unseamed(monkeypatch, tmp_path)
    _mark(repo, home)
    assert _decision(_run('git commit -m "lab"', repo, home)) == "allow"
    assert _decision(_run('git commit -m "lab"', repo, home, dispatched=True)) == "allow"


def test_dash_C_commit_into_remote_less_repo_is_scoped_to_that_repo(
    monkeypatch, repo, home, tmp_path
):
    """`git -C <lab> commit` from a session sitting elsewhere: the scope is the
    repo the commit TARGETS, not the session cwd."""
    _unseamed(monkeypatch, tmp_path)
    _mark(repo, home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(elsewhere),
            "tool_input": {"command": f'git -C {repo} -c user.name=t commit -q -m "lab"'},
        }
    )
    env = {**os.environ, "HOME": str(home)}
    env.pop("GENESIS_CC_SESSION", None)
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert _decision(result) == "allow", result.stdout + result.stderr


def test_local_path_remote_is_not_a_github_remote(monkeypatch, repo, home, tmp_path):
    """A lab clone of a local checkout has only a filesystem remote, which gh
    can never resolve to a pull request."""
    _unseamed(monkeypatch, tmp_path)
    _git(repo, "remote", "add", "origin", str(tmp_path / "upstream-checkout"))
    _git(repo, "remote", "add", "file", "file://" + str(tmp_path / "other"))
    _mark(repo, home)
    assert _decision(_run('git commit -m "lab"', repo, home, dispatched=True)) == "allow"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        # Not provably local: gh might resolve it, so it must not be read as "no remote".
        "relative/dir",
        # Look like paths, but gh parses a HOST out of both (measured, gh 2.100).
        "//github.com/owner/repo",
        "file://github.com/owner/repo",
    ],
)
def test_non_local_remote_with_unreadable_lookup_keeps_todays_behaviour(
    monkeypatch, repo, home, tmp_path, url
):
    """NEGATIVE CONTROL. A branch of the public repo whose PR lookup fails is
    still unknown evidence: ask in the foreground, deny when dispatched."""
    _unseamed(monkeypatch, tmp_path)
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    _git(repo, "remote", "add", "origin", url)
    _mark(repo, home)
    fg = _run('git commit -m "fix"', repo, home)
    assert _decision(fg) == "ask"
    assert "could not be read reliably" in fg.stdout
    assert _decision(_run('git commit -m "fix"', repo, home, dispatched=True)) == "deny"


def test_gh_repo_env_keeps_an_unreadable_lookup_unknown(monkeypatch, repo, home, tmp_path):
    """GH_REPO points gh at a repository regardless of remotes, so a missing
    remote proves nothing there."""
    _unseamed(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_REPO", "owner/repo")
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"', repo, home)) == "ask"
    assert _decision(_run('git commit -m "fix"', repo, home, dispatched=True)) == "deny"


@pytest.mark.parametrize(
    "form",
    [
        "git --git-dir={pub}/.git --work-tree={pub} commit -m fix",
        "git --git-dir {pub}/.git commit -m fix",
        "GIT_DIR={pub}/.git GIT_WORK_TREE={pub} git commit -m fix",
        "export GIT_DIR={pub}/.git; git commit -m fix",
    ],
)
def test_git_dir_redirect_from_a_remote_less_cwd_keeps_unknown(
    monkeypatch, repo, home, tmp_path, form
):
    """NEGATIVE CONTROL. The remote probe reads the commit's cwd, but these
    forms point git at a DIFFERENT repository. That repository may be the public
    one, so a remote-less cwd proves nothing: keep ask / deny."""
    _unseamed(monkeypatch, tmp_path)
    pub = tmp_path / "pub"
    pub.mkdir()
    _git(pub, "init", "-q", "-b", "feature/x")
    _git(pub, "remote", "add", "origin", "https://github.com/owner/repo.git")
    _mark(repo, home)
    command = form.format(pub=pub)
    assert _decision(_run(command, repo, home)) == "ask"
    assert _decision(_run(command, repo, home, dispatched=True)) == "deny"


def test_missing_gh_binary_in_a_remote_less_repo_consults_no_budget(monkeypatch, repo):
    """Launching gh can fail outright (not installed). In a repo with no remote
    that is still a proven no-PR; with a GitHub remote it stays unknown."""
    real_run = subprocess.run

    def no_gh(argv, *args, **kwargs):
        if argv and argv[0] == "gh":
            raise FileNotFoundError("gh")
        return real_run(argv, *args, **kwargs)

    monkeypatch.delenv("_TEST_REVIEW_BUDGET_PR", raising=False)
    monkeypatch.setattr(_guard.subprocess, "run", no_gh)
    command = 'git commit -m "lab"'
    segs, _ = _guard.analyze_checked(command)
    branch = "feature/review-budget"
    assert _guard._branch_review_budget(str(repo), branch, segs=segs, command=command) is None
    # Without the parsed command the gate cannot prove where the commit lands.
    assert _guard._branch_review_budget(str(repo), branch)["status"] == "unknown"
    _git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    result = _guard._branch_review_budget(str(repo), branch, segs=segs, command=command)
    assert result is not None and result["status"] == "unknown"


@pytest.mark.parametrize(
    "form",
    [
        "git -C {cwd} -C {pub} commit -m fix",
        "pushd {pub} && git commit -m fix",
        "env -C {pub} git commit -m fix",
        "sudo git commit -m fix",
        # An earlier segment reshapes the probed path before the commit runs.
        "git -C {pub} worktree add -f {cwd}/sub feature/x && cd {cwd}/sub && git commit -m fix",
        # A cd that exists at hook time can still fail at run time.
        "cd {cwd} || git commit -m fix",
        "cd {cwd}; git commit -m fix",
        # Documented residue: any cd chain keeps the previous behaviour.
        "cd {cwd} && git commit -m fix",
        # Process substitution runs a second command inside ONE parsed segment.
        "git -C {cwd} commit -m fix > >(git -C {pub} commit -m x)",
        "git -C {cwd} commit -m fix 2> >(git -C {pub} commit -m x)",
        "git -C {cwd} commit -F <(git -C {pub} commit -m x)",
    ],
)
def test_other_retargeting_shapes_keep_unknown(monkeypatch, repo, home, tmp_path, form):
    """NEGATIVE CONTROL. The exemption is an allowlist of command shapes that
    provably commit in the probed cwd; every other shape keeps ask / deny, even
    though these particular ones are not individually named anywhere."""
    _unseamed(monkeypatch, tmp_path)
    pub = tmp_path / "pub"
    pub.mkdir()
    _git(pub, "init", "-q", "-b", "feature/x")
    _git(pub, "remote", "add", "origin", "https://github.com/owner/repo.git")
    (repo / "sub").mkdir()  # exists at hook time, as the worktree shape needs
    _mark(repo, home)
    command = form.format(pub=pub, cwd=repo)
    assert _decision(_run(command, repo, home)) == "ask"
    assert _decision(_run(command, repo, home, dispatched=True)) == "deny"


@pytest.mark.parametrize(
    "key,value",
    [
        ("branch.feature/review-budget.remote", "https://github.com/owner/repo.git"),
        ("branch.feature/review-budget.pushRemote", "origin"),
        ("remote.pushDefault", "git@github.com:owner/repo.git"),
    ],
)
def test_branch_push_target_without_a_remote_keeps_unknown(
    monkeypatch, repo, home, tmp_path, key, value
):
    """NEGATIVE CONTROL. `git push -u <url>` leaves a branch target with no
    remote defined; that branch can have an open PR gh simply cannot find."""
    _unseamed(monkeypatch, tmp_path)
    _git(repo, "config", key, value)
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"', repo, home)) == "ask"


def _clone_of_a_local_bare(tmp_path: Path) -> Path:
    """What `git clone <path>` actually leaves behind: a remote named `origin`
    whose URL is a filesystem path, and `branch.<b>.remote = origin` — a remote
    NAME, not a URL (MEASURED, git 2.43)."""
    bare = tmp_path / "upstream.git"
    _git(tmp_path, "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    _git(
        seed,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(bare), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "tester")
    _git(clone, "checkout", "-qb", "feature/review-budget", "--track", "origin/main")
    (clone / "f.py").write_text("value = 2\n")
    _git(clone, "add", "-A")
    return clone


@pytest.mark.parametrize(
    "key",
    [
        None,  # exactly what the clone wrote: branch.<b>.remote = origin
        "branch.feature/review-budget.pushRemote",
        "remote.pushDefault",
    ],
)
def test_a_branch_target_naming_a_local_remote_is_not_github(monkeypatch, home, tmp_path, key):
    """Codex P1 on #2480: the branch target was classified as a URL, so the
    remote NAME `origin` counted as possibly-GitHub and a plain local clone still
    asked. The name resolves to the remote's URLs, all filesystem paths here."""
    _unseamed(monkeypatch, tmp_path)
    clone = _clone_of_a_local_bare(tmp_path)
    got = subprocess.run(
        ["git", "-C", str(clone), "config", "branch.feature/review-budget.remote"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert got.stdout.strip() == "origin"  # the fixture really has the shape
    if key is not None:
        _git(clone, "config", key, "origin")
    _mark(clone, home)
    assert _decision(_run('git commit -m "lab"', clone, home)) == "allow"
    assert _decision(_run('git commit -m "lab"', clone, home, dispatched=True)) == "allow"


@pytest.mark.parametrize(
    "setup",
    [
        ("remote", "set-url", "origin", "https://github.com/owner/repo.git"),
        ("remote", "set-url", "--push", "origin", "git@github.com:owner/repo.git"),
        ("config", "branch.feature/review-budget.pushRemote", "fork"),
    ],
)
def test_a_branch_target_naming_a_github_or_unknown_remote_keeps_unknown(
    monkeypatch, home, tmp_path, setup
):
    """NEGATIVE CONTROL for the resolution above. The same local clone whose
    named remote is a GitHub URL (fetch or push), or whose branch names a remote
    that is not configured at all, keeps ask / deny."""
    _unseamed(monkeypatch, tmp_path)
    clone = _clone_of_a_local_bare(tmp_path)
    _git(clone, *setup)
    _mark(clone, home)
    assert _decision(_run('git commit -m "fix"', clone, home)) == "ask"
    assert _decision(_run('git commit -m "fix"', clone, home, dispatched=True)) == "deny"


def test_a_push_rewrite_to_github_on_a_named_local_remote_keeps_unknown(
    monkeypatch, home, tmp_path
):
    """NEGATIVE CONTROL for the dependency the name resolution relies on: a
    named remote whose local URL `url.<base>.pushInsteadOf` rewrites to GitHub
    is caught only because `git remote -v` lists the REWRITTEN push URL
    (MEASURED, git 2.43). If git stopped applying the rewrite there, the name
    would resolve to a local path and this would wrongly allow."""
    _unseamed(monkeypatch, tmp_path)
    clone = _clone_of_a_local_bare(tmp_path)
    _git(clone, "config", "url.git@github.com:owner/.pushInsteadOf", str(tmp_path) + "/")
    listed = subprocess.run(
        ["git", "-C", str(clone), "remote", "-v"], capture_output=True, text=True, check=True
    ).stdout
    assert "git@github.com:owner/" in listed  # the rewrite really reaches the listing
    _mark(clone, home)
    assert _decision(_run('git commit -m "fix"', clone, home)) == "ask"
    assert _decision(_run('git commit -m "fix"', clone, home, dispatched=True)) == "deny"


def test_local_branch_target_is_still_no_github(monkeypatch, repo, home, tmp_path):
    """`branch.<b>.remote = .` means the local repository itself."""
    _unseamed(monkeypatch, tmp_path)
    _git(repo, "config", "branch.feature/review-budget.remote", ".")
    _mark(repo, home)
    assert _decision(_run('git commit -m "lab"', repo, home, dispatched=True)) == "allow"


def test_open_pr_on_another_repository_consults_no_budget(monkeypatch, repo, home):
    """An exhausted budget on a PR that lives OUTSIDE the configured public repo
    is not this gate's business."""
    _evidence(monkeypatch, 5, head=HEADS[5])
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_REPO", "someone/else")
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    _mark(repo, home)
    assert _decision(_run('git commit -m "fix"', repo, home, dispatched=True)) == "allow"


@pytest.mark.parametrize(
    "canonical",
    [
        "owner/repo",
        "OWNER/Repo",
        "",
        # A wrong OWNER in the config (the example file's placeholder, a display
        # name, a contributor's fork) must not switch the budget off on the real
        # repository: only a different repository NAME proves "unrelated".
        "YOUR_GITHUB_USER/repo",
        "contributor/repo",
        # A free-text setup answer can carry the clone suffix.
        "owner/repo.git",
    ],
)
def test_open_pr_budget_still_applies_on_the_public_repo(monkeypatch, repo, home, canonical):
    """NEGATIVE CONTROL. The public repo's own PR, or any PR while the public
    repo is undeterminable, keeps the exhausted-budget approval."""
    _evidence(monkeypatch, 4)
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", canonical)
    _mark(repo, home)
    result = _run('git commit -m "fix"', repo, home)
    assert _decision(result) == "ask"
    assert f"standing authorization ended after {_HEAD_LIMIT}" in result.stdout


def test_canonical_public_repo_matches_the_push_guards_reading(monkeypatch, tmp_path):
    """Two gates read the same config key. Lock them together so the commit
    gate's scope cannot drift from the push guard's."""
    guard_spec = importlib.util.spec_from_file_location(
        "push_guard_for_scope_parity", ROOT / "scripts" / "hooks" / "git_push_guard.py"
    )
    push_guard = importlib.util.module_from_spec(guard_spec)
    assert guard_spec.loader is not None
    guard_spec.loader.exec_module(push_guard)

    monkeypatch.delenv("_TEST_CANONICAL_PUBLIC_REPO", raising=False)
    config_dir = tmp_path / ".genesis" / "config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    cases = [
        "github:\n  user: Owner\n  public_repo: Project\n",
        "github:\n  user: Owner\n  public_repo: ''\n",
        "github:\n  user: ''\n  public_repo: Project\n",
        "github:\n  user: Owner\n  public_repo: a/b\n",
        "github:\n  user: Owner\n  public_repo: $X\n",
        "other: 1\n",
        "github: [unbalanced\n",
        None,
    ]
    for text in cases:
        cfg = config_dir / "genesis.yaml"
        if text is None:
            cfg.unlink(missing_ok=True)
        else:
            cfg.write_text(text)
        assert _guard._canonical_public_repo() == push_guard._canonical_public_repo(), text
    for seam in ("owner/repo", "github.com/owner/repo", "", "  "):
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", seam)
        assert _guard._canonical_public_repo() == push_guard._canonical_public_repo(), seam
