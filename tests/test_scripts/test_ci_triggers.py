"""CI trigger contract for .github/workflows/ci.yml (#2735).

Loads ci.yml with PyYAML (no network) and asserts the current contract:
  * pull_request targets `main` only — stacked PRs are NOT supported and get
    no CI (a PR based on another branch is blocked by the merge gate anyway),
  * `edited` is not a listed activity type — a retarget or title/body edit
    does not re-run CI, so a passing verdict cannot be silently erased,
  * push-to-main / schedule / workflow_dispatch are untouched,
  * the cc-pin-receipts advisory diffs against merge-base origin/main HEAD —
    on PR runs HEAD is the merge ref, whose first parent is main at CI time,
    so merge-base is the PR's base; pull_request.base.sha lags main, and
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
        str(v) for step in job.get("steps", []) for v in (step.get("env") or {}).values()
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


def test_cc_pin_uses_merge_base_with_main():
    """The cc-pin-receipts advisory diffs against merge-base origin/main HEAD.

    On pull_request runs the checkout is the merge ref, whose first parent is
    main at CI time — merge-base IS the PR's base. `pull_request.base.sha`
    lags main and must not appear in the job.
    """
    job = _load()["jobs"]["cc-pin-receipts"]
    assert "git merge-base origin/main HEAD" in _run_text(job)
    assert "github.event.pull_request.base.sha" not in _run_text(job)
    assert "github.event.pull_request.base.sha" not in _env_text(job)


def test_no_job_hardcodes_main_as_pr_base():
    """A literal `origin/main` in a step script must be a non-PR fallback.

    On pull_request events the comparison base is the PR's own base commit;
    `origin/main` is only legitimate on push/schedule/dispatch, where the PR
    base SHA does not exist. Each remaining literal use is allowlisted by
    EXACT command line — a new use inside an already-exempted job still
    trips this test, and a renamed command leaves a dead exemption behind,
    which the stale-guard assertion rejects.
    """
    allowlist: dict[tuple[str, str], str] = {
        (
            "cc-pin-receipts",
            'base="$(git merge-base origin/main HEAD 2>/dev/null || true)"',
        ): (
            "PR runs check out the merge ref, whose first parent is main at "
            "CI time, so merge-base origin/main HEAD is the PR's base; "
            "pull_request.base.sha lags main (#2774)"
        ),
        (
            "migration-check",
            'if [[ -z "$base" ]] && git rev-parse --verify -q origin/main >/dev/null \\',
        ): (
            "origin/main is reached only when PR_BASE_SHA (a pull_request event "
            "field, base-branch-relative) and PUSH_BEFORE_SHA are both absent — "
            "i.e. schedule/dispatch, where main is the correct anchor"
        ),
        (
            "migration-check",
            '&& [[ "$(git rev-parse origin/main)" != "$(git rev-parse HEAD)" ]]; then',
        ): (
            "origin/main is reached only when PR_BASE_SHA (a pull_request event "
            "field, base-branch-relative) and PUSH_BEFORE_SHA are both absent — "
            "i.e. schedule/dispatch, where main is the correct anchor"
        ),
        (
            "migration-check",
            'base="origin/main"',
        ): (
            "origin/main is reached only when PR_BASE_SHA (a pull_request event "
            "field, base-branch-relative) and PUSH_BEFORE_SHA are both absent — "
            "i.e. schedule/dispatch, where main is the correct anchor"
        ),
    }
    seen: set[tuple[str, str]] = set()
    offenders = []
    for name, job in _load()["jobs"].items():
        for line in _run_text(job).split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or "origin/main" not in stripped:
                continue
            if (name, stripped) in allowlist:
                seen.add((name, stripped))
            else:
                offenders.append((name, stripped))
    assert not offenders, (
        f"step-script lines using literal origin/main without an allowlisted reason: {offenders}"
    )
    stale = set(allowlist) - seen
    assert not stale, f"allowlist entries no longer present in ci.yml: {stale}"
