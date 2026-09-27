"""Tests for the eval_staleness health snapshot.

Covers the three derivations the snapshot documents: one entry per
(model_id, profile), infrastructural vs substantive failure, and staleness only
for a profile something is expected to re-run.
"""

from __future__ import annotations

import importlib
import itertools
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.observability.snapshots.eval_staleness import eval_staleness

_MIGRATIONS = (
    "0002_add_eval_tables",
    "0003_eval_results_skipped",
    "0014_eval_results_metadata",
)
_ids = itertools.count()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    for mig in _MIGRATIONS:
        await importlib.import_module(f"genesis.db.migrations.{mig}").up(conn)
    await conn.commit()
    yield conn
    await conn.close()


def _ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def _run(
    db,
    *,
    model_id="free-tier",
    profile="model-b",
    trigger="surplus",
    days_ago=0.0,
    passed=0,
    wrong=0,
    provider_errors=0,
    skipped=0,
    write_results=True,
):
    """Insert one run shaped the way genesis.eval.runner records it."""
    run_id = f"run{next(_ids)}"
    created = _ago(days_ago)
    await db.execute(
        """INSERT INTO eval_runs
           (id, model_id, model_profile, dataset, trigger, task_category,
            total_cases, passed_cases, failed_cases, skipped_cases, created_at)
           VALUES (?, ?, ?, 'classification', ?, 'classification', ?, ?, ?, ?, ?)""",
        (
            run_id,
            model_id,
            profile,
            trigger,
            passed + wrong + provider_errors + skipped,
            passed,
            wrong + provider_errors,
            skipped,
            created,
        ),
    )
    if write_results:
        cases = (
            [(1, 0, "exact match")] * passed
            + [(0, 0, "expected='4', got='3'")] * wrong
            + [(0, 0, "provider error: litellm call exceeded hard timeout")] * provider_errors
            + [(0, 1, "provider error (transient, 429): rate limited")] * skipped
        )
        for n, (ok, sk, detail) in enumerate(cases):
            await db.execute(
                """INSERT INTO eval_results
                   (id, run_id, case_id, input_text, passed, scorer_type,
                    scorer_detail, skipped, created_at)
                   VALUES (?, ?, ?, 'x', ?, 'exact_match', ?, ?, ?)""",
                (f"{run_id}-{n}", run_id, f"c{n}", ok, detail, sk, created),
            )
    await db.commit()
    return run_id


def _entry(result, profile):
    matches = [p for p in result["providers"] if p["profile"] == profile]
    assert len(matches) == 1, result["providers"]
    return matches[0]


async def test_zero_state_is_no_data(db):
    result = await eval_staleness(db)
    assert result == {"status": "no_data", "providers": []}


async def test_missing_tables_is_no_data():
    conn = await aiosqlite.connect(":memory:")
    try:
        assert (await eval_staleness(conn))["status"] == "no_data"
    finally:
        await conn.close()


async def test_a_failed_read_is_not_reported_as_no_data(db):
    # A broken query must not read as "nothing to report".
    await db.execute("ALTER TABLE eval_runs RENAME COLUMN trigger TO trig")
    await db.commit()
    result = await eval_staleness(db)
    assert result["status"] == "error"


async def test_repointed_provider_reports_each_profile_separately(db):
    # Same provider id, old model then new model: the counts must not merge.
    await _run(db, profile="model-a", days_ago=60, passed=80, wrong=20)
    await _run(db, profile="model-b", days_ago=1, passed=6, wrong=4)
    result = await eval_staleness(db)

    assert result["total_benchmarked"] == 2
    new = _entry(result, "model-b")
    assert (new["total_passed"], new["total_failed"]) == (6, 4)
    assert new["recent"]["failure_rate"] == 0.4


async def test_superseded_profile_is_not_stale(db):
    await _run(db, profile="model-a", days_ago=60, passed=5)
    await _run(db, profile="model-b", days_ago=1, passed=5)
    result = await eval_staleness(db)

    old = _entry(result, "model-a")
    assert old["superseded"] is True
    assert old["stale"] is False
    assert _entry(result, "model-b")["superseded"] is False
    assert result["stale_count"] == 0
    assert result["superseded_count"] == 1


