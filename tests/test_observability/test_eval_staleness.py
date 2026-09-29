"""Tests for the eval_staleness health snapshot.

Covers the three derivations the snapshot documents: one entry per
(model_id, profile), infrastructural vs substantive failure, and staleness only
for a profile something is expected to re-run.
"""

from __future__ import annotations

import importlib
import itertools
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


async def test_stale_profile_reports_the_scheduled_age_the_verdict_used(db):
    # days_ago follows the manual run; the stale verdict follows the scheduler,
    # and a reader needs the number the verdict was taken on.
    await _run(db, days_ago=45, passed=5)
    await _run(db, trigger="manual", days_ago=1, passed=5)
    entry = _entry(await eval_staleness(db), "model-b")
    assert entry["stale"] is True
    assert entry["days_ago"] == 1
    assert entry["days_since_scheduled"] == 45


async def test_manual_profile_has_no_scheduled_age(db):
    await _run(db, profile="bench:x", trigger="manual", days_ago=3, passed=5)
    assert _entry(await eval_staleness(db), "bench:x")["days_since_scheduled"] is None


async def test_missing_results_table_beside_recorded_runs_is_an_error(db):
    # eval_runs holds runs, so "no data" would hide a broken read.
    await _run(db, passed=5)
    await db.execute("DROP TABLE eval_results")
    await db.commit()
    result = await eval_staleness(db)
    assert result["status"] == "error"
    assert result["error"] == "OperationalError"


async def test_missing_results_table_on_empty_runs_is_still_an_error(db):
    # Half the schema is present: a broken install, not a fresh one.
    await db.execute("DROP TABLE eval_results")
    await db.commit()
    assert (await eval_staleness(db))["status"] == "error"


# ── Dashboard rendering of every snapshot state ────────────────────────────


async def _migrated():
    conn = await aiosqlite.connect(":memory:")
    for mig in _MIGRATIONS:
        await importlib.import_module(f"genesis.db.migrations.{mig}").up(conn)
    await conn.commit()
    return conn


async def _snapshot_states():
    """One real eval_staleness() output per state the snapshot can emit."""
    states = {"no_db": await eval_staleness(None)}

    conn = await aiosqlite.connect(":memory:")
    states["no_data_no_tables"] = await eval_staleness(conn)
    await conn.close()

    conn = await _migrated()
    states["no_data_empty"] = await eval_staleness(conn)
    await _run(conn, passed=5)
    await conn.execute("DROP TABLE eval_results")
    await conn.commit()
    states["error"] = await eval_staleness(conn)
    await conn.close()

    conn = await _migrated()
    await _run(conn, model_id="fresh", profile="p-fresh", days_ago=2, passed=8, wrong=2)
    await _run(conn, model_id="aging", profile="p-aging", days_ago=20, passed=5)
    await _run(conn, model_id="masked", profile="p-masked", days_ago=45, passed=5)
    await _run(conn, model_id="masked", profile="p-masked", trigger="manual", days_ago=1, passed=5)
    await _run(conn, model_id="stopped", profile="p-stopped", days_ago=40, passed=5)
    await _run(conn, model_id="manual", profile="p-manual", trigger="manual", days_ago=3, passed=5)
    await _run(conn, model_id="moved", profile="p-old", days_ago=60, passed=5)
    await _run(conn, model_id="moved", profile="p-new", days_ago=1, passed=4, wrong=1)
    await _run(
        conn, model_id="outage", profile="p-outage", days_ago=1, provider_errors=3, skipped=2
    )
    states["ok"] = await eval_staleness(conn)
    await conn.close()
    return states


