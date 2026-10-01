"""CI trigger contract for .github/workflows/ci.yml (#2735).

Loads ci.yml with PyYAML (no network) and asserts the current contract:
  * pull_request targets `main` only — stacked PRs are NOT supported and get
    no CI (a PR based on another branch is blocked by the merge gate anyway),
  * `edited` is not a listed activity type — a retarget or title/body edit
    does not re-run CI, so a passing verdict cannot be silently erased,
  * push-to-main / schedule / workflow_dispatch are untouched,
  * the cc-pin-receipts advisory compares against the PR's own base commit on
    pull_request events and falls back to merge-base with origin/main on
    push/schedule/dispatch, and
  * no other job step diffs against a literal `origin/main` where the PR base
    could differ — remaining uses are allowlisted with a reason.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _load() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def _on_block(doc: dict) -> dict:
    # YAML 1.1 parsers may resolve the bare key `on:` to boolean True.
    return doc.get("on") or doc.get(True)


def _pull_request(doc: dict) -> dict:
    pr = _on_block(doc)["pull_request"]
    return pr if isinstance(pr, dict) else {}


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def _env_text(job: dict) -> str:
    return "\n".join(
        str(v)
        for step in job.get("steps", [])
        for v in (step.get("env") or {}).values()
    )


def test_pull_request_targets_main_only():
    assert _pull_request(_load()).get("branches") == ["main"]


def test_pull_request_types_exclude_edited():
    types = _pull_request(_load()).get("types") or []
    assert "edited" not in types


def test_push_schedule_dispatch_unchanged():
    on = _on_block(_load())
    assert on["push"] == {"branches": ["main"]}
    assert on["schedule"] == [{"cron": "0 6 * * 1"}]
    assert "workflow_dispatch" in on


def test_cc_pin_uses_pr_base_sha():
    """The cc-pin-receipts advisory uses the PR's own base commit on
    pull_request events, falling back to merge-base origin/main elsewhere."""
    job = _load()["jobs"]["cc-pin-receipts"]
    assert "github.event.pull_request.base.sha" in _env_text(job)
    run = _run_text(job)
    assert "PR_BASE_SHA" in run
    assert "merge-base origin/main" in run


def test_no_job_hardcodes_main_as_pr_base():
    """A literal `origin/main` in a step script must be a non-PR fallback.

    On pull_request events the comparison base is the PR's own base commit;
    `origin/main` is only legitimate on push/schedule/dispatch, where the PR
    base SHA does not exist. Each remaining literal use is allowlisted with
    its reason.
    """
    allowlist = {
        "cc-pin-receipts": (
            "origin/main only on push/schedule/dispatch; PR events use the "
            "PR base SHA"
        ),
        "migration-check": (
            "origin/main is reached only when PR_BASE_SHA (a pull_request event "
            "field, base-branch-relative) and PUSH_BEFORE_SHA are both absent — "
            "i.e. schedule/dispatch, where main is the correct anchor"
        ),
    }
    offenders = []
    for name, job in _load()["jobs"].items():
        if "origin/main" in _run_text(job) and name not in allowlist:
            offenders.append(name)
    assert not offenders, (
        f"step scripts using literal origin/main without an allowlisted reason: "
        f"{offenders}"
    )
