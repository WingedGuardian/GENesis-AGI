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
import shlex
import subprocess
from pathlib import Path

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
# A resolved cwd for the command; the repo it resolves to comes from a seam.
HERE = str(Path(__file__).resolve().parent)
CTX = {"cwd": HERE}


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


def _main(monkeypatch, command: str, cwd: str | None = HERE, *, dispatched=False) -> int:
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
    """A checkout on branch ``other`` with a ``feat`` branch and an ``origin`` whose
    URL names the PR's repo (the deny checks that). A deny returns before any network
    call; a test that lets the push through stubs the dry run (``_no_network``)."""
    work = tmp_path / "work"
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
    _git(work, "remote", "add", "origin", f"https://github.com/{REPO}.git")
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
    assert f"`! cd {shlex.quote(str(repo))} && {cmd}`" in err
    assert "merge at the terminal head" in err and "send the PR back" in err


def test_a_plain_re_push_of_the_current_branch_is_denied(monkeypatch, capsys, repo):
    _git(repo, "checkout", "-q", "feat")
    _seams(monkeypatch, _budget(current=T))
    assert _main(monkeypatch, "git push", str(repo)) == 2
    assert "PR #7, which is past its terminal review round" in capsys.readouterr().err


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
    _no_network(monkeypatch)
    assert _main(monkeypatch, "git push origin feat", str(repo)) == 0
    assert "past its terminal review round" not in capsys.readouterr().err


def _no_network(monkeypatch) -> None:
    """The push arm's own probes that would reach the remote: the dry run and the
    remote-branch read. None is their documented "could not tell"."""
    monkeypatch.setattr(G, "_push_dry_run_verdict", lambda *a, **k: None)
    monkeypatch.setattr(G, "_remote_branch_sha", lambda *a, **k: None)


def _verdict(monkeypatch, capsys, cmd: str, cwd: str) -> tuple[int, str, str]:
    rc = _main(monkeypatch, cmd, cwd)
    got = capsys.readouterr()
    return rc, got.out, got.err


def test_below_terminal_a_push_keeps_its_old_verdict(monkeypatch, capsys, repo):
    """main(): a PR branch below terminal gets exactly the verdict a branch with no
    PR gets, so the lookup changes nothing there."""
    _no_network(monkeypatch)
    _seams(monkeypatch, _budget(current=T, state="below"), prs={})
    without = _verdict(monkeypatch, capsys, "git push origin feat", str(repo))
    _seams(monkeypatch, _budget(current=T, state="below"))
    with_pr = _verdict(monkeypatch, capsys, "git push origin feat", str(repo))
    assert with_pr == without
    assert with_pr[0] == 0 and "past its terminal" not in with_pr[2]


def test_a_pr_whose_repo_is_not_the_push_destination_is_ignored(monkeypatch, repo):
    """gh may resolve another repo than the one the push reaches; the PR's own URL
    must name the push destination, as `_open_pr_count_for_branch` requires."""
    elsewhere = {
        "feat": [
            {"number": 7, "url": "https://github.com/other/repo/pull/7", "isCrossRepository": False}
        ]
    }
    _seams(monkeypatch, _budget(current=T), prs=elsewhere)
    assert _push_verdict(repo, "git push origin feat") is None


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
    assert f"`! cd {shlex.quote(HERE)} && {cmd}`" in err


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
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


def test_the_review_request_ask_stays_at_the_terminal_head(monkeypatch):
    _seams(monkeypatch, _budget(current=T))
    cmd = CODEX_ROUTES[0]
    assert G._check_codex_round_escalation(G.analyze(cmd), cmd, CTX)[0] == "ask"


def test_the_review_request_ask_becomes_a_deny_off_the_terminal_head(monkeypatch):
    _seams(monkeypatch, _budget(current=MOVED))
    cmd = CODEX_ROUTES[0]
    decision, reason = G._check_codex_round_escalation(G.analyze(cmd), cmd, CTX)
    assert decision == "deny"
    assert "past its terminal review round" in reason


@pytest.mark.parametrize("cmd", CODEX_ROUTES[2:])
def test_codex_on_a_number_that_is_not_a_pr_is_unaffected(monkeypatch, cmd):
    """R-S3: an issue number reads as an unknown budget (GraphQL has no such PR)."""
    _seams(monkeypatch, {"status": "unknown", "errors": ["graphql_errors"]})
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