def _render(states):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to execute the dashboard view-models")
    harness = Path(__file__).resolve().parents[2] / "tests" / "assets" / "eval_render_check.js"
    r = subprocess.run(
        [node, str(harness)], input=json.dumps(states), capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _without_label(view):
    return json.dumps({k: v for k, v in view.items() if k != "label"}, sort_keys=True)


async def test_every_snapshot_status_renders_distinctly():
    states = await _snapshot_states()
    statuses = {name: s["status"] for name, s in states.items()}
    # Guard the fixture: every status the snapshot can emit is present.
    assert set(statuses.values()) == {"no_db", "no_data", "error", "ok"}, statuses
    out = _render(states)

    assert out["ok"]["notice"] is None
    notices = {statuses[n]: out[n]["notice"]["text"] for n in out if statuses[n] != "ok"}
    assert len(set(notices.values())) == len(notices), notices
    assert out["no_data_no_tables"]["notice"] == out["no_data_empty"]["notice"]
    assert "failed" in out["error"]["notice"]["text"]
    assert out["error"]["notice"]["color"] == "#ef4444"
    assert "failed" not in out["no_data_empty"]["notice"]["text"]
    assert "database" in out["no_db"]["notice"]["text"]


async def test_every_profile_state_renders_distinctly():
    snap = (await _snapshot_states())["ok"]
    views = dict(
        zip(
            [p["profile"] for p in snap["providers"]],
            _render({"ok": snap})["ok"]["profiles"],
            strict=True,
        )
    )
    # Guard the fixture: each profile is in the state its name claims.
    by = {p["profile"]: p for p in snap["providers"]}
    assert by["p-masked"]["stale"] and by["p-masked"]["days_ago"] == 1
    assert by["p-stopped"]["stale"] and not by["p-fresh"]["stale"]
    assert by["p-old"]["superseded"] and by["p-manual"]["scheduled"] is False
    assert by["p-outage"]["recent"]["passed"] + by["p-outage"]["recent"]["failed"] == 0
    assert by["p-outage"]["unreliable"] is True

    # Stale behind a manual run: the scheduled age, not the manual run's.
    masked = views["p-masked"]
    assert masked["age"].startswith("45d since scheduled run"), masked
    assert "last run 1d ago" in masked["age"]
    assert masked["ageColor"] == "#ef4444"
    assert views["p-stopped"]["ageColor"] == "#ef4444"

    # Approaching overdue is judged on the scheduled age too.
    assert views["p-aging"]["ageColor"] == "#fbbf24"
    assert views["p-fresh"]["ageColor"] == "#4ade80"
    assert views["p-manual"]["age"].endswith("manual")
    assert views["p-old"]["age"].endswith("superseded")

    # A fully unscored window still shows its infrastructure rate and counts.
    outage = views["p-outage"]
    assert outage["fail"] is None
    assert outage["infra"]["text"] == "infra 100%"
    assert "2 skipped + 3 provider errors of 5 cases" in outage["infra"]["title"]
    assert outage["unscored"] is not None
    assert outage["unreliable"] is True

    # A scored window shows its wrong-answer rate and is not "unscored".
    assert views["p-fresh"]["fail"]["text"] == "fail 20%"
    assert views["p-fresh"]["unscored"] is None and views["p-fresh"]["infra"] is None

    # Distinctness: no two profile states render identically once the label
    # (which is only the name) is set aside.
    rendered = {name: _without_label(v) for name, v in views.items()}
    assert len(set(rendered.values())) == len(rendered), rendered


def test_the_eval_section_renders_through_the_view_models():
    # The view-models above are only exercised if the DOM loop uses them, and
    # a notice only shows if it can open the section on its own.
    src = (
        Path(__file__).resolve().parents[2] / "src/genesis/dashboard/templates/neural_monitor.html"
    ).read_text()
    loop = src[src.index("async function loadSurplusDetail") :]
    loop = loop[: loop.index("// Recent eval run scores")]
    assert "evalSnapshotNotice(data.eval_staleness)" in loop
    assert "if (evalNotice || providers.length > 0" in loop
    assert "evalProfileView(p)" in loop
    assert "for (const part of [v.fail, v.infra, v.unscored, v.unreadable])" in loop


async def test_missing_runs_table_beside_results_is_an_error(db):
    # The mirror image of a missing eval_results: half the schema is present.
    await db.execute("DROP TABLE eval_runs")
    await db.commit()
    assert (await eval_staleness(db))["status"] == "error"


async def test_an_unreadable_table_probe_is_an_error(db, monkeypatch):
    # When absence cannot be established, the read is reported as failed.
    # import_module, not ``import … as``: the package re-exports the FUNCTION
    # under the module's name, so attribute access would return the function.
    mod = importlib.import_module("genesis.observability.snapshots.eval_staleness")

    async def _unknown(_db, _name):
        return None

    await db.execute("DROP TABLE eval_results")
    await db.execute("DROP TABLE eval_runs")
    await db.commit()
    monkeypatch.setattr(mod, "_table_exists", _unknown)
    assert (await eval_staleness(db))["status"] == "error"


async def test_an_unparseable_scheduled_timestamp_is_unknown_not_dropped(db):
    # julianday() is NULL for it: the profile must still be reported, with an
    # unknown scheduled age, never silently dropped or given days_ago instead.
    run_id = await _run(db, profile="p-garbled", days_ago=45, passed=5)
    await db.execute("UPDATE eval_runs SET created_at = 'not-a-date' WHERE id = ?", (run_id,))
    await db.commit()
    entry = _entry(await eval_staleness(db), "p-garbled")
    assert entry["days_since_scheduled"] is None
    assert entry["days_ago"] is None  # unknown, not "today"
    assert entry["unreadable_runs"] == 1
    assert entry["stale"] is False
    view = _render({"s": {"status": "ok", "providers": [entry]}})["s"]["profiles"][0]
    assert view["age"] == "scheduled run age unknown", view
    assert view["ageColor"] == "#fbbf24"
    assert view["unreadable"]["text"] == "1 unreadable"
    assert "no readable run timestamp" in view["unscored"]["title"]


@pytest.mark.parametrize("garbage", ["not-a-date", "now", "2461000.5"])
async def test_a_garbled_sibling_does_not_supersede_a_stale_profile(db, garbage):
    # Text MAX sorts 'not-a-date' above any ISO date, and julianday() reads
    # 'now' and bare numbers as times; none may pose as the provider's newest
    # scheduled run and silence a real stale alarm.
    await _run(db, profile="p-real", days_ago=45, passed=5)
    garbled = await _run(db, profile="p-garbled", days_ago=1, passed=5)
    await db.execute("UPDATE eval_runs SET created_at = ? WHERE id = ?", (garbage, garbled))
    await db.commit()
    result = await eval_staleness(db)
    real = _entry(result, "p-real")
    assert (real["stale"], real["superseded"]) == (True, False)
    assert result["stale_count"] == 1
    assert _entry(result, "p-garbled")["unreadable_runs"] == 1


async def test_a_garbled_row_does_not_hide_the_readable_runs(db):
    await _run(db, days_ago=45, passed=5)
    garbled = await _run(db, days_ago=1, passed=5)
    await db.execute("UPDATE eval_runs SET created_at = 'not-a-date' WHERE id = ?", (garbled,))
    await db.commit()
    entry = _entry(await eval_staleness(db), "model-b")
    assert (entry["days_ago"], entry["days_since_scheduled"]) == (45, 45)
    assert entry["stale"] is True
    assert entry["unreadable_runs"] == 1
    assert entry["recent"]["runs"] == 1


async def test_approaching_overdue_uses_the_scheduled_age_behind_a_manual_run(db):
    # Scheduled 20 days ago, manual yesterday: amber (scheduler aging), not
    # green (which the manual run's age alone would give).
    await _run(db, profile="p-aging-masked", days_ago=20, passed=5)
    await _run(db, profile="p-aging-masked", trigger="manual", days_ago=1, passed=5)
    entry = _entry(await eval_staleness(db), "p-aging-masked")
    assert (entry["days_since_scheduled"], entry["days_ago"], entry["stale"]) == (20, 1, False)
    view = _render({"s": {"status": "ok", "providers": [entry]}})["s"]["profiles"][0]
    assert view["ageColor"] == "#fbbf24", view
    assert view["age"] == "20d since scheduled run · last run 1d ago"
