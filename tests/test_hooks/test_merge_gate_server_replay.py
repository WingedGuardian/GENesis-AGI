"""The merge gate as the server runs it: ``git_push_guard.py --check-pr`` in a child.

``.github/workflows/merge-gate.yml`` runs ``python3 scripts/hooks/git_push_guard.py
--check-pr N --repo R`` and turns its exit code into the published verdict. The
characterization corpus calls ``check_pr_report`` in-process and stubs the readers of
five rows, so the code that fetches GitHub's answers and parses them is never
exercised there. This file runs the real script as a SUBPROCESS, with the workflow's
environment, and answers every ``gh`` call from recorded PR state.

THE FAKE ``gh``. A shim first on ``PATH`` runs ``fake_gh_replay.py`` beside this file
as a script. It routes on argv, applies the caller's ``--jq`` with the real
``jq`` (``gh --jq`` prints strings raw and everything else as compact JSON, measured
2026-11-08 against gh 2.101.0), and FAILS LOUDLY on any argv it does not know: exit
97 and a ``FAKE-GH-UNROUTED`` line, which every test asserts is absent. A new ``gh``
read in the report therefore fails here until a route records its answer. GraphQL
answers only an exact stored copy of each gate query (``GRAPHQL_DOCUMENTS``), so a
changed query is refused, and the drift test below names the document to re-record.

NO ``_TEST_GH_*`` SEAMS. Each one returns before its ``gh`` call, which would skip the
fetch and parse this file exists to run. The only seam set is
``_TEST_CANONICAL_PUBLIC_REPO``, which the workflow itself sets. The child's
environment is built from nothing: the conftest's autouse ``_TEST_*`` pins, a
developer's ``GH_TOKEN`` and an outer job's ``GITHUB_*`` never reach it.

THE ROUTES, one per ``gh``-calling function reachable from ``check_pr_report``
(an AST walk of the guard, plus ``review_budget.evaluate_pr`` behind the ``rounds``
row). ``test_every_reachable_gh_caller_is_known`` repeats that walk, so a new caller
fails here by name. Not every caller is reached by a fixture: the two ``gh run``
readers only ever see the refusal the workflow's token gets (it has no
``actions: read``), so ``_run_failed_jobs`` and ``_base_commits_to_path`` stay
behind them; ``_derive_repo_from_cwd`` runs only without ``--repo``; and the
``main-reverts`` readers (``_commit_touched_files``, ``_path_in_base_history``),
``_rework_is_issue`` and review_budget's renamed-file read need states no fixture
here builds.

Synthetic values throughout: the repository, PR number, logins and shas are made up.
"""

from __future__ import annotations
import __future__

import ast
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_hooks.fake_gh_replay import (
    GRAPHQL_DOCUMENTS,
    LOG_ENV,
    REVIEW_BUDGET_READS,
    STATE_ENV,
    UNROUTED,
    _normalize,
)

_ROOT = Path(__file__).resolve().parents[2]
_GUARD = _ROOT / "scripts" / "hooks" / "git_push_guard.py"
_REVIEW_BUDGET = _ROOT / "scripts" / "review_budget.py"
#: Resolved from this file, so a mutation sweep's copy runs its own copy of the fake.
_FAKE_GH = Path(__file__).resolve().with_name("fake_gh_replay.py")

REPO = "example-owner/example-repo"
PR = "4242"
HEAD = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
STALE = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"
BASE_TIP = "0b1c2d3e4f5061728394a5b6c7d8e9f00b1c2d3e"
CODEX = "chatgpt-codex-connector[bot]"
OWNER_LOGIN = "example-owner"

#: Every function in the guard that runs ``gh`` and is reachable from
#: ``check_pr_report``, with the route that answers it. Kept by hand so the
#: report can name them; the AST test keeps it complete.
GH_CALLERS = {
    "_check_mergeable": "pr view --json mergeable",
    "_pr_ci_status": "pr view --json statusCheckRollup",
    "_pr_head_sha": "pr view --json headRefOid",
    "_pr_base_ref": "pr view --json baseRefName",
    "_pr_body_text": "pr view --json body",
    "_pr_created_at": "pr view --json createdAt",
    "_mechanical_scan_is_green": "pr view --json headRefOid,statusCheckRollup",
    "_repo_default_branch": "repo view --json defaultBranchRef",
    "_derive_repo_from_cwd": "repo view --json nameWithOwner",
    "_rework_sent_back": "pr list --label",
    "_newest_pr_ci_run": "run list --commit",
    "_main_ci_runs": "run list --branch",
    "_run_failed_jobs": "run view --json jobs",
    "_pr_base_sha": "api graphql, stored document base_ref",
    "_codex_signal_evidence": "api graphql, stored document codex_signal",
    "_pr_outside_facts": "api graphql, stored document outside_pr",
    "_fetch_comments_paged": "api pulls|issues/N/<endpoint> -X GET page=",
    "_pr_review_records": "api pulls/N/reviews --paginate",
    "_scheduled_review_rows": "api issues/N/comments | pulls/N/reviews --paginate",
    "_pr_changed_files_uncached": "api pulls/N/files --paginate",
    "_pr_commit_rows": "api pulls/N/commits",
    "_commit_at_prefix": "api commits/<sha>",
    "_commit_touched_files": "api commits/<sha> --paginate",
    "_classify_post_review_delta": "api compare/<a>...<b>",
    "_pr_contribution": "api compare/<a>...<b>",
    "_sha_is_ancestor": "api compare/<a>...<b>",
    "_base_commits_to_path": "api commits?sha=&path=&since=",
    "_path_in_base_history": "api commits -X GET path= sha=",
    "_pin_file_at_ref": "api contents/<pin file>?ref=",
    "_rework_is_issue": "api issues/<n>",
    "_rework_timeline": "api pulls/<n> | issues/<n>/timeline",
    "_rework_ack_comments": "api issues/<n>/comments",
}

