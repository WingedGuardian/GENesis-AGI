"""Past-terminal route friction (owner ruling 3f6ad6fb, 2026-11-25; plan PR3).

The guarantee is the merge check (tests/test_hooks/test_past_terminal_merge.py).
This file covers the friction in front of it, in the push guard:

- a ``git push`` whose destination is a past-terminal PR's head branch is denied,
  in every spelling (R-S1);
- ``gh pr update-branch N`` on a past-terminal PR is denied;
- an ``@codex`` mention on a confirmed past-terminal PR whose head moved off the
  terminal head is denied; at the terminal head it is unchanged (F-B3);
- an agent's delete or edit of a comment on any OPEN PR is denied (F-S2);
- unknown evidence keeps today's behaviour (R-S4); a dispatched session is denied
  the same way as a foreground one.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.test_hooks import test_merge_gate_characterization as ch

# The characterization module's autouse fixtures, re-exported so they apply here.
from tests.test_hooks.test_merge_gate_characterization import (  # noqa: F401
    _hermetic_pr_files,
    _pin_canonical_public_repo,
    _reset_merge_deadline,
)

G = ch._mod

T = "a" * 40  # the terminal head
MOVED = "b" * 40  # a head after it that changed a file
REPO = "example/repo"
PR_URL = f"https://github.com/{REPO}/pull/7"


def _budget(*, current: str, state: str = "past", status: str = "ok") -> dict:
    return {
        "status": status,
        "terminal_state": state,
        "terminal_head": T if state == "past" else None,
        "terminal_error": None,
        "current_head": current,
        "gate_surface": False,
        "approval_required": state == "past",
        "commits": [
            {"sha": T, "parents": ["c" * 40], "tree": "d" * 40},
            {"sha": MOVED, "parents": [T], "tree": "e" * 40},
        ],
    }


def _seams(monkeypatch, budget: dict, *, prs=None, states=None) -> None:
    monkeypatch.setenv("_TEST_TERMINAL_BUDGET", json.dumps(budget))
    monkeypatch.setattr(G._review_budget, "evaluate_pr", lambda *a, **k: dict(budget))
    monkeypatch.setenv(
        "_TEST_GH_BRANCH_PRS",
        json.dumps(
            {"feat": [{"number": 7, "url": PR_URL, "isCrossRepository": False}]}
            if prs is None
            else prs
        ),
    )
    monkeypatch.setenv("_TEST_GH_PR_STATE", json.dumps(states or {}))
    monkeypatch.setenv("_TEST_GH_DERIVED_REPO", REPO)


def _main(monkeypatch, command: str, cwd: str | None = None, *, dispatched=False) -> int:
    monkeypatch.delenv("CLAUDE_TOOL_INPUT", raising=False)
    if dispatched:
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    else:
        monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    payload = {"tool_input": {"command": command}}
    if cwd:
        payload["cwd"] = cwd
    monkeypatch.setattr(G, "read_payload", lambda: payload)
    return G.main()


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A checkout on branch ``other`` with a local bare ``origin`` and a ``feat``
    branch, so no push spelling below reaches the network."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "other", str(work)], check=True)
    _git(
        work,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "branch", "feat")
    return work


# -- git push onto a past-terminal PR's head branch ----------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        "git push origin feat",
        "git push origin HEAD:refs/heads/feat",
        "git push origin {sha}:feat",
        "git push origin other:refs/heads/feat",
        "git push origin --delete feat",
    ],
)
def test_a_push_onto_a_past_terminal_pr_branch_is_denied(monkeypatch, capsys, repo, spelling):
    _seams(monkeypatch, _budget(current=T))
    cmd = spelling.format(sha=_git(repo, "rev-parse", "HEAD"))
    assert _main(monkeypatch, cmd, str(repo)) == 2
    err = capsys.readouterr().err
    assert "PR #7, which is past its terminal review round" in err
    assert f"`! {cmd}`" in err
    assert "merge at the terminal head" in err and "send the PR back" in err


def test_a_plain_re_push_of_the_current_branch_is_denied(monkeypatch, capsys, repo):
    _git(repo, "checkout", "-q", "feat")
    _seams(monkeypatch, _budget(current=T))
    assert _main(monkeypatch, "git push", str(repo)) == 2
    assert "PR #7, which is past its terminal review round" in capsys.readouterr().err


def test_a_force_push_onto_the_pr_branch_is_denied(monkeypatch, repo):
    _seams(monkeypatch, _budget(current=T))
    assert _main(monkeypatch, "git push --force origin feat", str(repo)) == 2


def test_a_dispatched_session_is_denied_the_same_way(monkeypatch, repo):
    _seams(monkeypatch, _budget(current=T))
    assert _main(monkeypatch, "git push origin feat", str(repo), dispatched=True) == 2


def _push_verdict(repo, cmd: str, cur: str = "other"):
    seg = G.analyze(cmd)[0]
    return G._past_terminal_push_deny(seg, cur, "origin", str(repo))


def test_the_destination_not_the_current_branch_is_judged(monkeypatch, repo):
    # Current branch `other` has no PR; the refspec lands on `feat`, which has one.
    _seams(monkeypatch, _budget(current=T))
    assert _push_verdict(repo, "git push origin HEAD:refs/heads/feat") is not None
    assert _push_verdict(repo, "git push origin other") is None


def test_a_dry_run_push_is_unaffected(monkeypatch, repo):
    _seams(monkeypatch, _budget(current=T))
    assert _push_verdict(repo, "git push --dry-run origin feat") is None


def test_a_push_to_a_branch_with_no_open_pr_is_unaffected(monkeypatch, repo):
    _seams(monkeypatch, _budget(current=T), prs={})
    assert _push_verdict(repo, "git push origin feat") is None


def test_below_terminal_a_push_is_unaffected(monkeypatch, repo):
    _seams(monkeypatch, _budget(current=T, state="below"))
    assert _push_verdict(repo, "git push origin feat") is None


def test_unknown_evidence_at_the_push_allows(monkeypatch, capsys, repo):
    """R-S4: an unreadable budget keeps today's behaviour at the push, even when
    the unreadable result carries a stale ``past`` (the status term decides)."""
    _seams(monkeypatch, _budget(current=T, status="unknown"))
    assert _push_verdict(repo, "git push origin feat") is None
    assert _main(monkeypatch, "git push origin feat", str(repo)) == 0
    assert "past its terminal review round" not in capsys.readouterr().err


def test_a_cross_repository_pr_on_the_same_branch_name_is_ignored(monkeypatch, repo):
    fork = {"feat": [{"number": 7, "url": PR_URL, "isCrossRepository": True}]}
    _seams(monkeypatch, _budget(current=T), prs=fork)
    assert _push_verdict(repo, "git push origin feat") is None


# -- gh pr update-branch -------------------------------------------------------


@pytest.mark.parametrize("dispatched", [False, True])
def test_update_branch_past_terminal_is_denied(monkeypatch, capsys, dispatched):
    _seams(monkeypatch, _budget(current=T))
    cmd = f"gh pr update-branch 7 --repo {REPO}"
    assert _main(monkeypatch, cmd, dispatched=dispatched) == 2
    err = capsys.readouterr().err
    assert "PR #7, which is past its terminal review round" in err
    assert f"`! {cmd}`" in err


def test_update_branch_below_terminal_is_unchanged(monkeypatch):
    _seams(monkeypatch, _budget(current=T, state="below"))
    assert _main(monkeypatch, f"gh pr update-branch 7 --repo {REPO}") == 0


# -- @codex on a confirmed past-terminal PR ------------------------------------

CODEX_ROUTES = [
    f'gh pr comment 7 --repo {REPO} --body "@codex review"',
    f'gh pr comment 7 --repo {REPO} --body "@codex please look again"',
    f'gh issue comment 7 --repo {REPO} --body "@codex review"',
    'gh api repos/{owner}/{repo}/issues/7/comments -f body="@codex review"',
    f'gh api repos/{REPO}/issues/7/comments -f "body=@Codex review"',
    f'gh pr review 7 --repo {REPO} --comment --body "@codex review"',
]


@pytest.mark.parametrize("cmd", CODEX_ROUTES)
@pytest.mark.parametrize("dispatched", [False, True])
def test_codex_off_the_terminal_head_is_denied(monkeypatch, capsys, cmd, dispatched):
    _seams(monkeypatch, _budget(current=MOVED))
    assert _main(monkeypatch, cmd, dispatched=dispatched) == 2
    err = capsys.readouterr().err
    assert "PR #7, which is past its terminal review round" in err
    assert "merge at the terminal head" in err


@pytest.mark.parametrize("cmd", CODEX_ROUTES[1:])
def test_codex_at_the_terminal_head_is_unchanged(monkeypatch, cmd):
    """F-B3: at the terminal head an @codex request opens no round, and Codex at
    the head is needed to merge."""
    _seams(monkeypatch, _budget(current=T))
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, {}) is None


def test_the_review_request_ask_stays_at_the_terminal_head(monkeypatch):
    _seams(monkeypatch, _budget(current=T))
    cmd = CODEX_ROUTES[0]
    assert G._check_codex_round_escalation(G.analyze(cmd), cmd, {})[0] == "ask"


def test_the_review_request_ask_becomes_a_deny_off_the_terminal_head(monkeypatch):
    _seams(monkeypatch, _budget(current=MOVED))
    cmd = CODEX_ROUTES[0]
    decision, reason = G._check_codex_round_escalation(G.analyze(cmd), cmd, {})
    assert decision == "deny"
    assert "past its terminal review round" in reason


@pytest.mark.parametrize("cmd", CODEX_ROUTES[2:])
def test_codex_on_a_number_that_is_not_a_pr_is_unaffected(monkeypatch, cmd):
    """R-S3: an issue number reads as an unknown budget (GraphQL has no such PR)."""
    _seams(monkeypatch, {"status": "unknown", "errors": ["graphql_errors"]})
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, {}) is None


def test_codex_below_terminal_is_unaffected(monkeypatch):
    _seams(monkeypatch, _budget(current=MOVED, state="below"))
    for cmd in CODEX_ROUTES[1:]:
        assert G._past_terminal_route_deny(G.analyze(cmd), cmd, {}) is None


def test_a_comment_without_codex_is_unaffected(monkeypatch):
    _seams(monkeypatch, _budget(current=MOVED))
    cmd = f'gh issue comment 7 --repo {REPO} --body "Fixed in abc123"'
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, {}) is None


# -- F-S2: deleting or editing a comment on an open PR -------------------------

REWRITES = [
    f"gh pr comment 7 --repo {REPO} --delete-last --yes",
    f'gh pr comment 7 --repo {REPO} --edit-last --body "x"',
    f'gh issue comment 7 --repo {REPO} --edit-last --body "x"',
    f"gh api -X DELETE repos/{REPO}/issues/comments/123",
    "gh api --method PATCH repos/{owner}/{repo}/issues/comments/123 -f body=x",
    f"gh api -X DELETE repos/{REPO}/pulls/comments/123",
]


@pytest.mark.parametrize("cmd", REWRITES)
@pytest.mark.parametrize("dispatched", [False, True])
def test_a_comment_rewrite_on_an_open_pr_is_denied(monkeypatch, capsys, cmd, dispatched):
    # Below terminal on purpose: F-S2 holds on ANY open PR.
    _seams(
        monkeypatch,
        _budget(current=T, state="below"),
        states={"pr:7": "OPEN", "comment:123": "OPEN"},
    )
    assert _main(monkeypatch, cmd, dispatched=dispatched) == 2
    err = capsys.readouterr().err
    assert "deletes or edits a comment on an open pull request" in err
    assert f"`! {cmd}`" in err


@pytest.mark.parametrize("cmd", REWRITES)
@pytest.mark.parametrize("state", ["CLOSED", "ISSUE", None])
def test_a_comment_rewrite_off_an_open_pr_is_unaffected(monkeypatch, cmd, state):
    """A closed PR, an issue, and an unreadable lookup (None) all allow: best
    effort, by owner ruling."""
    states = {} if state is None else {"pr:7": state, "comment:123": state}
    _seams(monkeypatch, _budget(current=T, state="below"), states=states)
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, {}) is None


def test_a_get_of_a_comment_is_not_a_rewrite(monkeypatch):
    _seams(monkeypatch, _budget(current=T), states={"comment:123": "OPEN"})
    cmd = f"gh api repos/{REPO}/issues/comments/123"
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, {}) is None


# -- F-S5: the stale-base advice past terminal ---------------------------------


def test_stale_base_advice_past_terminal_does_not_say_update(monkeypatch):
    monkeypatch.delenv("_TEST_STALE_BASE_NOTE", raising=False)
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", T)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv(
        "_TEST_GH_PR_CI_RUNS",
        json.dumps({"ci": [{"databaseId": 1, "createdAt": "2026-07-01T00:00:00Z"}]}),
    )
    monkeypatch.setattr(G, "_required_ci_workflows", lambda: ["ci"])
    monkeypatch.setenv("_TEST_GH_PR_FILES", '{"filename": "src/x.py", "previous_filename": null}')
    monkeypatch.setenv(
        "_TEST_GH_PATH_COMMITS_SINCE", json.dumps({"src/x.py": [{"sha": "f" * 40, "subject": "s"}]})
    )
    below = G._stale_base_note("7", repo=REPO, head=T)
    assert below and "gh pr update-branch 7" in below
    past = G._stale_base_note("7", repo=REPO, head=T, past_terminal=True)
    assert past and "send it back" in past
    assert "`! gh pr update-branch 7 --repo example/repo`" in past
    assert "If they may, update the branch" not in past
