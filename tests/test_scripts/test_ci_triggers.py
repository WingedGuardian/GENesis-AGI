"""CI trigger contract for .github/workflows/ci.yml (#2735).

Loads ci.yml with PyYAML (no network) and asserts the stacked-PR contract:
  * pull_request fires whatever the base branch (no branches filter),
  * `edited` is a listed activity type (a retarget re-runs CI),
  * every job skips an `edited` event that changed no base (title/body edits
    must not re-run the suite),
  * push-to-main / schedule / workflow_dispatch are untouched, and
  * no job step diffs against a literal `origin/main` where the PR base could
    be another branch — remaining uses are allowlisted with a reason.
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


def test_pull_request_has_no_base_branch_filter():
    pr = _pull_request(_load())
    assert "branches" not in pr or pr["branches"] == ["**"]


def test_pull_request_types_include_edited():
    types = _pull_request(_load()).get("types", [])
    for t in ("opened", "synchronize", "reopened", "edited"):
        assert t in types, f"pull_request.types missing {t!r}: {types}"


def test_edited_runs_only_on_base_change():
    """Every job carries an if: that excludes `edited` events lacking
    `changes.base` — a title/body edit must not re-run the suite."""
    for name, job in _load()["jobs"].items():
        cond = str(job.get("if", ""))
        assert "github.event.changes.base" in cond, (
            f"job {name!r} has no base-change guard for `edited` events: {cond!r}"
        )


def test_push_main_and_schedule_unchanged():
    on = _on_block(_load())
    assert on["push"]["branches"] == ["main"]
    assert "schedule" in on
    assert "workflow_dispatch" in on


def test_no_job_hardcodes_main_as_pr_base():
    """A literal `origin/main` in a step script must be a non-PR fallback.

    PR diff bases are the PR's own base branch; a stacked PR diffs against its
    parent branch. Each remaining literal use is allowlisted with its reason.
    """
    allowlist = {
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