#: Outside the guard: the ``rounds`` row's evaluator.
REVIEW_BUDGET_GH = {
    "snapshot": "api graphql, stored documents review_budget(_reread)",
    "_evaluate_pr_inner": "api pulls/N/files --paginate (a renamed file only)",
}


# ---------------------------------------------------------------------------
# Recorded PR state: raw GitHub responses, before any --jq.
# ---------------------------------------------------------------------------


def _user(login: str, kind: str = "User") -> dict:
    return {"login": login, "type": kind}


def _check_run(name: str, conclusion: str, run_id: int, workflow: str = "CI") -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": workflow,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "detailsUrl": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{run_id + 1}",
    }


def _codex_review(commit: str, review_id: int = 7001) -> dict:
    return {
        "id": review_id,
        "user": _user(CODEX, "Bot"),
        "author_association": "NONE",
        "commit_id": commit,
        "state": "COMMENTED",
        "body": "",
        "submitted_at": "2026-01-02T03:04:05Z",
    }


def _scheduled_comment(head: str) -> dict:
    markers = "\n".join(
        f"<!-- genesis-scheduled-review: head={head} kind={kind} -->"
        for kind in ("code-review", "leaks")
    )
    return {
        "id": 8001,
        "user": _user(OWNER_LOGIN),
        "author_association": "OWNER",
        "body": "Scheduled review complete.\n" + markers,
        "created_at": "2026-01-02T04:00:00Z",
        "updated_at": "2026-01-02T04:00:00Z",
    }


#: What ``gh run`` gets under the merge-gate workflow's token, which carries no
#: ``actions: read`` (``.github/workflows/merge-gate.yml`` permissions; the guard's
#: ``_main_ci_red_note`` docstring records that its NOTE is local-only for this reason).
_NO_ACTIONS_READ = {"__gh_error__": "HTTP 403: Resource not accessible by integration"}

_PIN_FILE = "scripts/lib/cc_version.sh"


def _pin_contents() -> dict:
    """The contents API's answer for the CC pin file: the same synthetic pin at head
    and on the base, so the PR does not move it."""
    text = 'CC_VERSION="${CC_VERSION:-0.0.1}"\n'
    return {
        "type": "file",
        "encoding": "base64",
        "path": _PIN_FILE,
        "sha": "5" * 40,
        "content": base64.b64encode(text.encode()).decode(),
    }


def _owner_pr_facts() -> dict:
    """The outside-contribution read for a PR the owner opened from a branch here."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": HEAD,
                    "isCrossRepository": False,
                    "authorAssociation": "OWNER",
                    "author": {"__typename": "User", "login": OWNER_LOGIN},
                    "userContentEdits": {"totalCount": 0, "nodes": []},
                    "timelineItems": {"filteredCount": 0, "nodes": []},
                    "forcePushes": {"filteredCount": 0},
                }
            }
        }
    }


def _green_state() -> dict:
    """A PR every gate passes: green CI, a Codex review at head, the scheduled markers."""
    return {
        "repo": REPO,
        "pr": PR,
        "pr_view": {
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [_check_run("test", "SUCCESS", 5001)],
            "headRefOid": HEAD,
            "baseRefName": "main",
            "body": "Adds an example.\n\nE2E: none — synthetic fixture, no runtime surface\n",
            "createdAt": "2099-01-01T00:00:00Z",
        },
        "repo_view": {"defaultBranchRef": {"name": "main"}, "nameWithOwner": REPO},
        "pr_list": {"needs-rework": [], "needs-architecture-session": []},
        # The workflow's token has no `actions: read`, so every run read is refused.
        "run_list": {
            f"--commit={HEAD} --workflow=CI --event=pull_request": _NO_ACTIONS_READ,
            "--branch=main --workflow=CI --event=push": _NO_ACTIONS_READ,
        },
        "run_view": {},
        # The owner opened it from a branch of the repository: not an outside
        # contribution, so no review requirement is waived for it.
        "graphql": {"outside_pr": _owner_pr_facts()},
        "api": {
            f"issues/{PR}/comments": [_scheduled_comment(HEAD)],
            f"pulls/{PR}/comments": [],
            f"pulls/{PR}/reviews": [_codex_review(HEAD)],
            f"pulls/{PR}/commits": [
                {
                    "sha": HEAD,
                    "parents": [{"sha": BASE_TIP}],
                    "author": {"login": OWNER_LOGIN},
                    "committer": {"login": OWNER_LOGIN},
                    "commit": {"message": "feat: add an example"},
                }
            ],
            f"contents/{_PIN_FILE}?ref={HEAD}": _pin_contents(),
            f"contents/{_PIN_FILE}?ref=main": _pin_contents(),
            f"pulls/{PR}/files": [
                {
                    "filename": "src/example/module.py",
                    "previous_filename": None,
                    "status": "modified",
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# The harness: the workflow's environment, built from nothing.
# ---------------------------------------------------------------------------


def _child_env(home: Path, fakebin: Path, state: Path, log: Path) -> dict[str, str]:
    """What ``merge-gate.yml``'s "Run the merge gate" step gives the script, and no more.

    Synthetic values for the Actions identity the runner sets; no ``GH_TOKEN`` (the fake
    needs none, and a real token must not ride along). ``HOME`` is empty so no
    install's ``~/.genesis`` config is read: the runner has none either."""
    return {
        "PATH": os.pathsep.join([str(fakebin), os.environ.get("PATH", "/usr/bin:/bin")]),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_WORKFLOW": "merge-gate",
        "GITHUB_JOB": "genesis-merge-gate",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": REPO,
        "_TEST_CANONICAL_PUBLIC_REPO": REPO,
        STATE_ENV: str(state),
        LOG_ENV: str(log),
    }


