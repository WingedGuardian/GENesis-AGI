"""Dedup-by-name tests for git_push_guard._pr_ci_status (merge-authorization gate).

Origin (MEASURED on ec925917 during the #1408 merge): a duplicate pull_request
run was concurrency-cancelled (ci/codeql `cancel-in-progress: true` for non-main),
leaving the superseded run's `cancelled` check-runs attached to the commit. The
classifier read EVERY statusCheckRollup entry independently, so it reported
`ci: red` forever even though every check NAME's latest run was SUCCESS (`gh pr
checks` and the GitHub UI both dedupe latest-per-name; the gate didn't).

The fix groups entries by identity (name / context) and, per group:
  1. any non-terminal entry (a re-run in flight) → PENDING (short-circuit
     BEFORE any time ordering — a queued re-run has no completedAt/startedAt);
  2. else all terminal → classify the latest by completedAt;
  3. unorderable terminal group (missing/tied completedAt) with any red → red
     (fail-closed).

Security invariant: no path turns a real red or a non-terminal check into green;
only a SUPERSEDED terminal run that has a newer terminal sibling is dropped.

New file (not test_merge_review_gate.py) to avoid a collision with PR #1416,
which also references this function.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"


@pytest.fixture(scope="module")
def guard_module():
    """Import git_push_guard as a module for direct function testing."""
    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _set(monkeypatch, checks):
    monkeypatch.setenv("_TEST_GH_CI_ROLLUP", json.dumps(checks))


def test_superseded_cancelled_run_deduped_to_green(guard_module, monkeypatch):
    """THE incident: a check NAME with a superseded `cancelled` run and a newer
    `success` run reads GREEN (latest-per-name), not red."""
    _set(
        monkeypatch,
        [
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "startedAt": "2026-08-19T16:35:12Z",
                "completedAt": "2026-08-19T16:35:40Z",
                "workflowName": "CodeQL",
            },
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-19T16:35:15Z",
                "completedAt": "2026-08-19T16:40:00Z",
                "workflowName": "CodeQL",
            },
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "completedAt": "2026-08-19T16:38:00Z",
                "workflowName": "CI",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("green", [])


def test_only_cancelled_run_stays_red(guard_module, monkeypatch):
    """A name whose ONLY run is cancelled (no newer sibling) still blocks —
    dedup drops SUPERSEDED runs, never a genuine terminal cancel."""
    _set(
        monkeypatch,
        [
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "completedAt": "2026-08-19T16:35:40Z",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["Analyze"])


def test_queued_rerun_over_old_success_is_pending(guard_module, monkeypatch):
    """A re-run in flight (QUEUED, no completedAt/startedAt) over an older
    success must read PENDING — the merge waits for the re-run. This is the
    null-timestamp trap: a naive 'latest by startedAt' would rank the queued
    entry oldest and let the old success win (wrong-green). The non-terminal
    short-circuit runs BEFORE any ordering, so it can't."""
    _set(
        monkeypatch,
        [
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "completedAt": "2026-08-19T16:40:00Z",
            },
            {"name": "Analyze", "status": "QUEUED", "conclusion": None},
        ],
    )
    assert guard_module._pr_ci_status("1") == ("pending", ["Analyze"])


