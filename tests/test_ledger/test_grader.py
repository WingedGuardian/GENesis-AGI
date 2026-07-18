"""WS-2 P2 grader tests: the lane→status mapping matrix (keyed on
outcome_value), registry-vanish + resolver-exception alarms, idempotent
double-grade, and the no-LLM import lock.

Rows are created via the real CRUD (which rejects past deadlines), so each
prediction is born with a near-future deadline relative to ``BASE`` and graded
with an injected ``now`` past it — one frozen clock drives list_due_open and
every resolver, zero wall-clock dependence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import ledger_predictions as lp
from genesis.ledger import grader
from genesis.ledger.grader import grade_due_predictions
from genesis.ledger.metrics import REGISTRY

BASE = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
AFTER = BASE + timedelta(hours=2)  # past a BASE+1h deadline
DAY = "2026-07-16"


async def _predict(db, *, action_class, subject_type, subject, metric, domain, confidence=0.5):
    return await lp.create(
        db,
        action_class=action_class,
        subject_ref_type=subject_type,
        subject_ref_id=subject,
        domain=domain,
        metric=metric,
        confidence=confidence,
        deadline_at=(BASE + timedelta(hours=1)).isoformat(),
        provenance="policy_prior",
        predictor="test",
        now=BASE,
    )


async def _seed_outreach(db, oid, *, signal=None, outcome=None, user_response=None):
    await db.execute(
        "INSERT INTO outreach_history (id, signal_type, topic, category, salience_score,"
        " channel, message_content, created_at, engagement_signal, engagement_outcome,"
        " user_response) VALUES (?, 't', 't', 'insight', 0.5, 'telegram', 'm',"
        " '2026-07-14T00:00:00+00:00', ?, ?, ?)",
        (oid, signal, outcome, user_response),
    )
    await db.commit()


async def _seed_task(db, task_id, phase):
    from genesis.db.crud import task_states
    from genesis.db.crud.task_states import create_intake_token

    token = await create_intake_token(db)
    await task_states.create(
        db, task_id=task_id, description="d", current_phase=phase, intake_token=token
    )


# ── the outcome_value-keyed mapping matrix ───────────────────────────────────


async def test_affirmative_mechanical_resolves_1(db):
    await _seed_outreach(db, "o-yes", signal="user_reply")
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-yes",
        metric="reply_received",
        domain="outreach.insight",
    )
    report = await grade_due_predictions(db, now=AFTER)
    row = await lp.get_by_id(
        db,
        (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-yes"))[0][
            "id"
        ],
    )
    assert row["status"] == "resolved"
    assert row["outcome_value"] == 1
    assert row["resolver"] == "mechanical"
    assert row["evidence_ref"] == "outreach_history:o-yes"
    assert row["brier"] == pytest.approx((0.5 - 1) ** 2)
    assert report.resolved == 1 and report.mechanical == 1 and report.absence == 0


async def test_mechanical_absence_resolves_0(db):
    await _seed_outreach(db, "o-silent")  # exists, no signal/outcome
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-silent",
        metric="reply_received",
        domain="outreach.insight",
    )
    report = await grade_due_predictions(db, now=AFTER)
    row = (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-silent"))[0]
    assert row["status"] == "resolved"
    assert row["outcome_value"] == 0
    assert row["resolver"] == "mechanical_absence"
    assert report.absence == 1 and report.mechanical == 0


async def test_negative_mechanical_resolves_0(db):
    await _seed_outreach(db, "o-to", signal="timeout")
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-to",
        metric="reply_received",
        domain="outreach.insight",
    )
    await grade_due_predictions(db, now=AFTER)
    row = (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-to"))[0]
    assert row["status"] == "resolved" and row["outcome_value"] == 0
    assert row["resolver"] == "mechanical"  # a real signal fired, not absence


async def test_void_lane(db):
    # scheduled_job with no job_run_events that day → void:no_runs.
    await _predict(
        db,
        action_class="scheduled_job",
        subject_type="job_day",
        subject=f"somejob:{DAY}",
        metric="runs_clean_day",
        domain="job.somejob",
    )
    report = await grade_due_predictions(db, now=AFTER)
    row = (
        await lp.list_by_subject(db, action_class="scheduled_job", subject_ref_id=f"somejob:{DAY}")
    )[0]
    assert row["status"] == "void"
    assert row["outcome_value"] is None
    assert report.void == 1 and report.resolved == 0


async def test_unresolvable_subject_missing(db):
    # No outreach_history row for the subject → unresolvable:subject_missing.
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-ghost",
        metric="reply_received",
        domain="outreach.insight",
    )
    report = await grade_due_predictions(db, now=AFTER)
    row = (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-ghost"))[0]
    assert row["status"] == "unresolvable" and row["outcome_value"] is None
    assert report.unresolvable == 1
    # subject_missing is a data condition, NOT a code fault → no alarm.
    assert grader.grade_failure_counts()["metric_vanished"] == {}


async def test_fuzzy_pending_parked(db):
    await _seed_task(db, "t-fuzzy", "completed")
    await _predict(
        db,
        action_class="task_execution",
        subject_type="task",
        subject="t-fuzzy",
        metric="acceptance_pass",
        domain="task.x",
    )
    report = await grade_due_predictions(db, now=AFTER)
    row = (await lp.list_by_subject(db, action_class="task_execution", subject_ref_id="t-fuzzy"))[0]
    assert row["status"] == "fuzzy_pending" and row["outcome_value"] is None
    assert report.fuzzy_pending == 1


# ── alarm sensors ────────────────────────────────────────────────────────────


async def test_registry_vanished_alarms(db, monkeypatch):
    await _seed_outreach(db, "o-v", signal="user_reply")
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-v",
        metric="reply_received",
        domain="outreach.insight",
    )
    # Simulate a code rollback: the metric is gone from the registry at grade
    # time. The grader must mark the row unresolvable and alarm, never skip.
    monkeypatch.setattr(grader, "REGISTRY", {})
    report = await grade_due_predictions(db, now=AFTER)
    row = (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-v"))[0]
    assert row["status"] == "unresolvable"
    assert report.unresolvable == 1
    assert grader.grade_failure_counts()["metric_vanished"] == {"outreach_send": 1}


async def test_resolver_exception_isolated_and_counted(db, monkeypatch):
    await _seed_outreach(db, "o-boom", signal="user_reply")
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-boom",
        metric="reply_received",
        domain="outreach.insight",
    )

    async def _boom(*a, **k):
        raise RuntimeError("resolver blew up")

    bad = replace(REGISTRY["reply_received"], resolver_fn=_boom)
    monkeypatch.setattr(grader, "REGISTRY", {"reply_received": bad})
    # Must NOT raise — the batch swallows the resolver error.
    report = await grade_due_predictions(db, now=AFTER)
    row = (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-boom"))[0]
    assert row["status"] == "open"  # left open, not graded
    assert report.left_open == 1
    assert grader.grade_failure_counts()["grade_failed"] == {"outreach_send": 1}


async def test_batch_continues_past_one_bad_resolver(db, monkeypatch):
    await _seed_outreach(db, "o-good", signal="user_reply")
    await _seed_task(db, "t-good", "completed")
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-good",
        metric="reply_received",
        domain="outreach.insight",
    )
    await _predict(
        db,
        action_class="task_execution",
        subject_type="task",
        subject="t-good",
        metric="completed",
        domain="task.x",
    )

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    reg = dict(REGISTRY)
    reg["reply_received"] = replace(REGISTRY["reply_received"], resolver_fn=_boom)
    monkeypatch.setattr(grader, "REGISTRY", reg)
    report = await grade_due_predictions(db, now=AFTER)
    # The good task row still graded despite the bad outreach resolver.
    task_row = (
        await lp.list_by_subject(db, action_class="task_execution", subject_ref_id="t-good")
    )[0]
    assert task_row["status"] == "resolved" and task_row["outcome_value"] == 1
    assert report.mechanical == 1 and report.left_open == 1


# ── idempotence + empty state ────────────────────────────────────────────────


async def test_double_grade_is_noop(db):
    await _seed_outreach(db, "o-idem", signal="user_reply")
    await _predict(
        db,
        action_class="outreach_send",
        subject_type="outreach",
        subject="o-idem",
        metric="reply_received",
        domain="outreach.insight",
    )
    first = await grade_due_predictions(db, now=AFTER)
    second = await grade_due_predictions(db, now=AFTER)
    assert first.resolved == 1
    assert second.scanned == 0  # resolved row no longer open → not re-listed
    row = (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-idem"))[0]
    assert row["status"] == "resolved" and row["outcome_value"] == 1


async def test_default_now_grades_absence_rows(db):
    """The production call site passes no ``now`` — the grader must default it,
    or every absence-path row TypeErrors into grade_failed (regression: the
    resolvers' _past_deadline does ``now >= deadline`` with no fallback).

    Deterministic without wall-clock coupling: the row is born with a fixed
    deadline (2026-07-16T13:00) that is monotonically in the past for any real
    run, so the defaulted real ``now`` always sees it as due + past-deadline.
    """
    await _seed_outreach(db, "o-def")  # exists, silent → absence path
    await lp.create(
        db,
        action_class="outreach_send",
        subject_ref_type="outreach",
        subject_ref_id="o-def",
        domain="outreach.insight",
        metric="reply_received",
        confidence=0.5,
        deadline_at=(BASE + timedelta(hours=1)).isoformat(),
        provenance="policy_prior",
        predictor="test",
        now=BASE,
    )
    report = await grade_due_predictions(db)  # NO now= — the production path
    row = (await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-def"))[0]
    assert row["status"] == "resolved" and row["resolver"] == "mechanical_absence"
    assert report.absence == 1
    assert grader.grade_failure_counts()["grade_failed"] == {}  # no TypeError swallowed


async def test_empty_state_clean_noop(db):
    report = await grade_due_predictions(db, now=AFTER)
    assert report.scanned == 0 and report.resolved == 0
    assert report.mechanical_share() is None


async def test_grade_report_summary_and_share(db):
    await _seed_outreach(db, "o-a", signal="user_reply")
    await _seed_outreach(db, "o-b")  # silent → absence
    for oid in ("o-a", "o-b"):
        await _predict(
            db,
            action_class="outreach_send",
            subject_type="outreach",
            subject=oid,
            metric="reply_received",
            domain="outreach.insight",
        )
    report = await grade_due_predictions(db, now=AFTER)
    assert report.resolved == 2
    assert report.mechanical_share() == 1.0  # both graded mechanically
    assert "mechanical_share=1.00" in report.summary()


# ── the no-LLM import lock (the P2 grader's core invariant) ───────────────────


def test_no_llm_import_path():
    """Importing the grader must not pull ``genesis.routing`` — the mechanical
    path makes ZERO LLM calls. Checked in a fresh interpreter (this test
    process imported routing via other suites long ago)."""
    import subprocess
    import sys

    code = (
        "import sys; import genesis.ledger.grader; "
        "bad = sorted(n for n in sys.modules if n.startswith('genesis.routing')); "
        "print('pulled:', bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


# ── P2b autonomy feed: failure-only + shadow-first (into the autonomy Trap) ───


async def _task_pred(db, tid, metric="completed"):
    await lp.create(
        db,
        action_class="task_execution",
        subject_ref_type="task",
        subject_ref_id=tid,
        domain="task.x",
        metric=metric,
        confidence=0.5,
        deadline_at=(BASE + timedelta(hours=1)).isoformat(),
        provenance="policy_prior",
        predictor="test",
        now=BASE,
    )


def _mgr():
    m = AsyncMock()
    m.record_success = AsyncMock()
    m.record_correction = AsyncMock()
    return m


async def test_autonomy_shadow_logs_but_fires_nothing(db, monkeypatch):
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "shadow")
    await _seed_task(db, "t-shadow", "completed")
    await _task_pred(db, "t-shadow")
    mgr = _mgr()
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    assert report.autonomy["shadow:success"] == 1
    mgr.record_success.assert_not_awaited()  # shadow writes nothing to the Trap
    mgr.record_correction.assert_not_awaited()


async def test_autonomy_live_success_on_completion(db, monkeypatch):
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-live", "completed")
    await _task_pred(db, "t-live")
    mgr = _mgr()
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    assert report.autonomy["live:success"] == 1
    mgr.record_success.assert_awaited_once_with("direct_session")
    mgr.record_correction.assert_not_awaited()


async def test_autonomy_live_correction_on_genuine_failure(db, monkeypatch):
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-fail", "failed")
    await _task_pred(db, "t-fail")
    mgr = _mgr()
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    assert report.autonomy["live:correction"] == 1
    mgr.record_correction.assert_awaited_once()
    assert mgr.record_correction.call_args[0][0] == "direct_session"
    mgr.record_success.assert_not_awaited()


async def test_autonomy_no_fire_on_cancelled(db, monkeypatch):
    # A cancelled task grades 0 (phase:cancelled) but is NOT a competence signal.
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-cancel", "cancelled")
    await _task_pred(db, "t-cancel")
    mgr = _mgr()
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    assert not report.autonomy
    mgr.record_success.assert_not_awaited()
    mgr.record_correction.assert_not_awaited()


async def test_autonomy_no_fire_on_deadline_miss(db, monkeypatch):
    # Still running at deadline → mechanical_absence: slowness, not failure.
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-slow", "dispatching")
    await _task_pred(db, "t-slow")
    mgr = _mgr()
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    assert report.absence == 1 and not report.autonomy
    mgr.record_success.assert_not_awaited()
    mgr.record_correction.assert_not_awaited()


async def test_autonomy_off_fires_nothing(db, monkeypatch):
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "off")
    await _seed_task(db, "t-off", "completed")
    await _task_pred(db, "t-off")
    mgr = _mgr()
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    assert not report.autonomy
    mgr.record_success.assert_not_awaited()


async def test_autonomy_only_completed_metric_not_first_attempt(db, monkeypatch):
    # completed_first_attempt must NOT feed autonomy (one event per task).
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-cfa", "completed")
    await _task_pred(db, "t-cfa", metric="completed_first_attempt")
    mgr = _mgr()
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    assert report.resolved == 1 and not report.autonomy  # graded, no autonomy
    mgr.record_success.assert_not_awaited()


async def test_autonomy_live_no_manager_does_not_overcount(db, monkeypatch):
    # live mode but no manager wired: nothing fires AND report.autonomy must not
    # claim a fire (else it hides a wiring regression — architect NOTE-1).
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-nomgr", "completed")
    await _task_pred(db, "t-nomgr")
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=None)
    assert report.mechanical == 1  # graded fine
    assert not report.autonomy  # but nothing counted as fired


async def test_autonomy_exactly_once_across_regrade(db, monkeypatch):
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-once", "completed")
    await _task_pred(db, "t-once")
    mgr = _mgr()
    await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)  # resolved row not re-listed
    mgr.record_success.assert_awaited_once()  # fired once, not per pass


async def test_autonomy_manager_failure_never_touches_grade(db, monkeypatch):
    monkeypatch.setattr(grader, "autonomy_feed_mode", lambda: "live")
    await _seed_task(db, "t-boom", "completed")
    await _task_pred(db, "t-boom")
    mgr = _mgr()
    mgr.record_success = AsyncMock(side_effect=RuntimeError("autonomy store down"))
    report = await grade_due_predictions(db, now=AFTER, autonomy_manager=mgr)
    row = (await lp.list_by_subject(db, action_class="task_execution", subject_ref_id="t-boom"))[0]
    assert row["status"] == "resolved" and row["outcome_value"] == 1  # grade still landed
    assert report.mechanical == 1
    assert grader.autonomy_feed_failure_counts() == {"success": 1}