def _run_report(tmp_path: Path, state: dict) -> tuple[subprocess.CompletedProcess, list[dict]]:
    home = tmp_path / "home"
    fakebin = tmp_path / "bin"
    home.mkdir()
    fakebin.mkdir()
    shim = fakebin / "gh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE_GH}" "$@"\n')
    shim.chmod(0o755)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    log = tmp_path / "gh-calls.jsonl"
    log.touch()
    proc = subprocess.run(
        [sys.executable, str(_GUARD), "--check-pr", state["pr"], "--repo", state["repo"]],
        cwd=str(_ROOT),
        env=_child_env(home, fakebin, state_file, log),
        capture_output=True,
        text=True,
        # No bound of its own: a hung child is bounded by the run that holds it, as every
        # test here is: pyproject's per-test pytest-timeout (`timeout = 600`, thread
        # method, wherever the plugin is installed), the corpus sweep's per-run
        # --timeout, and CI's job timeout.
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return proc, calls


def _rows(stdout: str) -> dict[str, str]:
    """``label : value`` per report row; the indented detail lines are skipped."""
    rows = {}
    for line in stdout.splitlines():
        if line[:1].isspace() or ":" not in line:
            continue
        label, _, value = line.partition(":")
        rows[label.strip()] = value.strip()
    return rows


def _assert_all_routed(proc: subprocess.CompletedProcess, calls: list[dict]) -> None:
    unrouted = [c["argv"] for c in calls if not c["routed"]]
    assert not unrouted, f"the report made gh reads no route answers: {unrouted}"
    assert UNROUTED not in proc.stdout + proc.stderr
    assert calls, "the report made no gh call at all, so the fake gh was not the one it ran"
    assert "Traceback" not in proc.stderr, proc.stderr


def _report(
    tmp_path: Path, state: dict
) -> tuple[subprocess.CompletedProcess, dict[str, str], list]:
    proc, calls = _run_report(tmp_path, state)
    _assert_all_routed(proc, calls)
    return proc, _rows(proc.stdout), calls


def _codex_inline(
    severity: str = "P1",
    *,
    comment_id: int = 9101,
    path: str = "src/example/module.py",
    review_id: int = 7001,
) -> dict:
    """An inline Codex finding on a file the PR changes, in the badge form Codex posts."""
    return {
        "id": comment_id,
        "in_reply_to_id": None,
        "pull_request_review_id": review_id,
        "user": _user(CODEX, "Bot"),
        "author_association": "NONE",
        "path": path,
        "line": 12,
        "body": (
            f"**<sub><sub>![{severity} Badge](https://img.shields.io/badge/{severity}-orange"
            f"?style=flat)</sub></sub>  Off-by-one drops the last row**\n\n"
            f"The loop stops one short."
        ),
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T03:04:05Z",
    }


def _stale_review_state() -> dict:
    """Codex reviewed an older commit, and the branch changed code since."""
    state = _green_state()
    state["api"][f"pulls/{PR}/reviews"] = [_codex_review(STALE)]
    state["graphql"]["codex_signal"] = _codex_thread([])
    state["graphql"]["base_ref"] = {"data": {"repository": {"ref": {"target": {"oid": BASE_TIP}}}}}
    state["api"][f"compare/{STALE}...{HEAD}"] = _compare(STALE, "6" * 40, 60)
    # The branch's own contribution moved between the two heads: the base did not
    # merely advance under a reviewed change.
    state["api"][f"compare/{BASE_TIP}...{STALE}"] = _compare(BASE_TIP, "7" * 40, 4)
    state["api"][f"compare/{BASE_TIP}...{HEAD}"] = _compare(BASE_TIP, "8" * 40, 64)
    return state


def _compare(merge_base: str, blob: str, added: int) -> dict:
    """A compare-API answer: one changed code file."""
    return {
        "status": "ahead",
        "merge_base_commit": {"sha": merge_base},
        "files": [
            {
                "filename": "src/example/module.py",
                "sha": blob,
                "additions": added,
                "deletions": 0,
                "status": "modified",
                "patch": "@@ -1 +1 @@\n+x = 1\n",
            }
        ],
    }


def _codex_thread(bodies: list[str]) -> dict:
    """The clean-signal GraphQL read: Codex's comments, unedited, history unmoved."""
    nodes = [
        {
            "body": body,
            "author": {"login": CODEX.removesuffix("[bot]"), "__typename": "Bot"},
            "editor": None,
            "userContentEdits": {"totalCount": 0, "nodes": []},
        }
        for body in bodies
    ]
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "comments": {"totalCount": len(nodes), "nodes": nodes},
                    "timelineItems": {"filteredCount": 0, "nodes": []},
                }
            }
        }
    }


