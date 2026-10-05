"""A pull-request merge spelled through the GitHub API is refused into the gate (#2768).

The push guard gates a merge only when it is spelled `gh pr merge`. The same merge
sent through `gh api` (REST `pulls/N/merge`, `merge-async`, or a GraphQL merge
mutation) used to pass silently, so every merge-gate check was skipped on that
route. The fix refuses the SPELLING, not the merge: the refusal names the gated
command, so a clean PR still merges, through the one parser that runs the gates.

Three properties, each pinned below through the REAL hook as a subprocess:

1. Every merge spelling is refused, in the foreground and in a dispatched session
   alike (no ask: an ask with nobody present is a block nobody intended).
2. Reads stay silent: a GET of `pulls/N/merge` (the "is it merged?" check), the
   `merge-async/{uuid}` status read, GraphQL queries, the two mutations that UNDO
   a merge request, ordinary review replies, and `gh pr merge --help`.
3. The refusal is decided before the guard runs any subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _HOOKS / "git_push_guard.py"

sys.path.insert(0, str(_HOOKS))

#: The text the refusal must carry, so a session knows the route it was refused on.
_API_MARK = "through the GitHub API"

_R = "repos/octo/demo"
_URL = "https://api.github.com/repos/octo/demo"
_MUT = 'mutation { mergePullRequest(input:{pullRequestId:\\"x\\"}) { clientMutationId } }'


def _run(command: str, *, dispatched: bool, cwd: str | None = None, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("GENESIS_CC_SESSION",)}
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    env.update(env_extra or {})
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if cwd is not None:
        payload["cwd"] = cwd
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=cwd,
    )


REFUSED = [
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge -f merge_method=squash", "5", id="rest-X-PUT"),
    pytest.param(f"gh api -XPUT {_R}/pulls/5/merge", "5", id="rest-glued-X"),
    pytest.param(f"gh api --method=put {_R}/pulls/5/merge", "5", id="rest-method-eq-lower"),
    pytest.param(f"gh api --method PUT {_R}/pulls/5/merge", "5", id="rest-method-space"),
    pytest.param(f"gh api {_R}/pulls/5/merge -X PUT", "5", id="rest-method-after-endpoint"),
    pytest.param(f"gh -X PUT api {_R}/pulls/5/merge", "5", id="rest-method-before-group"),
    pytest.param(f"gh api -X POST {_R}/pulls/5/merge", "5", id="rest-other-write-method"),
    pytest.param(
        f"gh api {_R}/pulls/5/merge -f merge_method=squash", "5", id="rest-field-implied-post"
    ),
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge-async -f sha=abc", "5", id="rest-merge-async"),
    pytest.param(f"gh api -X PUT /{_R}/pulls/5/merge", "5", id="rest-leading-slash"),
    pytest.param(f"gh api -X PUT {_URL}/pulls/5/merge", "5", id="rest-full-url"),
    pytest.param(
        "gh api -X PUT https://ghe.example/api/v3/repos/octo/demo/pulls/5/merge",
        "5",
        id="rest-enterprise-url",
    ),
    pytest.param("gh api -X PUT 'repos/{owner}/{repo}/pulls/5/merge'", "5", id="rest-placeholders"),
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge/", "5", id="rest-trailing-slash"),
    pytest.param(f"gh api -X PUT '{_R}/pulls/5/merge?x=1'", "5", id="rest-query-string"),
    pytest.param("gh api -X PUT $ENDPOINT", None, id="rest-put-variable-endpoint"),
    pytest.param(f'bash -c "gh api -X PUT {_R}/pulls/5/merge"', "5", id="bash-c-carrier"),
    pytest.param(f"gh api graphql -f query='{_MUT}'", None, id="graphql-inline"),
    pytest.param(f"gh api /graphql -f query='{_MUT}'", None, id="graphql-leading-slash"),
    pytest.param(
        f"gh api https://api.github.com/graphql -f query='{_MUT}'", None, id="graphql-full-url"
    ),
    pytest.param(
        "gh api graphql -f query='mutation { m: mergePullRequest(input:{}) { clientMutationId } }'",
        None,
        id="graphql-alias",
    ),
    pytest.param(
        "gh api graphql -f query='mutation M { ...F } fragment F on Mutation "
        "{ enablePullRequestAutoMerge(input:{}) { clientMutationId } }'",
        None,
        id="graphql-fragment-spread",
    ),
    pytest.param(
        "gh api graphql -f query='mutation { enqueuePullRequest(input:{}) { clientMutationId } }'",
        None,
        id="graphql-enqueue",
    ),
    pytest.param("gh api graphql -F query=@-", None, id="graphql-query-stdin"),
    pytest.param("gh api graphql --input -", None, id="graphql-input-stdin"),
    pytest.param('gh api graphql -f query="$Q"', None, id="graphql-query-variable"),
    pytest.param(
        "gh api graphql -F query=@/nonexistent/q.graphql", None, id="graphql-missing-file"
    ),
    # pflag reads `--help=false` / `--help=0` as help OFF, so the command runs.
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge --help=false", "5", id="help-false"),
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge --help=0", "5", id="help-zero"),
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge --help --help=false", "5", id="help-last-wins"),
    # Shell expansions the guard cannot see through.
    pytest.param("gh api -X PUT repos/$R/pulls/5/merge", None, id="rest-put-expanded-endpoint"),
    pytest.param('gh api -X PUT "repos/${R}/pulls/5/merge"', None, id="rest-put-braced-endpoint"),
    pytest.param('gh api -X "$M" "$EP"', None, id="rest-variable-method-and-endpoint"),
    pytest.param(
        'gh api graphql -f query="mutation { $(cat m.graphql) }"', None, id="graphql-substitution"
    ),
    # xargs appends words after the guard has judged the command.
    pytest.param(
        f"echo {_R}/pulls/5/merge | xargs gh api -X PUT", None, id="xargs-appends-endpoint"
    ),
    pytest.param(f"echo -X PUT | xargs gh api {_R}/pulls/5/merge", "5", id="xargs-appends-method"),
    pytest.param("echo query=x | xargs gh api graphql -f", None, id="xargs-graphql"),
    pytest.param(f"echo {_R}/pulls/5 | xargs gh api", None, id="xargs-endpoint-unseen"),
    # Endpoint spellings that normalise to the merge path.
    pytest.param(
        f"gh api -X PUT HTTPS://api.github.com/{_R}/pulls/5/merge", "5", id="rest-upper-scheme"
    ),
    pytest.param(f"gh api -X PUT {_R}//pulls/5/./merge", "5", id="rest-dot-segments"),
    pytest.param(f"gh api -X PUT {_R}/pulls/5/%6Derge", "5", id="rest-percent-encoded"),
    pytest.param(f"gh api -X PUT {_R}/PULLS/5/MERGE", "5", id="rest-upper-path"),
    # A help flag on a merge spelling is not an exemption: gh may hand it to a value flag.
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge --help", "5", id="api-help-is-not-exempt"),
    # Short-flag groups: pflag hands the next word to the first value-taking letter.
    pytest.param(f"gh api -iXPUT {_R}/pulls/5/merge", "5", id="group-iXPUT"),
    pytest.param(f"gh api -iX PUT {_R}/pulls/5/merge", "5", id="group-iX-space"),
    pytest.param(f"gh api -X PUT {_R}/pulls/5/merge -iq --help", "5", id="group-iq-help"),
    pytest.param(
        "gh api -if query='mutation { mergePullRequest(input:{}) { clientMutationId } }' graphql",
        None,
        id="group-if-query",
    ),
    pytest.param(
        "gh api \"$EP\" -f query='mutation { mergePullRequest(input:{}) { clientMutationId } }'",
        None,
        id="variable-endpoint-inline-merge",
    ),
    pytest.param("gh api $EP -F query=@m.graphql", None, id="variable-endpoint-query-file"),
    pytest.param(f"gh api -X PUT {_R}/pull{{s..s}}/5/merge", None, id="brace-expanded-path"),
    pytest.param("gh api -X PUT repositories/123/pulls/5/merge", "5", id="repositories-alias"),
    pytest.param("gh api -iXPUT $EP", None, id="group-X-variable-endpoint"),
    pytest.param(
        "gh api graphql -F query=@q.graphql 2> >(cp m.graphql q.graphql)",
        None,
        id="process-substitution-race",
    ),
]


@pytest.mark.parametrize("dispatched", [False, True], ids=["foreground", "dispatched"])
@pytest.mark.parametrize(("command", "pr"), REFUSED)
def test_api_merge_spellings_are_refused(command, pr, dispatched):
    r = _run(command, dispatched=dispatched)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert _API_MARK in r.stderr, r.stderr
    assert "gh pr merge" in r.stderr and "--match-head-commit" in r.stderr
    if pr is not None:
        assert f"--check-pr {pr}" in r.stderr


SILENT = [
    pytest.param(f"gh api {_R}/pulls/5/merge", id="rest-get-is-merged"),
    pytest.param(f"gh api -X GET {_R}/pulls/5/merge", id="rest-explicit-get"),
    pytest.param(f"gh api {_R}/pulls/5/merge-async/0f1e2d", id="rest-merge-async-status"),
    pytest.param(f"gh api {_R}/pulls/5", id="rest-pr-read"),
    pytest.param(f"gh api -X PUT {_R}/pulls/5/update-branch", id="rest-update-branch"),
    pytest.param(f"gh api {_R}/pulls/5/comments/7/replies -f body=thanks", id="rest-review-reply"),
    pytest.param('gh api "$EP" -f body=thanks -F in_reply_to=7', id="rest-post-variable-endpoint"),
    pytest.param("gh api graphql -f query='{ viewer { login } }'", id="graphql-query"),
    pytest.param(
        "gh api graphql -f query='{ __type(name:\\\"Mutation\\\") { fields { name } } }' "
        "--jq '.data.__type.fields[] | select(.name==\\\"mergePullRequest\\\")'",
        id="graphql-introspection-naming-merge",
    ),
    pytest.param(
        "gh api graphql -f query='mutation { disablePullRequestAutoMerge(input:{}) { clientMutationId } }'",
        id="graphql-disable-auto-merge",
    ),
    pytest.param(
        "gh api graphql -f query='mutation { dequeuePullRequest(input:{}) { clientMutationId } }'",
        id="graphql-dequeue",
    ),
    pytest.param("gh pr merge --help", id="pr-merge-help"),
    pytest.param("gh pr merge -h", id="pr-merge-short-help"),
    pytest.param(f"echo 'gh api -X PUT {_R}/pulls/5/merge'", id="quoted-mention"),
    pytest.param(
        "gh api graphql -f query='query($n: Int!) { viewer { login } }' -F n=1",
        id="graphql-variables",
    ),
    pytest.param('gh api -X GET "$EP"', id="rest-get-variable-endpoint"),
    pytest.param(f"gh api {_R}/pulls/$(echo 5)", id="rest-read-with-substitution"),
    pytest.param(
        f"gh api {_R}/code-scanning/alerts -X GET -f ref=refs/pull/5/merge",
        id="get-with-merge-ref-field",
    ),
    pytest.param(
        'gh api graphql -f query=\'{ repository(owner:"$(gh repo view --json owner -q .owner.login)", '
        'name:"demo") { id } }\'',
        id="graphql-value-substitution",
    ),
    pytest.param(
        f"gh api {_R}/issues/5/comments -f body='do not call repos/o/r/pulls/5/merge'",
        id="comment-body-mentions-merge",
    ),
    pytest.param(
        'gh api -X PATCH "repos/$REPO/pulls/5" --input body.json',
        id="pr-body-patch-variable-endpoint",
    ),
]


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 5 --squash --help=false",
        "gh pr merge 5 --squash --help",
        "gh pr merge 5 --squash -sb --help",
        "gh pr merge 5 -sb --help",
        "gh pr merge --help=true",
    ],
)
def test_only_the_bare_help_form_skips_the_cli_merge_arm(command):
    """Help is exempt only as exactly `gh pr merge --help|-h`: with any other word,
    gh may run the merge (`-sb --help` is a squash merge whose body is `--help`)."""
    r = _run(command, dispatched=False)
    assert r.returncode == 2 and "without --admin" in r.stderr, r.stderr


@pytest.mark.parametrize("dispatched", [False, True], ids=["foreground", "dispatched"])
@pytest.mark.parametrize("command", SILENT)
def test_reads_and_help_stay_silent(command, dispatched):
    r = _run(command, dispatched=dispatched)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert _API_MARK not in r.stderr


def test_gh_pr_merge_without_admin_is_unchanged():
    """The existing arm still owns the CLI spelling, with its own message."""
    r = _run("gh pr merge 5 --squash", dispatched=False)
    assert r.returncode == 2
    assert "without --admin" in r.stderr
    assert _API_MARK not in r.stderr


def test_query_file_with_a_merge_is_refused_and_a_plain_one_is_not(tmp_path):
    """`-F query=@path` reads the file, relative to the command's cwd."""
    (tmp_path / "merge.graphql").write_text(
        'mutation { mergePullRequest(input: {pullRequestId: "x"}) { clientMutationId } }\n'
    )
    (tmp_path / "read.graphql").write_text("query { viewer { login } }\n")
    refused = _run("gh api graphql -F query=@merge.graphql", dispatched=False, cwd=str(tmp_path))
    assert refused.returncode == 2 and _API_MARK in refused.stderr, refused.stderr
    allowed = _run("gh api graphql -F query=@read.graphql", dispatched=False, cwd=str(tmp_path))
    assert allowed.returncode == 0 and _API_MARK not in allowed.stderr, allowed.stderr


