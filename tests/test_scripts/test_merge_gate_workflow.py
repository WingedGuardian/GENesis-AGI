"""`.github/workflows/merge-gate.yml` keeps ONE `genesis-merge-gate` check per head (#2409).

The `pull_request` job used to carry the check name itself, and concurrency cancels
it whenever a review event follows: MEASURED 2026-10-31, 74 of 85 open PRs listed
`genesis-merge-gate` twice, 31 of 34 sampled `pull_request` entries `cancelled`.
The job now runs under its own name and every event publishes the verdict. These
pin that shape, since a later edit restoring either half brings the duplicate back.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "merge-gate.yml"
VERDICT = "genesis-merge-gate"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text())


def _job() -> dict:
    return _workflow()["jobs"]["genesis-merge-gate"]


def _step(name: str) -> dict:
    (step,) = [s for s in _job()["steps"] if s.get("name") == name]
    return step


def test_the_job_does_not_carry_the_verdict_name():
    assert _job().get("name") not in (None, VERDICT)


def test_every_event_publishes_the_verdict_under_its_name():
    publish = _step("Publish the verdict on the PR head")
    assert "if" not in publish, "a filtered publish leaves some events with no verdict on the head"
    assert f'-f name="{VERDICT}"' in publish["run"]


def test_the_gate_step_never_fails_the_job():
    run = _step("Run the merge gate")["run"]
    exits = [ln.strip() for ln in run.splitlines() if ln.strip().startswith("exit")]
    assert exits == ["exit 0"], exits


def test_a_read_only_token_hands_off_for_every_pull_request_event():
    """A fork PR and anything Dependabot triggers run with a read-only token."""
    publish = _step("Publish the verdict on the PR head")
    assert "pull_request|pull_request_review|pull_request_review_comment)" in publish["run"]
    assert '[ "$ACTOR" = "dependabot[bot]" ]' in publish["run"]
    assert publish["env"]["ACTOR"] == "${{ github.actor }}"


def test_the_job_entry_stays_excluded_by_the_gate():
    """`_pr_ci_status` drops the job's entry by workflowName "merge-gate"; a
    renamed workflow would put the job back into the CI row as a check."""
    guard = (_WORKFLOW.parents[2] / "scripts" / "hooks" / "git_push_guard.py").read_text()
    assert _workflow()["name"] == "merge-gate"
    assert 'wf == "merge-gate"' in guard


def test_the_workflow_may_write_check_runs():
    assert _workflow()["permissions"]["checks"] == "write"