def test_all_green_reports_mergeable_and_exits_zero(tmp_path):
    proc, rows, _ = _report(tmp_path, _green_state())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rows["verdict"] == "MERGEABLE (all gates pass)"
    assert rows["mergeable"] == "MERGEABLE"
    assert rows["ci"] == "green"
    assert rows["base-branch"] == "ok (default)"
    assert rows["codex-at-head"].startswith("ok ("), rows["codex-at-head"]
    assert rows["scheduled-claude"] == "ok (at head)"
    assert rows["review-body"] == "ok"
    assert rows["inline-findings"].startswith("ok"), rows["inline-findings"]
    assert rows["rework"].startswith("n/a"), rows["rework"]
    assert f"--match-head-commit {HEAD}" in rows["merge-with"]


def test_a_codex_p1_inline_finding_blocks(tmp_path):
    """A lone P1 on ordinary code scores under the lane's threshold, so only the
    always-fix floor stops it; and nobody answered it, so no reply clears it."""
    state = _green_state()
    state["api"][f"pulls/{PR}/comments"] = [_codex_inline("P1")]
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 1, proc.stdout
    inline = rows["inline-findings"]
    # The reason, not just the verdict: an unreadable scan also prints BLOCK.
    assert inline.startswith("BLOCK — always-fix floor: 1 unresolved [P1]"), inline
    assert "could not read" not in inline, inline
    assert rows["verdict"] == "1 gate(s) would block"
    assert "merge-with" not in rows


def test_a_maintainer_answered_p1_passes(tmp_path):
    """The control for the test above: the same P1, answered by the owner in its own
    review. The inline comments reach the guard's jq filter non-empty and must come
    out readable, with the reply's ``in_reply_to_id`` read as the P1 it answers."""
    state = _green_state()
    reply = {
        "id": 9102,
        "in_reply_to_id": 9101,
        "pull_request_review_id": 7101,
        "user": _user(OWNER_LOGIN),
        "author_association": "OWNER",
        "path": "src/example/module.py",
        "line": 12,
        "body": "Fixed.",
        "created_at": "2026-01-02T05:00:00Z",
        "updated_at": "2026-01-02T05:00:00Z",
    }
    owner_review = {
        **_codex_review(HEAD, review_id=7101),
        "user": _user(OWNER_LOGIN),
        "author_association": "OWNER",
        "submitted_at": "2026-01-02T05:00:00Z",
    }
    state["api"][f"pulls/{PR}/comments"] = [_codex_inline("P1"), reply]
    state["api"][f"pulls/{PR}/reviews"].append(owner_review)
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rows["inline-findings"].startswith("ok"), rows["inline-findings"]
    assert rows["verdict"] == "MERGEABLE (all gates pass)"


def test_two_codex_p2s_block_a_critical_change(tmp_path):
    """The critical lane's threshold is two P2s, on a change to the gate's own surface."""
    state = _green_state()
    workflow = ".github/workflows/example.yml"
    state["api"][f"pulls/{PR}/files"] = [
        {"filename": workflow, "previous_filename": None, "status": "modified"}
    ]
    state["api"][f"pulls/{PR}/comments"] = [
        _codex_inline("P2", comment_id=9201, path=workflow),
        _codex_inline("P2", comment_id=9202, path=workflow),
    ]
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 1, proc.stdout
    inline = rows["inline-findings"]
    assert inline.startswith(
        "BLOCK — review score 1.0 >= 1.0 blocks this CRITICAL change: 2 unresolved [P2]"
    ), inline
    assert "could not read" not in inline, inline
    assert rows["verdict"] == "1 gate(s) would block"


