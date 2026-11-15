"""The builder guard (spec E, E3e): with GENESIS_BUILDER_KEY set, a builder session
cannot merge a pull request or write a review thread, by any ordinary spelling;
without it, the hook does nothing at all."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "builder_guard.py"

REFUSED = [
    "gh pr merge 12 --squash",
    "gh pr merge --admin 12",
    "gh pr review 12 --approve",
    "gh pr review 12 --comment -b looks-good",
    "gh api -X PUT repos/o/r/pulls/12/merge",
    "gh api --method PUT repos/o/r/pulls/12/merge-async",
    "gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"x\"}) { clientMutationId } }'",
    "gh api repos/o/r/pulls/12/comments/99/replies -f body=ok",
    "gh api -X POST repos/o/r/pulls/12/comments -f body=ok -F in_reply_to=99",
    "gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:\"x\"}) { thread { id } } }'",
    "gh api graphql -f query='mutation { unresolveReviewThread(input:{threadId:\"x\"}) { thread { id } } }'",
    "gh api graphql -f query='mutation { addPullRequestReviewThreadReply(input:{}) { comment { id } } }'",
    "curl -X PUT -H 'Authorization: token x' https://api.github.com/repos/o/r/pulls/12/merge",
    'curl -d \'{"query":"mutation { resolveReviewThread(input:{}) { thread { id } } }"}\' https://api.github.com/graphql',
    "curl -d @body.json https://api.github.com/graphql",
    "wget --method=PUT https://api.github.com/repos/o/r/pulls/12/merge",
    "curl -X POST https://api.github.com/repos/o/r/pulls/12/comments/9/replies -d '{}'",
    "bash -c 'gh pr merge 12'",
    "cd /tmp && gh pr merge 12",
    "echo start; gh pr review 12 --approve",
    # Unreadable text that names a refused operation fails closed.
    "gh pr mer\\\nge 12",
    'gh api graphql --input - <<\'Q\'\n{"query": "mutation { resolveReviewThread(input:{}) {thread{id}} }"}\nQ',
    # Review round 1 on this hook: every spelling below got past the first cut.
    'gh api graphql -f query=\'mutation{addPullRequestReviewComment(input:{pullRequestReviewId:"r",inReplyTo:"c",body:"x"}){clientMutationId}}\'',
    "gh api graphql -f query='mutation{addPullRequestReview(input:{pullRequestId:\"p\",event:APPROVE}){clientMutationId}}'",
    "gh api graphql -f query='mutation{minimizeComment(input:{subjectId:\"s\",classifier:RESOLVED}){clientMutationId}}'",
    "gh api repos/o/r/pulls/5/comments --input reply.json",
    "gh api repos/o/r/pulls/5/comments --input missing.json",
    "curl -d @body.json https://api.github.com/repos/o/r/pulls/5/comments",
    "curl -X POST https://api.github.com/repos/o/r/pulls/5/comments --data-binary @body.json",
    'eval "gh pr merge 5"',
    "eval gh pr merge 5",
    "xargs -I{} gh pr merge {} <<< 5",
    "find . -maxdepth 0 -exec gh pr merge 5 \\;",
    "python3 -c 'import os; os.system(\"gh pr merge 5\")'",
    "gh api repos/o/r/pulls/5/comments/9//replies -f body=hi",
    "gh api repos/o/r/pulls//5/comments/9/replies -f body=hi",
    "curl -XPUT https://api.github.com/repos/o/r/pulls//5/merge",
    "curl -X PUT https://api.github.com/repos/o/r/pulls/5/./merge",
    "curl -X PUT 'https://api.github.com/repos/o/r/pulls/5/%6derge'",
    "curl -d @q.json https://API.GITHUB.COM/graphql",
    "curl --data-binary @q.json https://api.github.com:443/graphql",
    'curl -sd "$BODY" https://api.github.com/graphql',
    "wget --post-file=q.json https://api.github.com/graphql",
    'wget --post-data "$BODY" https://api.github.com/graphql',
    "curl -K curl.cfg",
    "gh api repos/o/r/pulls/5/reviews -f event=APPROVE",
    'curl -X POST https://api.github.com/repos/o/r/pulls/5/reviews -d \'{"event":"APPROVE"}\'',
    "gh api -X PUT repos/o/r/pulls/5/reviews/7/dismissals -f message=x",
    "gh api -X DELETE repos/o/r/pulls/comments/9",
    "gh api repos/o/r/merges -f base=main -f head=feat",
    "gh alias set mm 'pr merge'",
    "gh extension exec merge 5",
    # Adversarial audit: routes past the second cut.
    "curl -sK curl.cfg",
    "curl -sSK curl.cfg",
    "$(which gh) pr merge 5",
    "curl -X PUT --url=https://api.github.com/repos/o/r/pulls/5/merge",
    "gh api 'graphql?x=1' -f query=\"$(cat m.graphql)\"",
    "curl -X PUT https://api.github.com/repos/o/r/pulls/5/x/../merge",
    "python3 -c 'import os; os.system(\"gh pr merge 5\")'",
]

ALLOWED = [
    "gh pr create --title t --body b",
    "gh pr comment 12 --body '@codex review'",
    "gh pr view 12 --json state,mergeable",
    "gh pr checks 12",
    "gh api repos/o/r/pulls/12/comments",
    "gh api repos/o/r/pulls/12/comments/99",
    "gh api graphql -f query='query { viewer { login } }'",
    "gh issue view 12",
    "git push -u origin HEAD",
    "git merge --ff-only origin/main",
    "curl -s https://api.github.com/repos/o/r/pulls/12",
    "ls -la",
    "grep -n review notes.md",
    # A plain GET of the merge endpoint only asks whether the PR is merged.
    "curl -s -H 'Authorization: Bearer x' https://api.github.com/repos/o/r/pulls/5/merge",
    "curl -s https://api.github.com/repos/o/r/pulls/5/reviews",
    "gh api repos/o/r/pulls/5/reviews",
    "find . -name '*review*'",
    "python3 -c 'print(1)'",
    "gh api repos/o/r/pulls/5/comments --input plain.json",
    # Adversarial audit: ordinary work the second cut refused.
    "python3 -m pytest tests/test_hooks/test_gh_merge.py -k graphql -q",
    "python3 -m pytest tests/test_board/ -k 'in_reply_to or merge' -q",
    "python3 scripts/fetch_pr.py https://api.github.com/repos/o/r/pulls/5",
    "node scripts/graphql-schema.js",
    "find . -name '*graphql*'",
    "find src -path '*/pulls/*'",
    "watch -n 30 gh api repos/o/r/pulls/5",
    "perl -ne 'print if /graphql/' file.txt",
    "curl -s -o /dev/null -w '[%{http_code}]' https://example.com",
    "curl -s -H 'X-Ids: [1,2]' https://example.com",
    'curl -s -H "Authorization: bearer $T" -d \'{"query":"query { viewer { login } }"}\' https://api.github.com/graphql',
    'curl -X PATCH -H "Authorization: token $GH_TOKEN" https://api.github.com/repos/o/r/pulls/5 -d \'{"body":"x"}\'',
    "gh api repos/o/r/issues/5/comments -f body='the guard now refuses resolveReviewThread'",
    "gh api -X PATCH repos/o/r/pulls/5 -f body='Blocks minimizeComment and addPullRequestReview.'",
]


def _fixtures(cwd: Path) -> None:
    (cwd / "reply.json").write_text('{"body": "x", "in_reply_to": 9}')
    (cwd / "plain.json").write_text('{"body": "x", "commit_id": "abc", "path": "a.py", "line": 1}')
    (cwd / "body.json").write_text('{"body": "x", "in_reply_to": 9}')
    (cwd / "q.json").write_text(
        '{"query": "mutation { resolveReviewThread(input:{}) {thread{id}} }"}'
    )
    (cwd / "curl.cfg").write_text("url = https://api.github.com/repos/o/r/pulls/5/merge\n")


def _run(cmd: str, *, key: str | None, cwd: Path, extra_env: dict | None = None):
    _fixtures(cwd)
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_BUILDER_KEY"}
    if key is not None:
        env["GENESIS_BUILDER_KEY"] = key
    env.update(extra_env or {})
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


@pytest.mark.parametrize("cmd", REFUSED)
def test_a_builder_cannot_merge_or_write_a_review_thread(tmp_path, cmd):
    res = _run(cmd, key="PVTI_x@2026-11-15T00:00:00Z", cwd=tmp_path)
    assert res.returncode == 2, (cmd, res.stderr)
    assert "BLOCKED (builder session)" in res.stderr
    assert "closing session" in res.stderr


@pytest.mark.parametrize("cmd", REFUSED + ALLOWED)
@pytest.mark.parametrize("key", [None, "", "   "])
def test_without_the_key_the_guard_does_nothing(tmp_path, cmd, key):
    res = _run(cmd, key=key, cwd=tmp_path)
    assert res.returncode == 0 and res.stderr == "", (cmd, res.stderr)


@pytest.mark.parametrize("cmd", ALLOWED)
def test_a_builder_keeps_its_ordinary_work(tmp_path, cmd):
    res = _run(cmd, key="k@1", cwd=tmp_path)
    assert res.returncode == 0, (cmd, res.stderr)


def test_a_thread_mutation_in_a_query_file_is_refused(tmp_path):
    (tmp_path / "q.graphql").write_text(
        'mutation { resolveReviewThread(input:{threadId:"x"}) { thread { id } } }\n'
    )
    res = _run("gh api graphql -F query=@q.graphql", key="k@1", cwd=tmp_path)
    assert res.returncode == 2, res.stderr


def test_a_read_query_file_is_allowed(tmp_path):
    (tmp_path / "q.graphql").write_text("query { viewer { login } }\n")
    res = _run("gh api graphql -F query=@q.graphql", key="k@1", cwd=tmp_path)
    assert res.returncode == 0, res.stderr


def _shadow_run(shadow: Path, key: str | None):
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_BUILDER_KEY"}
    if key:
        env["GENESIS_BUILDER_KEY"] = key
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    return subprocess.run(
        [sys.executable, str(shadow / "builder_guard.py")],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


def test_a_guard_that_cannot_import_its_parser_blocks_a_builder_only(tmp_path):
    """The parser modules load only for a builder; without them a builder is
    refused (exit 1 would let the command run) and everyone else passes."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "builder_guard.py").write_text(HOOK.read_text())
    (shadow / "hook_input.py").write_text((HOOK.parent / "hook_input.py").read_text())
    (shadow / "shell_parse.py").write_text("raise ImportError('broken tree')\n")
    res = _shadow_run(shadow, "k@1")
    assert res.returncode == 2 and "could not check" in res.stderr, res.stderr
    assert _shadow_run(shadow, None).returncode == 0


def test_a_broken_hook_input_blocks_every_session_and_says_so(tmp_path):
    """The house degraded contract every Bash blocker shares (test_import_time_degraded)."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "builder_guard.py").write_text(HOOK.read_text())
    (shadow / "hook_input.py").write_text("raise ImportError('broken tree')\n")
    for key in ("k@1", None):
        res = _shadow_run(shadow, key)
        assert res.returncode == 2 and "GUARD DEGRADED" in res.stderr, res.stderr


def test_the_guard_is_wired_for_every_bash_call():
    settings = json.loads((HOOK.parents[2] / ".claude" / "settings.json").read_text())
    commands = [
        h["command"]
        for block in settings["hooks"]["PreToolUse"]
        if block.get("matcher") == "Bash"
        for h in block["hooks"]
    ]
    assert any(c.endswith("genesis-hook hooks/builder_guard.py") for c in commands)