async def test_manual_benchmark_is_never_stale(db):
    await _run(
        db, model_id="pinned-model", profile="bench:x", trigger="manual", days_ago=70, passed=5
    )
    entry = _entry(await eval_staleness(db), "bench:x")
    assert entry["scheduled"] is False
    assert entry["stale"] is False


async def test_overdue_scheduled_profile_is_stale(db):
    await _run(db, days_ago=45, passed=5)
    result = await eval_staleness(db)
    entry = _entry(result, "model-b")
    assert entry["scheduled"] is True
    assert entry["superseded"] is False
    assert entry["stale"] is True
    assert result["stale_count"] == 1


async def test_provider_errors_count_as_infrastructural_not_wrong_answers(db):
    # 2 passed, 1 wrong answer, 3 hard timeouts recorded as failed cases.
    await _run(db, passed=2, wrong=1, provider_errors=3)
    recent = _entry(await eval_staleness(db), "model-b")["recent"]
    assert recent["failed"] == 1
    assert recent["infra_failed"] == 3
    assert recent["failure_rate"] == round(1 / 3, 2)
    assert recent["infra_rate"] == 0.5


async def test_infrastructural_failures_make_a_profile_unreliable(db):
    # Skips alone (2/6) stay under the threshold; with provider-error failures
    # the infrastructural share is 4/6 and the measurement is untrustworthy.
    await _run(db, passed=2, provider_errors=2, skipped=2)
    result = await eval_staleness(db)
    assert _entry(result, "model-b")["unreliable"] is True
    assert result["unreliable_count"] == 1


async def test_wrong_answers_alone_do_not_make_a_profile_unreliable(db):
    # An 80% wrong-answer rate is a result, not harness noise. 0.8 is above the
    # infrastructural threshold, so counting wrong answers would flip the flag.
    await _run(db, passed=2, wrong=8)
    entry = _entry(await eval_staleness(db), "model-b")
    assert entry["unreliable"] is False
    assert entry["recent"]["failure_rate"] == 0.8


async def test_rates_cover_the_window_before_the_profiles_last_run(db):
    # An old bad stretch outside the window no longer drives the rate.
    await _run(db, days_ago=50, passed=0, provider_errors=10)
    await _run(db, days_ago=1, passed=9, wrong=1)
    entry = _entry(await eval_staleness(db), "model-b")
    assert entry["recent"]["runs"] == 1
    assert entry["recent"]["infra_rate"] == 0.0
    assert entry["unreliable"] is False
    assert entry["total_failed"] == 11  # all-time totals are unchanged


async def test_failures_without_per_case_rows_stay_substantive(db):
    await _run(db, passed=3, wrong=1, provider_errors=1, write_results=False)
    recent = _entry(await eval_staleness(db), "model-b")["recent"]
    assert recent["infra_failed"] == 0
    assert recent["failed"] == 2


async def test_a_newer_manual_run_does_not_supersede_a_scheduled_profile(db):
    # Only a newer SCHEDULED profile supersedes; a manual run of another
    # profile under the same provider leaves the scheduled one overdue.
    await _run(db, profile="model-a", days_ago=60, passed=5)
    await _run(db, profile="model-b", trigger="manual", days_ago=1, passed=5)
    old = _entry(await eval_staleness(db), "model-a")
    assert old["superseded"] is False
    assert old["stale"] is True


async def test_a_manual_run_does_not_mask_a_stopped_scheduler(db):
    # The scheduled runs stopped 45 days ago; a manual run of the same
    # profile yesterday must not make it look fresh.
    await _run(db, days_ago=45, passed=5)
    await _run(db, trigger="manual", days_ago=1, passed=5)
    entry = _entry(await eval_staleness(db), "model-b")
    assert entry["days_ago"] == 1
    assert entry["stale"] is True


async def test_null_profile_is_its_own_entry(db):
    await _run(db, profile=None, passed=3, provider_errors=1)
    result = await eval_staleness(db)
    entry = _entry(result, None)
    assert entry["recent"]["infra_failed"] == 1


async def test_unreliable_count_names_only_running_profiles(db):
    # A superseded profile keeps its flag but no longer raises the count.
    await _run(db, profile="model-a", days_ago=60, passed=1, skipped=9)
    await _run(db, profile="model-b", days_ago=1, passed=5)
    result = await eval_staleness(db)
    assert _entry(result, "model-a")["unreliable"] is True
    assert result["unreliable_count"] == 0