def test_a_file_renamed_out_of_the_hook_surface_runs_the_gate_lane(tmp_path):
    """GraphQL names only the new path; ``RENAMED`` sends the evaluator back to REST
    for the previous one, and that previous path under ``scripts/hooks/`` is what
    puts the change in the gate lane."""
    state = _green_state()
    state["api"][f"pulls/{PR}/files"] = [
        {
            "filename": "scripts/example_moved.py",
            "previous_filename": "scripts/hooks/example_moved.py",
            "status": "renamed",
        }
    ]
    proc, rows, calls = _report(tmp_path, state)
    # The budget evaluator's own REST fallback; the guard reads REST files elsewhere
    # whatever GraphQL says, so only this jq tells the two apart.
    budget_jq = ".[] | {filename: .filename, previous_filename: .previous_filename}"
    assert any(c["argv"][-1] == budget_jq for c in calls), (
        "the rename never sent the evaluator back to REST for the previous path"
    )
    assert rows["rounds"].startswith("1 (gate lane, terminal at 2)"), proc.stdout


def test_red_ci_blocks(tmp_path):
    state = _green_state()
    state["pr_view"]["statusCheckRollup"] = [
        _check_run("test", "SUCCESS", 5001),
        _check_run("lint", "FAILURE", 5001),
    ]
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 1, proc.stdout
    assert rows["ci"] == "red (lint)"
    assert rows["verdict"] == "1 gate(s) would block"
    assert "merge-with" not in rows


def test_a_conclusion_github_adds_later_reads_red(tmp_path):
    """A completed check with a conclusion the gate has never heard of is not a pass."""
    state = _green_state()
    state["pr_view"]["statusCheckRollup"] = [
        _check_run("test", "SUCCESS", 5001),
        _check_run("lint", "NEWLY_INVENTED", 5001),
    ]
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 1, proc.stdout
    assert rows["ci"] == "red (lint)"
    assert rows["verdict"] == "1 gate(s) would block"


def test_a_codex_review_at_a_stale_head_blocks(tmp_path):
    proc, rows, _ = _report(tmp_path, _stale_review_state())
    assert proc.returncode == 1, proc.stdout
    assert rows["codex-at-head"].startswith("BLOCK"), rows["codex-at-head"]
    assert rows["verdict"] == "1 gate(s) would block"
    assert "merge-with" not in rows


def _clean_signal(short: str) -> str:
    return f"Codex Review: Didn't find any major issues.\n\nReviewed commit: `{short}`"


def test_a_clean_signal_naming_the_head_by_prefix_passes(tmp_path):
    """The repository resolves the abbreviated id to this head, so Codex vouched for it."""
    state = _stale_review_state()
    state["graphql"]["codex_signal"] = _codex_thread([_clean_signal(HEAD[:7])])
    state["api"][f"commits/{HEAD[:7]}"] = {"sha": HEAD}
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rows["codex-at-head"].startswith("ok ("), rows["codex-at-head"]
    assert rows["verdict"] == "MERGEABLE (all gates pass)"


def test_a_clean_signal_whose_prefix_resolves_elsewhere_blocks(tmp_path):
    """The same prefix, but the repository resolves it to ANOTHER commit: an older
    review sharing the prefix must not read as a review of this head."""
    state = _stale_review_state()
    state["graphql"]["codex_signal"] = _codex_thread([_clean_signal(HEAD[:7])])
    state["api"][f"commits/{HEAD[:7]}"] = {"sha": HEAD[:7] + "f" * 33}
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 1, proc.stdout
    assert rows["codex-at-head"].startswith("BLOCK"), rows["codex-at-head"]
    assert rows["verdict"] == "1 gate(s) would block"


def _code_review_only(head: str) -> dict:
    comment = _scheduled_comment(head)
    comment["id"] = 8002
    comment["body"] = (
        f"Scheduled review complete.\n<!-- genesis-scheduled-review: head={head} kind=code-review -->"
    )
    return comment


def test_a_missing_leaks_marker_blocks_an_owner_pr(tmp_path):
    """Only the code-review marker is posted. The PR is the owner's own, not an
    outside contribution, so nothing exempts it from the leaks review."""
    state = _green_state()
    state["api"][f"issues/{PR}/comments"] = [_code_review_only(HEAD)]
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 1, proc.stdout
    assert rows["scheduled-claude"].startswith("BLOCK"), rows["scheduled-claude"]
    assert "leaks" in rows["scheduled-claude"], rows["scheduled-claude"]
    assert rows["verdict"] == "1 gate(s) would block"


def test_a_leaks_review_of_an_ancestor_carries_when_the_scan_is_green(tmp_path):
    """The leaks review was accepted at an earlier commit of this branch, and CI's
    leak-detector is green at the head: the review carries, and the row says so."""
    state = _green_state()
    state["api"][f"issues/{PR}/comments"] = [_scheduled_comment(STALE), _code_review_only(HEAD)]
    state["api"][f"compare/{STALE}...{HEAD}"] = {"status": "ahead"}
    state["pr_view"]["statusCheckRollup"] = [
        _check_run("test", "SUCCESS", 5001),
        _check_run("leak-detector", "SUCCESS", 5001),
    ]
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rows["scheduled-claude"] == (
        f"ok (leaks carried from {STALE[:12]}, leak-detector green at head)"
    )


