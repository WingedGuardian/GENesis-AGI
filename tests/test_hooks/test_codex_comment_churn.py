"""Real budget -> adapter -> shell launcher -> harmless action sink (no native CLI)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import review_budget as rb  # noqa: E402

HEADS = [str(i) * 40 for i in range(1, 6)]


@pytest.mark.parametrize("case", [
    "neutral", "neutral_dispatched", "terminal", "terminal_dispatched",
    "confirmation", "head", "review", "malformed", "failed_read",
])
def test_real_launcher_comment_churn_action_receipts(tmp_path, case):
    reviews = [
        {
            "author": {"login": "chatgpt-codex-connector", "__typename": "Bot"},
            "state": "APPROVED", "submittedAt": "2026-01-01T00:00:00Z",
            "commit": {"oid": head},
            "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }
        for head in HEADS[:4]
    ] if case.startswith("terminal") else []
    commits = [
        {"commit": {"oid": head, "parents": {
            "totalCount": int(i > 0),
            "nodes": [{"oid": HEADS[i - 1]}] if i else [],
        }}}
        for i, head in enumerate(HEADS)
    ]
    snapshots = []
    for i in range(2):
        comment = {"body": "Progress", "author": {"login": "owner", "__typename": "User"}}
        if case == "confirmation":
            comment["body"] = rb.confirmation_marker(HEADS[-1])
        if case == "malformed":
            comment["body"] = 9
        connections = {
            "reviews": reviews + ([{
                "author": {"login": "chatgpt-codex-connector", "__typename": "Bot"},
                "state": "APPROVED", "submittedAt": "2026-01-01T00:00:00Z",
                "commit": {"oid": HEADS[0]},
                "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            }] if case == "review" and i else []),
            "comments": [comment] if i or case == "malformed" else [],
            "files": [{"path": "src/x.py", "changeType": "MODIFIED"}],
            "commits": commits,
        }
        pr = {"headRefOid": HEADS[0] if case == "head" and i else HEADS[-1], "body": "Fixture"}
        pr.update({name: {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
                   for name, nodes in connections.items()})
        snapshots.append({"data": {"repository": {"pullRequest": pr}}})
    (tmp_path / "snapshots.json").write_text(json.dumps(snapshots))
    shim = tmp_path / "gh"
    shim.write_text(f"#!{sys.executable}\n" + '''
import json, sys
from pathlib import Path
root = Path(__file__).parent
if sys.argv[1:4] == ["pr", "comment", "7"]:
    with (root / "receipt").open("a") as out:
        out.write(json.dumps(sys.argv[1:]) + "\\n")
elif sys.argv[1:3] == ["api", "graphql"]:
    counter = root / "reads"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    if (root / "failed_read").exists():
        sys.exit(1)
    print(json.dumps(json.loads((root / "snapshots.json").read_text())[min(n, 1)]))
else:
    sys.exit(64)
''')
    shim.chmod(0o700)
    if case == "failed_read":
        (tmp_path / "failed_read").touch()
    env = {k: v for k, v in os.environ.items() if not k.startswith("_TEST_")}
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GH_REPO",
                 "GENESIS_CC_SESSION", "GENESIS_SESSION_ORIGIN"):
        env.pop(name, None)
    env.update(PATH=f"{tmp_path}{os.pathsep}{env['PATH']}", GH_HOST="github.com",
               GENESIS_EXTERNAL_REVIEW_IDENTITY_TEMPLATE="")
    if case.endswith("dispatched"):
        env["GENESIS_CC_SESSION"] = "1"
    command = 'gh pr comment 7 --repo github.com/owner/repo --body "@codex review"'
    payload = {"tool_name": "Bash", "cwd": str(ROOT), "tool_input": {"command": command}}
    hook = subprocess.run(
        ["bash", str(ROOT / "scripts/hooks/codex-review-stop")],
        input=json.dumps(payload), env=env, capture_output=True, text=True, check=False,
    )
    # This harness models the observed exit-2 contract, not the native frontend.
    if hook.returncode == 0:
        subprocess.run(shlex.split(command), env=env, check=True)
    allowed = case in {"neutral", "neutral_dispatched"}
    assert hook.returncode == (0 if allowed else 2), hook.stderr
    assert hook.stdout == "", hook.stdout
    receipt = tmp_path / "receipt"
    assert receipt.exists() is allowed
    if allowed:
        assert [json.loads(row) for row in receipt.read_text().splitlines()] == [shlex.split(command)[1:]]
    else:
        assert "STOP and handoff" in hook.stderr
        reason = (
            "already has 4 review rounds" if case.startswith("terminal") else {
                "confirmation": "evidence_changed_during_evaluation",
                "head": "head_changed_during_evaluation",
                "review": "evidence_changed_during_evaluation",
                "malformed": "malformed_comment_record",
                "failed_read": "graphql_unreadable",
            }[case]
        )
        assert reason in hook.stderr, hook.stderr
    assert int((tmp_path / "reads").read_text()) == 2