def test_unorderable_terminal_group_fails_closed_red(guard_module, monkeypatch):
    """Two terminal runs of one name with NO completedAt to order by (legacy
    shape) and conflicting results → fail-closed red, never a coin-flip green."""
    _set(
        monkeypatch,
        [
            {"name": "Deploy", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"name": "Deploy", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["Deploy"])


def test_mixed_shape_group_without_completedat_fails_closed(guard_module, monkeypatch):
    """A CheckRun (has completedAt) and a legacy StatusContext (no completedAt,
    keyed by `context`) sharing ONE identity can't be time-ordered → fail-closed:
    a red anywhere in the group keeps it red, never a coin-flip green."""
    _set(
        monkeypatch,
        [
            {
                "name": "CI",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "completedAt": "2026-08-19T16:40:00Z",
            },
            {"context": "CI", "state": "FAILURE"},  # StatusContext, no completedAt
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["CI"])


def test_tied_invocation_key_group_fails_closed(guard_module, monkeypatch):
    """Two terminal runs of one name+workflow with the SAME (startedAt,
    completedAt) have no unique latest invocation → fail-closed red on a
    red/green conflict."""
    _set(
        monkeypatch,
        [
            {
                "name": "Build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-19T16:30:00Z",
                "completedAt": "2026-08-19T16:40:00Z",
                "workflowName": "CI",
            },
            {
                "name": "Build",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-19T16:30:00Z",
                "completedAt": "2026-08-19T16:40:00Z",
                "workflowName": "CI",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["Build"])


def test_dedup_is_per_name_independent(guard_module, monkeypatch):
    """Deduping one name's superseded cancel to green must NOT mask a genuine
    failure on a DIFFERENT name."""
    _set(
        monkeypatch,
        [
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "startedAt": "2026-08-19T16:35:12Z",
                "completedAt": "2026-08-19T16:35:40Z",
                "workflowName": "CodeQL",
            },
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-19T16:35:15Z",
                "completedAt": "2026-08-19T16:40:00Z",
                "workflowName": "CodeQL",
            },
            {
                "name": "unit",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-19T16:37:00Z",
                "completedAt": "2026-08-19T16:38:00Z",
                "workflowName": "CI",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["unit"])


def test_same_workflow_success_supersedes_red(guard_module, monkeypatch):
    """A later-INVOCATION SUCCESS under the SAME workflowName legitimately
    supersedes an earlier red (the real concurrency-cancel/re-run case)."""
    _set(
        monkeypatch,
        [
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "startedAt": "2026-08-19T10:00:00Z",
                "completedAt": "2026-08-19T10:05:00Z",
                "workflowName": "CodeQL",
            },
            {
                "name": "Analyze",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-19T10:30:00Z",
                "completedAt": "2026-08-19T11:00:00Z",
                "workflowName": "CodeQL",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("green", [])


def test_cross_workflow_success_cannot_supersede_red(guard_module, monkeypatch):
    """SECURITY: a later SUCCESS under a DIFFERENT workflowName must NOT override
    a real red that shares only the check NAME — bare `name` is not run identity,
    so a foreign workflow/app can't launder a failure into a merge-authorizing
    green."""
    _set(
        monkeypatch,
        [
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "completedAt": "2026-08-19T10:00:00Z",
                "workflowName": "CI",
            },
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "completedAt": "2026-08-19T11:00:00Z",
                "workflowName": "Injected",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["test"])


def test_later_skip_does_not_launder_prior_red(guard_module, monkeypatch):
    """A later SKIPPED/NEUTRAL run must NOT clear an earlier real FAILURE (e.g. a
    path-filtered re-run) — only a genuine SUCCESS supersedes a red."""
    _set(
        monkeypatch,
        [
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-19T10:00:00Z",
                "completedAt": "2026-08-19T10:10:00Z",
                "workflowName": "CI",
            },
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "startedAt": "2026-08-19T10:30:00Z",
                "completedAt": "2026-08-19T10:35:00Z",
                "workflowName": "CI",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["test"])


def test_none_workflow_entries_do_not_supersede(guard_module, monkeypatch):
    """SECURITY (Codex P1): an ABSENT workflowName is not a run identity —
    entries from two different apps both yield None and would collide under a
    bare `len(workflows)==1` check. A None-identity group is unorderable →
    fail-closed, so a later same-named SUCCESS can't launder a real FAILURE."""
    _set(
        monkeypatch,
        [
            {
                "name": "gate",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-19T10:00:00Z",
                "completedAt": "2026-08-19T10:10:00Z",
                # no workflowName (app-published check)
            },
            {
                "name": "gate",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-19T10:30:00Z",
                "completedAt": "2026-08-19T10:40:00Z",
                # no workflowName (different app)
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["gate"])


def test_invocation_order_uses_startedat_not_completedat(guard_module, monkeypatch):
    """SECURITY (Codex P1): overlapping runs can COMPLETE out of order — a
    superseded older invocation may finish after its replacement. Ordering must
    be by startedAt (invocation order), not completedAt: here an OLD success
    that finishes LAST must not supersede the NEWER failing invocation."""
    _set(
        monkeypatch,
        [
            {  # older invocation (started first) that happens to finish last
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-19T10:00:00Z",
                "completedAt": "2026-08-19T11:00:00Z",
                "workflowName": "CI",
            },
            {  # newer invocation (started later) that finishes first
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-19T10:30:00Z",
                "completedAt": "2026-08-19T10:45:00Z",
                "workflowName": "CI",
            },
        ],
    )
    # By completedAt the old SUCCESS (11:00) would win → wrong green. By
    # startedAt the newer FAILURE (10:30) is latest → red.
    assert guard_module._pr_ci_status("1") == ("red", ["test"])


def test_expected_legacy_status_blocks_not_vanishes(guard_module, monkeypatch):
    """SECURITY (audit): a legacy StatusContext state=EXPECTED is a required
    check that has NOT yet reported. It must count as pending, never be dropped
    as 'ignore' — otherwise it vanishes and a green sibling authorizes the merge
    past a still-unsatisfied required check."""
    # EXPECTED alongside a green check → the merge must still block (pending).
    _set(
        monkeypatch,
        [
            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "required-external-ci", "state": "EXPECTED"},
        ],
    )
    assert guard_module._pr_ci_status("1") == ("pending", ["required-external-ci"])


def test_completed_with_unknown_conclusion_is_pending(guard_module, monkeypatch):
    """SECURITY (audit): a COMPLETED run with an unrecognized/future conclusion
    must fail toward BLOCKING (pending), not silently non-block ('skip')."""
    _set(
        monkeypatch,
        [
            {"name": "future-check", "status": "COMPLETED", "conclusion": "SOME_NEW_STATE"},
        ],
    )
    assert guard_module._pr_ci_status("1") == ("pending", ["future-check"])


def test_stale_conclusion_is_non_blocking(guard_module, monkeypatch):
    """A STALE conclusion (a run superseded before it reported) is genuinely
    non-blocking — it must NOT push the group to pending/red the way an unknown
    conclusion does."""
    _set(
        monkeypatch,
        [
            {"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "b", "status": "COMPLETED", "conclusion": "STALE"},
        ],
    )
    assert guard_module._pr_ci_status("1") == ("green", [])


def test_nonstring_completedat_fails_closed(guard_module, monkeypatch):
    """A non-string completedAt (gh never emits this; defense-in-depth) makes the
    group unorderable → fail closed, never a TypeError that could abort mid-scan."""
    _set(
        monkeypatch,
        [
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "completedAt": "2026-08-19T10:00:00Z",
                "workflowName": "CI",
            },
            {
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "completedAt": 12345,
                "workflowName": "CI",
            },
        ],
    )
    assert guard_module._pr_ci_status("1") == ("red", ["test"])