def test_codex_below_terminal_is_unaffected(monkeypatch):
    _seams(monkeypatch, _budget(current=MOVED, state="below"))
    for cmd in CODEX_ROUTES[1:]:
        assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


def test_a_comment_without_codex_is_unaffected(monkeypatch):
    _seams(monkeypatch, _budget(current=MOVED))
    cmd = f'gh issue comment 7 --repo {REPO} --body "Fixed in abc123"'
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


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
    assert f"`! cd {shlex.quote(HERE)} && {cmd}`" in err


@pytest.mark.parametrize("cmd", REWRITES)
@pytest.mark.parametrize("state", ["CLOSED", "ISSUE", None])
def test_a_comment_rewrite_off_an_open_pr_is_unaffected(monkeypatch, cmd, state):
    """A closed PR, an issue, and an unreadable lookup (None) all allow: best
    effort, by owner ruling."""
    states = {} if state is None else {"pr:7": state, "comment:123": state}
    _seams(monkeypatch, _budget(current=T, state="below"), states=states)
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


def test_a_get_of_a_comment_is_not_a_rewrite(monkeypatch):
    _seams(monkeypatch, _budget(current=T), states={"comment:123": "OPEN"})
    cmd = f"gh api repos/{REPO}/issues/comments/123"
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


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
# -- review findings on b426a8ff8 ----------------------------------------------


def test_codex_review_at_the_terminal_head_still_asks_through_main(monkeypatch, capsys):
    """main(), F-B3: at the terminal head `@codex review` keeps the native ask."""
    _seams(monkeypatch, _budget(current=T))
    assert _main(monkeypatch, CODEX_ROUTES[0]) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


NUMBERLESS_CODEX = [
    'gh pr comment --body "@codex please look again"',
    'gh pr comment --body "@codex review"',
    'gh pr review --comment -b "@codex review"',
]


@pytest.mark.parametrize("cmd", NUMBERLESS_CODEX)
def test_codex_with_no_pr_number_resolves_the_current_branch_pr(monkeypatch, capsys, cmd):
    _seams(monkeypatch, _budget(current=MOVED))
    seen = []
    monkeypatch.setattr(
        G, "_resolve_pr_number", lambda c, repo=None, cwd=None: seen.append(cwd) or "7"
    )
    assert _main(monkeypatch, cmd) == 2
    assert "PR #7, which is past its terminal review round" in capsys.readouterr().err
    assert seen == [HERE]  # resolved in the command's own cwd


@pytest.mark.parametrize(
    "cmd",
    [
        'gh pr comment --body "@codex please look again"',
        'gh api repos/{owner}/{repo}/issues/7/comments -f body="@codex review"',
        "gh pr comment --delete-last --yes",
    ],
)
def test_with_no_resolved_cwd_and_no_repo_a_route_is_not_judged(monkeypatch, cmd):
    """Never the hook's own directory: with no cwd in the payload and no -R, the
    route keeps today's behaviour."""
    _seams(monkeypatch, _budget(current=MOVED), states={"pr:": "OPEN"})
    monkeypatch.setattr(G, "_resolve_pr_number", lambda *a, **k: pytest.fail("resolved"))
    monkeypatch.setattr(G, "_derive_repo_from_cwd", lambda *a, **k: pytest.fail("derived"))
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, {}) is None


@pytest.mark.parametrize(
    "cmd",
    [
        'gh pr comment "$N" --delete-last --yes',
        'gh issue comment 7 -R "$R" --edit-last --body x',
        'gh pr update-branch "$N"',
        'gh pr review "$N" --comment -b "@codex review"',
    ],
)
def test_an_unresolvable_target_is_no_match(monkeypatch, cmd):
    """A target the guard cannot read is not judged against the current branch's
    PR: best effort, by owner ruling."""
    _seams(monkeypatch, _budget(current=MOVED), states={"pr:": "OPEN", "pr:7": "OPEN"})
    monkeypatch.setattr(G, "_resolve_pr_number", lambda *a, **k: "7")
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


def test_an_absolute_api_url_is_read_as_its_path(monkeypatch, capsys):
    _seams(monkeypatch, _budget(current=MOVED), states={"comment:123": "OPEN"})
    delete = f"gh api -X DELETE https://api.github.com/repos/{REPO}/issues/comments/123"
    assert _main(monkeypatch, delete) == 2
    assert "deletes or edits a comment" in capsys.readouterr().err
    codex = f'gh api https://api.github.com/repos/{REPO}/issues/7/comments -f "body=@codex review"'
    assert _main(monkeypatch, codex) == 2
    assert "past its terminal review round" in capsys.readouterr().err