def test_a_rebuild_of_a_sent_back_pr_without_its_contract_blocks(tmp_path):
    """One replaced PR is on the sent-back list now; the other was sent back once, per
    its timeline. Neither carries a spec or an acknowledgement, and the body has no
    ``## Rework`` section."""
    state = _green_state()
    state["pr_view"]["body"] = (
        "Rebuilds the example.\n\nReplaces: #4100\nSupersedes: #4101\n\n"
        "E2E: none — synthetic fixture, no runtime surface\n"
    )
    state["pr_list"]["needs-rework"] = [{"number": 4100, "headRefOid": "c" * 40, "state": "CLOSED"}]
    state["api"]["pulls/4101"] = {"state": "closed", "merged_at": None}
    state["api"]["issues/4101/timeline"] = [
        {"event": "labeled", "label": {"name": "needs-architecture-session"}},
        {"event": "unlabeled", "label": {"name": "needs-architecture-session"}},
    ]
    state["api"]["issues/4100/comments"] = []
    state["api"]["issues/4101/comments"] = []
    proc, rows, _ = _report(tmp_path, state)
    assert proc.returncode == 1, proc.stdout
    assert rows["rework"].startswith("BLOCK — rebuild of sent-back PR(s) #4100, #4101"), rows[
        "rework"
    ]
    assert rows["verdict"] == "1 gate(s) would block"


