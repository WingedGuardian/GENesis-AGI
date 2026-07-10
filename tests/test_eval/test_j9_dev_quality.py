"""Tests for the J-9 ``dev_quality`` dimension (component g).

Uses the shared in-memory migrated ``db`` fixture (tests/conftest.py) and
seeds the three read paths directly: pr_review_findings observations,
code_audit observations, and tool_call_outcomes rows.
"""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import j9_eval
from genesis.eval.j9_aggregator import _compute_dev_quality

_SINCE = "2026-07-01T00:00:00+00:00"
_UNTIL = "2026-07-08T00:00:00+00:00"
_IN_WINDOW = "2026-07-03T10:00:00Z"       # GitHub-style Z suffix on purpose
_OUT_OF_WINDOW = "2026-06-01T10:00:00Z"


async def _seed_pr_row(db, number: int, *, merged_at: str,
                       findings: list[dict], review_count: int) -> None:
    content = json.dumps({
        "pr": number, "title": f"pr {number}", "merged_at": merged_at,
        "review_count": review_count, "findings": findings,
    })
    await db.execute(
        "INSERT INTO observations (id, source, type, category, content, "
        "priority, created_at) VALUES (?, 'recon', 'pr_review_findings', "
        "'pr_review_findings', ?, 'low', ?)",
        (f"prrev-{number}", content, "2026-07-06T00:00:00+00:00"),
    )


async def _seed_audit_row(db, oid: str, *, severity: str,
                          category: str | None = None,
                          resolved: int = 0) -> None:
    payload: dict = {"severity": severity}
    if category:
        payload["category"] = category
    await db.execute(
        "INSERT INTO observations (id, source, type, category, content, "
        "priority, created_at, resolved) VALUES (?, 'recon', 'code_audit', "
        "'code_audit', ?, 'medium', '2026-01-01T00:00:00+00:00', ?)",
        (oid, json.dumps(payload), resolved),
    )


async def _seed_tool_outcome(db, *, tool_name: str = "Edit",
                             success: int = 1, timestamp: str) -> None:
    await db.execute(
        "INSERT INTO tool_call_outcomes (tool_name, success, timestamp) "
        "VALUES (?, ?, ?)",
        (tool_name, success, timestamp),
    )


async def test_dev_quality_metrics_exact(db):
    # PR in window: blocker + should_fix, 2 reviews. PR out of window: ignored
    # by the windowed metrics but still counted by harvest_prs_seen.
    await _seed_pr_row(
        db, 1, merged_at=_IN_WINDOW, review_count=2,
        findings=[{"severity": "blocker"}, {"severity": "should_fix"}],
    )
    await _seed_pr_row(
        db, 2, merged_at=_OUT_OF_WINDOW, review_count=1,
        findings=[{"severity": "note"}],
    )
    # Open audit finding (counts) + resolved one (must not count).
    await _seed_audit_row(db, "au1", severity="should_fix", category="correctness")
    await _seed_audit_row(db, "au2", severity="blocker", resolved=1)
    # Edit/Write outcomes: 3 in window (1 failure), 1 out of window, and one
    # in-window non-Edit tool that must be excluded.
    await _seed_tool_outcome(db, timestamp="2026-07-02T09:00:00+00:00")
    await _seed_tool_outcome(db, tool_name="Write",
                             timestamp="2026-07-03T09:00:00+00:00")
    await _seed_tool_outcome(db, success=0, timestamp="2026-07-04T09:00:00+00:00")
    await _seed_tool_outcome(db, timestamp="2026-06-01T09:00:00+00:00")
    await _seed_tool_outcome(db, tool_name="Bash",
                             timestamp="2026-07-02T09:30:00+00:00")
    await db.commit()

    metrics, sample = await _compute_dev_quality(db, _SINCE, _UNTIL)

    assert metrics["prs_merged"] == 1
    assert metrics["review_findings_total"] == 2
    assert metrics["review_findings_by_severity"] == {
        "blocker": 1, "should_fix": 1, "note": 0, "unlabeled": 0,
    }
    assert metrics["findings_per_pr"] == 2.0
    assert metrics["review_count_total"] == 2
    assert metrics["mean_reviews_per_pr"] == 2.0
    assert metrics["harvest_prs_seen"] == 2  # coverage: windowed or not

    assert metrics["code_audit_open_findings"] == 1
    assert metrics["code_audit_by_severity"] == {"should_fix": 1}
    assert metrics["code_audit_by_category"] == {"correctness": 1}

    assert metrics["edit_calls_total"] == 3
    assert metrics["edit_failures"] == 1
    assert metrics["edit_failure_rate"] == pytest.approx(1 / 3, abs=1e-3)

    assert sample == 1 + 3  # prs_merged + edit_calls_total
    assert "note" in metrics  # taxonomy scaffold status stays visible


async def test_dev_quality_unknown_severity_folds_to_unlabeled(db):
    """A severity string outside the canonical vocab is never dropped or
    guessed — it lands in the honest ``unlabeled`` bucket."""
    await _seed_pr_row(
        db, 3, merged_at=_IN_WINDOW, review_count=0,
        findings=[{"severity": "critical"}, {}],
    )
    await db.commit()

    metrics, _ = await _compute_dev_quality(db, _SINCE, _UNTIL)
    assert metrics["review_findings_total"] == 2
    assert metrics["review_findings_by_severity"]["unlabeled"] == 2


async def test_dev_quality_empty_db_rates_none_never_zero(db):
    metrics, sample = await _compute_dev_quality(db, _SINCE, _UNTIL)

    assert sample == 0
    assert metrics["prs_merged"] == 0
    assert metrics["review_findings_total"] == 0
    assert metrics["review_findings_by_severity"] == {
        "blocker": 0, "should_fix": 0, "note": 0, "unlabeled": 0,
    }
    assert metrics["findings_per_pr"] is None       # 0/0 → None, NEVER 0.0
    assert metrics["review_count_total"] == 0
    assert metrics["mean_reviews_per_pr"] is None
    assert metrics["code_audit_open_findings"] == 0
    assert metrics["code_audit_by_severity"] == {}
    assert metrics["code_audit_by_category"] == {}
    assert metrics["edit_calls_total"] == 0
    assert metrics["edit_failures"] == 0
    assert metrics["edit_failure_rate"] is None
    assert metrics["harvest_prs_seen"] == 0


async def test_run_weekly_aggregation_includes_dev_quality_snapshot_only(db):
    """E2E guard: the dimension is registered (run_weekly_aggregation swallows
    per-dimension exceptions, so absence = silent production failure) AND it
    writes ZERO eval_events — the eval_events CHECK constraint does not
    include 'dev_quality'; this dimension is snapshot-only."""
    from genesis.eval.j9_aggregator import run_weekly_aggregation

    results = await run_weekly_aggregation(db)

    assert "dev_quality" in results, "dev_quality failed silently"
    snap = await j9_eval.get_latest_snapshot(db, dimension="dev_quality")
    assert snap is not None
    assert "findings_per_pr" in snap["metrics"]
    assert "edit_failure_rate" in snap["metrics"]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM eval_events WHERE dimension = 'dev_quality'",
    )
    assert (await cursor.fetchone())[0] == 0