GRAPHQL = "gh api graphql -f query='mutation {{ {name}(input: {{id: \"IC_x\"}}) {{ clientMutationId }} }}'"


@pytest.mark.parametrize(
    "name",
    [
        "deleteIssueComment",
        "updateIssueComment",
        "minimizeComment",
        "deletePullRequestReviewComment",
        "updatePullRequestReviewComment",
    ],
)
def test_graphql_comment_rewrites_are_denied_while_an_open_pr_exists(monkeypatch, capsys, name):
    cmd = GRAPHQL.format(name=name)
    _seams(monkeypatch, _budget(current=T, state="below"), states={"repo:": "OPEN"})
    assert _main(monkeypatch, cmd) == 2
    assert "deletes or edits a comment" in capsys.readouterr().err
    _seams(monkeypatch, _budget(current=T, state="below"), states={})
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


def test_a_graphql_read_is_not_a_rewrite(monkeypatch):
    cmd = "gh api graphql -f query='query { viewer { login } }'"
    _seams(monkeypatch, _budget(current=T), states={"repo:": "OPEN"})
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None
# -- round 1 findings on #5065 -------------------------------------------------


def test_the_owner_command_keeps_the_directory_of_a_push(monkeypatch, capsys, repo, tmp_path):
    """Devin 4703792023: a deny aborts the whole Bash call, so a `cd` before the
    push never ran; the bang command must name the push's own directory."""
    _seams(monkeypatch, _budget(current=T))
    cmd = f"cd {repo} && git push origin feat"
    assert _main(monkeypatch, cmd, str(tmp_path)) == 2
    assert f"`! cd {shlex.quote(str(repo))} && git push origin feat`" in capsys.readouterr().err


def test_the_owner_command_keeps_the_directory_of_a_gh_route(monkeypatch, capsys, tmp_path):
    _seams(monkeypatch, _budget(current=MOVED))
    target = tmp_path / "other checkout"
    target.mkdir()
    cmd = f"cd '{target}' && gh issue comment 7 --repo {REPO} --body '@codex review'"
    assert _main(monkeypatch, cmd) == 2
    err = capsys.readouterr().err
    assert f"`! cd {shlex.quote(str(target))} && gh issue comment 7 --repo {REPO}" in err


def test_the_review_request_deny_keeps_the_directory(monkeypatch, tmp_path):
    _seams(monkeypatch, _budget(current=MOVED))
    cmd = f"cd {tmp_path} && {CODEX_ROUTES[0]}"
    decision, reason = G._check_codex_round_escalation(G.analyze(cmd), cmd, CTX)
    assert decision == "deny"
    assert f"`! cd {shlex.quote(str(tmp_path))} && {CODEX_ROUTES[0]}`" in reason


# Codex 4704023791 and Devin 4703792118: the delivery-marker command compose prints.


def _marker_cmd(tmp_path) -> str:
    body = tmp_path / "pr-7-marker.md"
    body.write_text("<!-- genesis-delivery-check: ... -->\n@codex review is quoted here\n")
    return f"gh api repos/{REPO}/issues/7/comments -F body=@{body}"


@pytest.mark.parametrize("state", ["below", "past"])
@pytest.mark.parametrize("dispatched", [False, True])
def test_the_delivery_marker_post_is_allowed_and_raises_no_ask(
    monkeypatch, capsys, tmp_path, state, dispatched
):
    _seams(monkeypatch, _budget(current=MOVED, state=state))
    assert _main(monkeypatch, _marker_cmd(tmp_path), dispatched=dispatched) == 0
    got = capsys.readouterr()
    assert "permissionDecision" not in got.out, got.out
    assert "BLOCKED" not in got.err


def test_an_opaque_body_file_field_is_no_codex_match(monkeypatch, tmp_path):
    """A typed `-F body=@file` is a file the guard does not read, even when its name
    starts `codex`; a raw `-f body=@codex ...` is visible text and still matches."""
    _seams(monkeypatch, _budget(current=MOVED))
    opaque = f"gh api repos/{REPO}/issues/7/comments -F body=@codex.md"
    assert G._past_terminal_route_deny(G.analyze(opaque), opaque, CTX) is None
    raw = f"gh api repos/{REPO}/issues/7/comments -f body=@codex.md"
    assert G._past_terminal_route_deny(G.analyze(raw), raw, CTX) is not None