def test_input_file_body_is_read(tmp_path):
    """`--input file` carries the whole JSON body, including the query."""
    (tmp_path / "body.json").write_text(
        json.dumps(
            {"query": "mutation { enablePullRequestAutoMerge(input: {}) { clientMutationId } }"}
        )
    )
    (tmp_path / "read.json").write_text(json.dumps({"query": "{ viewer { login } }"}))
    refused = _run("gh api graphql --input body.json", dispatched=True, cwd=str(tmp_path))
    assert refused.returncode == 2 and _API_MARK in refused.stderr, refused.stderr
    allowed = _run("gh api graphql --input read.json", dispatched=True, cwd=str(tmp_path))
    assert allowed.returncode == 0 and _API_MARK not in allowed.stderr, allowed.stderr


def test_a_query_file_another_segment_could_write_is_not_trusted(tmp_path):
    """Write-then-run in one command: the guard would read the file too early."""
    (tmp_path / "q.graphql").write_text("query { viewer { login } }\n")
    for command in (
        "echo 'mutation { mergePullRequest(input:{}) { clientMutationId } }' > q.graphql "
        "&& gh api graphql -F query=@q.graphql",
        "cp m.graphql q.graphql; gh api graphql -F query=@q.graphql",
        "true && gh api graphql --input q.graphql",
    ):
        r = _run(command, dispatched=False, cwd=str(tmp_path))
        assert r.returncode == 2 and _API_MARK in r.stderr, (command, r.stderr)