def _gh_callers(path: Path, start: str) -> set[str]:
    """Functions reachable from ``start`` by name that hold an argv literal beginning
    ``"gh"``, each credited to its innermost enclosing function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(node)
    holders: set[str] = set()

    def visit(node: ast.AST, owner: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if (
            owner
            and isinstance(node, (ast.List, ast.Tuple))
            and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == "gh"
        ):
            holders.add(owner)
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, None)
    seen: set[str] = set()
    stack = [start]
    while stack:
        name = stack.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for fn in funcs[name]:
            for node in ast.walk(fn):
                ref = (
                    node.id
                    if isinstance(node, ast.Name)
                    else node.attr
                    if isinstance(node, ast.Attribute)
                    else None
                )
                if ref in funcs:
                    stack.append(ref)
    return holders & seen


def test_every_reachable_gh_caller_is_known():
    """A new ``gh`` read behind the report must get a route here, or this fails by name."""
    assert _gh_callers(_GUARD, "check_pr_report") == set(GH_CALLERS)
    source = _GUARD.read_text(encoding="utf-8")
    assert "_review_budget.evaluate_pr(" in source, "the rounds row's evaluator moved"
    assert _gh_callers(_REVIEW_BUDGET, "evaluate_pr") == set(REVIEW_BUDGET_GH)


def test_no_other_sibling_module_the_guard_imports_runs_gh():
    """The walk above follows names defined in the guard. Its sibling modules are
    reached through imports, so each one that can start a process is checked too."""
    tree = ast.parse(_GUARD.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    runs_gh = set()
    for name in names:
        for home in (_GUARD.parent, _ROOT / "scripts"):
            path = home / f"{name}.py"
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                if "import subprocess" in text and _gh_holders(path):
                    runs_gh.add(name)
    assert runs_gh == {"review_budget"}


def _gh_holders(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, (ast.List, ast.Tuple))
            and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == "gh"
        ):
            return True
    return False


def _fake_gh(tmp_path: Path, argv: list[str]) -> subprocess.CompletedProcess:
    state = tmp_path / "state.json"
    state.write_text(json.dumps(_green_state()), encoding="utf-8")
    env = {**os.environ, STATE_ENV: str(state), LOG_ENV: str(tmp_path / "log.jsonl")}
    return subprocess.run(
        [sys.executable, str(_FAKE_GH), *argv], env=env, capture_output=True, text=True
    )


_COMMENTS = f"repos/{REPO}/issues/{PR}/comments"
_OWNER, _NAME = REPO.split("/")
_VARS = ["-f", f"owner={_OWNER}", "-f", f"name={_NAME}", "-F", f"number={PR}"]
_BUDGET_DOC = GRAPHQL_DOCUMENTS["review_budget"]
#: A graphql read the fake has a recorded answer for, so only the method can refuse it.
_GRAPHQL_READ = ["-f", f"query={_BUDGET_DOC}", *_VARS]


@pytest.mark.parametrize(
    "argv",
    [
        # No field that would change the recorded key: only the method can refuse these.
        ["api", "-X", "POST", _COMMENTS],
        ["api", _COMMENTS, "-f", "per_page=100"],
        ["api", "-X", "PATCH", _COMMENTS],
        ["pr", "merge", PR, "--repo", REPO, "--squash"],
        ["pr", "comment", PR, "--repo", REPO, "--body", "x"],
        ["api", "graphql", "-X", "DELETE", *_GRAPHQL_READ],
        ["api", "graphql", "-X", "PATCH", *_GRAPHQL_READ],
    ],
    ids=[
        "post",
        "fields-without-get",
        "patch",
        "pr-merge",
        "pr-comment",
        "graphql-delete",
        "graphql-patch",
    ],
)
def test_the_fake_refuses_every_write(tmp_path, argv):
    """A write the gate starts must fail loudly, never get a recorded answer: the
    endpoint above has one for GET, so only the method keeps this from answering."""
    proc = _fake_gh(tmp_path, argv)
    assert proc.returncode == 97, proc.stdout
    assert UNROUTED in proc.stderr


def test_the_same_endpoint_read_with_get_is_answered(tmp_path):
    """The control for the refusals above: the endpoint itself is routed."""
    proc = _fake_gh(tmp_path, ["api", "-X", "GET", _COMMENTS])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)


@pytest.mark.parametrize("method", [[], ["-X", "POST"]], ids=["default", "post"])
def test_the_same_graphql_read_is_answered(tmp_path, method):
    """The control for the graphql refusals above: the query itself is routed."""
    proc = _fake_gh(tmp_path, ["api", "graphql", *method, *_GRAPHQL_READ])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["data"]["repository"]["pullRequest"]["headRefOid"] == HEAD


@pytest.mark.parametrize(
    "document",
    [
        "mutation" + _BUDGET_DOC.removeprefix("query"),
        "subscription" + _BUDGET_DOC.removeprefix("query"),
        _BUDGET_DOC.replace("headRefOid body", "headRefOid body title", 1),
        _BUDGET_DOC.replace(" changeType", "", 1),
        _BUDGET_DOC + " # pullRequest(number: $number) { headRefOid body",
        "mutation($owner: String!) { addComment { clientMutationId } }"
        " # pullRequest(number: $number) { headRefOid body",
    ],
    ids=["mutation", "subscription", "extra-field", "missing-field", "comment", "phrase"],
)
def test_the_fake_refuses_any_document_but_a_stored_one(tmp_path, document):
    """The variables match the stored review_budget read, which has a recorded
    answer, so only the exact-document match refuses these."""
    assert document != _BUDGET_DOC, "the edit did not change the stored document"
    proc = _fake_gh(tmp_path, ["api", "graphql", "-f", f"query={document}", *_VARS])
    assert proc.returncode == 97, proc.stdout
    assert "is not a stored gate query" in proc.stderr, proc.stderr


def test_a_stored_document_is_matched_after_whitespace_normalization(tmp_path):
    """The control: the same text reflowed onto indented lines is the same document."""
    spaced = _BUDGET_DOC.replace(" ", "\n  ")
    proc = _fake_gh(tmp_path, ["api", "graphql", "-f", f"query={spaced}", *_VARS])
    assert proc.returncode == 0, proc.stderr


def test_a_no_break_space_is_not_graphql_whitespace(tmp_path):
    """GraphQL whitespace is space, tab, CR and LF; a no-break space is part of a
    token, so a document spaced with one is a different document."""
    nbsp = _BUDGET_DOC.replace(" ", " ")
    proc = _fake_gh(tmp_path, ["api", "graphql", "-f", f"query={nbsp}", *_VARS])
    assert proc.returncode == 97, proc.stdout
    assert "is not a stored gate query" in proc.stderr, proc.stderr


@pytest.mark.parametrize(
    "route,variables",
    [
        ("review_budget", ["-f", f"owner={_OWNER}", "-f", f"name={_NAME}", "-f", f"number={PR}"]),
        ("outside_pr", ["-f", f"owner={_OWNER}", "-f", f"name={_NAME}", "-f", f"num={PR}"]),
        ("codex_signal", ["-F", f"owner={_OWNER}", "-f", f"name={_NAME}", "-F", f"number={PR}"]),
    ],
    ids=["number-as-string", "num-as-string", "name-by-other-flag"],
)
def test_a_variable_sent_by_another_flag_is_refused(tmp_path, route, variables):
    """``-f`` sends a raw string, ``-F`` a coerced value; an ``Int!`` sent with ``-f``
    is not the read the gate makes, so it is not answered as if GitHub had."""
    doc = GRAPHQL_DOCUMENTS[route]
    proc = _fake_gh(tmp_path, ["api", "graphql", "-f", f"query={doc}", *variables])
    assert proc.returncode == 97, proc.stdout
    assert "variables by flag" in proc.stderr, proc.stderr


def test_graphql_paginate_is_refused(tmp_path):
    proc = _fake_gh(tmp_path, ["api", "graphql", "--paginate", *_GRAPHQL_READ])
    assert proc.returncode == 97, proc.stdout
    assert "--paginate" in proc.stderr, proc.stderr


def test_the_review_budget_answer_carries_only_what_the_document_selects(tmp_path):
    doc = GRAPHQL_DOCUMENTS["review_budget_reread"]
    proc = _fake_gh(tmp_path, ["api", "graphql", "-f", f"query={doc}", *_VARS])
    assert proc.returncode == 0, proc.stderr
    pull = json.loads(proc.stdout)["data"]["repository"]["pullRequest"]
    assert set(pull) == {"headRefOid", "body", *REVIEW_BUDGET_READS["review_budget_reread"]}
    assert "files" not in pull


def _module_strings(path: Path) -> tuple[dict[str, str], ast.Module]:
    """Module-level string constants of ``path``, read as text and never imported:
    the corpus mutates these files, and an import would hand the test the mutant."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
                continue  # not a literal: no stored document comes from it
    return values, tree


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"{name} is no longer one top-level function"
    return found[0]