# Devin 4703792173: no empty-commit allowance for a review request.

E1 = "e" * 40  # an empty commit on top of T


def _empty_on_terminal() -> dict:
    budget = _budget(current=E1)
    budget["commits"] = [
        {"sha": T, "parents": ["c" * 40], "tree": "d" * 40},
        {"sha": E1, "parents": [T], "tree": "d" * 40},
    ]
    return budget


@pytest.mark.parametrize("cmd", CODEX_ROUTES)
def test_codex_at_an_empty_commit_on_the_terminal_head_is_denied(monkeypatch, cmd):
    budget = _empty_on_terminal()
    assert G._check_past_terminal("7", REPO, E1, budget=budget)[0] is False  # the merge passes
    _seams(monkeypatch, budget)
    assert _main(monkeypatch, cmd) == 2


# CodeRabbit 4703812706: an explicit false is unset.


def test_an_explicit_false_rewrite_flag_is_unset(monkeypatch):
    _seams(monkeypatch, _budget(current=T, state="below"), states={"pr:7": "OPEN"})
    off = f"gh pr comment 7 --repo {REPO} --edit-last=false --body x"
    assert G._past_terminal_route_deny(G.analyze(off), off, CTX) is None
    on = f"gh pr comment 7 --repo {REPO} --delete-last=true --yes"
    assert G._past_terminal_route_deny(G.analyze(on), on, CTX) is not None


# CodeRabbit 4703812712: only a mutation's code, never a read or a string.


@pytest.mark.parametrize(
    "query",
    [
        'query { search(query: "deleteIssueComment", type: ISSUE, first: 1) { issueCount } }',
        "query { viewer { login } } # deleteIssueComment",
        'mutation { addComment(input: {subjectId: "x", body: "updateIssueComment"}) { clientMutationId } }',
    ],
)
def test_graphql_names_in_a_read_a_string_or_a_comment_are_no_match(monkeypatch, query):
    cmd = f"gh api graphql -f query='{query}'"
    _seams(monkeypatch, _budget(current=T), states={"repo:": "OPEN"})
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


# GLM P2: deleting or editing a whole review.

REVIEW_REWRITES = [
    f"gh api -X PUT repos/{REPO}/pulls/7/reviews/99 -f body=x",
    "gh api --method PATCH repos/{owner}/{repo}/pulls/7/reviews/99 -f body=x",
    GRAPHQL.format(name="updatePullRequestReview"),
]


@pytest.mark.parametrize(
    "cmd",
    [
        f"gh api -X DELETE repos/{REPO}/pulls/7/reviews/99",
        GRAPHQL.format(name="deletePullRequestReview"),
    ],
)
def test_deleting_a_pending_review_is_not_denied(monkeypatch, cmd):
    # GitHub deletes only a pending review, which counts no round: nothing to protect.
    _seams(monkeypatch, _budget(current=T, state="below"), states={"pr:7": "OPEN", "repo:": "OPEN"})
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


@pytest.mark.parametrize("cmd", REVIEW_REWRITES)
def test_a_review_delete_or_edit_on_an_open_pr_is_denied(monkeypatch, capsys, cmd):
    _seams(monkeypatch, _budget(current=T, state="below"), states={"pr:7": "OPEN", "repo:": "OPEN"})
    assert _main(monkeypatch, cmd) == 2
    assert "deletes or edits a comment" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", REVIEW_REWRITES)
def test_a_review_delete_or_edit_off_an_open_pr_is_unaffected(monkeypatch, cmd):
    _seams(monkeypatch, _budget(current=T, state="below"), states={"pr:7": "CLOSED"})
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


def test_reading_a_review_is_not_a_rewrite(monkeypatch):
    _seams(monkeypatch, _budget(current=T), states={"pr:7": "OPEN"})
    cmd = f"gh api repos/{REPO}/pulls/7/reviews/99"
    assert G._past_terminal_route_deny(G.analyze(cmd), cmd, CTX) is None


# GLM P3: a full --limit window is a truncated read.


def test_a_full_pr_list_window_is_unknown(monkeypatch, repo):
    _seams(monkeypatch, _budget(current=T))
    monkeypatch.setattr(G, "_PR_LIST_WINDOW", 1)
    assert _push_verdict(repo, "git push origin feat") is None