def test_a_relative_query_file_without_a_payload_cwd_is_refused(tmp_path):
    """No cwd in the payload: the guard does not guess the hook's own directory."""
    (tmp_path / "q.graphql").write_text("query { viewer { login } }\n")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh api graphql -F query=@q.graphql"},
    }
    r = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
    )
    assert r.returncode == 2 and _API_MARK in r.stderr, r.stderr


def test_a_fifo_query_file_is_refused_without_hanging(tmp_path):
    """A FIFO would block a plain read until the hook is killed, which fails open."""
    fifo = tmp_path / "q.graphql"
    os.mkfifo(fifo)
    r = _run(f"gh api graphql -F query=@{fifo}", dispatched=False)
    assert r.returncode == 2 and _API_MARK in r.stderr, r.stderr


def test_refusal_runs_no_subprocess_first(tmp_path):
    """The verdict is reached before the guard spawns anything.

    Fake `gh` and `git` executables first on PATH write a marker if invoked. A
    REST merge must be refused with the marker absent, and the guard must have
    really been able to reach them (guard-the-guard: the control call does).
    """
    marker = tmp_path / "spawned"
    for name in ("gh", "git"):
        exe = tmp_path / name
        exe.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\nexit 1\n")
        exe.chmod(0o755)
    env = {"PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    r = _run(f"gh api -X PUT {_R}/pulls/5/merge", dispatched=False, env_extra=env)
    assert r.returncode == 2 and _API_MARK in r.stderr, r.stderr
    assert not marker.exists(), marker.read_text()
    # Control: the CLI merge arm does call out, so the fakes are reachable.
    _run("gh pr merge 5 --squash --admin", dispatched=False, env_extra=env)
    assert marker.exists(), "the fake executables were never reachable; the test proves nothing"


class TestPredicate:
    """The pure predicate, argv in, reason out."""

    def _reason(self, argv, cwd=None):
        from gh_merge import api_merge_reason

        return api_merge_reason(argv, cwd)

    def test_non_gh_and_other_groups_are_none(self):
        assert self._reason(["git", "push"]) is None
        assert self._reason(["gh", "workflow", "run", "api", "-X", "PUT"]) is None
        assert self._reason(["gh", "pr", "merge", "5", "--admin"]) is None

    def test_rest_merge_names_the_pr(self):
        found = self._reason(["gh", "api", "-X", "PUT", f"{_R}/pulls/12/merge"])
        assert found is not None and found.pr == "12" and not found.unreadable

    def test_unreadable_is_flagged(self):
        found = self._reason(["gh", "api", "graphql", "--input", "-"])
        assert found is not None and found.unreadable

    def test_relative_query_file_without_a_known_cwd_is_unreadable(self, tmp_path):
        (tmp_path / "q.graphql").write_text("query { viewer { login } }")
        found = self._reason(["gh", "api", "graphql", "-F", "query=@q.graphql"], None)
        assert found is not None and found.unreadable
        assert (
            self._reason(["gh", "api", "graphql", "-F", "query=@q.graphql"], str(tmp_path)) is None
        )

    def test_oversized_query_file_is_unreadable(self, tmp_path):
        big = tmp_path / "big.graphql"
        big.write_text("query { viewer { login } }" + " " * (2 << 20))
        found = self._reason(["gh", "api", "graphql", "-F", f"query=@{big}"])
        assert found is not None and found.unreadable

    def test_a_fifo_with_data_waiting_is_still_unreadable(self, tmp_path):
        """Only regular files are read: a pipe another process feeds is never trusted."""
        from gh_merge import _read_text

        fifo = tmp_path / "q.graphql"
        os.mkfifo(fifo)
        keep_open = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(writer, b"query { viewer { login } }")
            assert _read_text(str(fifo), None) is None
        finally:
            os.close(writer)
            os.close(keep_open)