def _live_graphql_documents() -> dict[str, str]:
    """Each stored document's live counterpart, rebuilt from the source text."""
    guard, guard_tree = _module_strings(_GUARD)
    base_ref = [
        ast.literal_eval(n.value)
        for n in ast.walk(_function(guard_tree, "_pr_base_sha"))
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "query"
    ]
    assert len(base_ref) == 1, "_pr_base_sha no longer assigns one `query` literal"
    budget, budget_tree = _module_strings(_REVIEW_BUDGET)
    # The composer runs from its own source text in a namespace holding only the
    # connection table it reads, so no other review_budget code executes.
    namespace = {"_GRAPHQL_CONNECTIONS": budget["_GRAPHQL_CONNECTIONS"]}
    composer = ast.Module([_function(budget_tree, "_graphql_query")], type_ignores=[])
    code = compile(
        composer,
        str(_REVIEW_BUDGET),
        "exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )
    exec(code, namespace)  # noqa: S102 - the repo's own composer, run from its source text
    live = {
        "base_ref": base_ref[0],
        "codex_signal": guard["_CODEX_SIGNAL_QUERY"],
        "outside_pr": guard["_OUTSIDE_PR_QUERY"],
    }
    for name, connections in _live_review_budget_reads(budget_tree).items():
        live[name] = namespace["_graphql_query"](list(connections))
    return live


def _live_review_budget_reads(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """The connections each snapshot in ``_evaluate_pr_inner`` reads when no seam is
    set, as the server runs it: the ``seams`` keys in order, then the ``mutable``
    re-read's tuple. Read from the source, so the fake's table is checked against
    the evaluator and not against itself."""
    inner = _function(tree, "_evaluate_pr_inner")
    seams = mutable = None
    for node in ast.walk(inner):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        target = node.targets[0].id
        if target == "seams" and isinstance(node.value, ast.Dict):
            seams = tuple(ast.literal_eval(k) for k in node.value.keys)
        elif target == "mutable" and isinstance(node.value, ast.ListComp):
            mutable = ast.literal_eval(node.value.generators[0].iter)
    assert seams and mutable, "_evaluate_pr_inner no longer names `seams` and `mutable`"
    return {"review_budget": seams, "review_budget_reread": tuple(mutable)}


def test_the_stored_review_budget_reads_match_the_evaluator():
    live = _live_review_budget_reads(ast.parse(_REVIEW_BUDGET.read_text(encoding="utf-8")))
    assert live == REVIEW_BUDGET_READS, (
        f"review_budget now reads {live}; re-record REVIEW_BUDGET_READS and the matching "
        "GRAPHQL_DOCUMENTS in tests/test_hooks/fake_gh_replay.py"
    )


def test_every_stored_graphql_document_matches_the_live_query():
    """A gate query that really changed fails HERE, by name, with the instruction to
    re-record; every replay test would otherwise fail as a bare UNROUTED."""
    live = _live_graphql_documents()
    assert set(live) == set(GRAPHQL_DOCUMENTS)
    stale = [
        name
        for name, document in GRAPHQL_DOCUMENTS.items()
        if _normalize(document) != _normalize(live[name])
    ]
    assert not stale, (
        f"the stored GraphQL document(s) {stale} no longer match the live gate query; "
        "if the query change is intended, re-record each one in "
        "tests/test_hooks/fake_gh_replay.py GRAPHQL_DOCUMENTS from its live source "
        "(whitespace-normalized), and update REVIEW_BUDGET_READS if the connections moved"
    )


def test_the_child_environment_carries_nothing_from_the_test_process(tmp_path, monkeypatch):
    """The hook conftest pins a dozen ``_TEST_*`` seams in this process; none may reach
    the child, and neither may a token or an outer job's identity."""
    monkeypatch.setenv("GH_TOKEN", "not-a-real-token")
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    env = _child_env(tmp_path, tmp_path, tmp_path / "s", tmp_path / "l")
    seams = {k for k in env if k.startswith("_TEST_")}
    assert seams == {"_TEST_CANONICAL_PUBLIC_REPO"}
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_RUN_ID"} & set(env)
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path)


def test_jq_is_available_to_the_fake():
    """The fake applies the caller's ``--jq`` with the real binary. Without it every
    filtered read fails loudly; this names the cause instead."""
    assert shutil.which("jq"), "the server replay needs a jq binary on PATH"
