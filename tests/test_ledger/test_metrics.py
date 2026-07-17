"""Registry + resolver lane tests (WS-2 P1a).

Every §2.3.1 lane for the spike-validated ``reply_received`` resolver is
pinned here, plus each other metric's mechanical lanes. Resolvers get an
injected ``now`` and prediction dicts — no wall-clock dependence, no
``ledger_predictions`` rows needed (resolvers only read evidence tables).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.ledger.metrics import REGISTRY

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
PAST_DEADLINE = "2026-07-15T12:00:00+00:00"  # already passed at NOW
FUTURE_DEADLINE = "2026-07-17T12:00:00+00:00"  # still open at NOW
DAY = "2026-07-16"


def _pred(subject: str, deadline: str = FUTURE_DEADLINE, threshold: float | None = None) -> dict:
    return {"subject_ref_id": subject, "deadline_at": deadline, "threshold": threshold}


async def _resolve(metric: str, db, pred: dict):
    return await REGISTRY[metric].resolver_fn(db, pred, now=NOW)


# ── seed helpers (evidence tables from the shared fixture) ───────────────────


async def _seed_outreach(db, oid: str, *, signal=None, outcome=None, user_response=None):
    await db.execute(
        "INSERT INTO outreach_history (id, signal_type, topic, category, salience_score,"
        " channel, message_content, created_at, engagement_signal, engagement_outcome,"
        " user_response) VALUES (?, 't', 't', 'insight', 0.5, 'telegram', 'm',"
        " '2026-07-14T00:00:00+00:00', ?, ?, ?)",
        (oid, signal, outcome, user_response),
    )
    await db.commit()


async def _seed_task(db, task_id: str, phase: str):
    # task_states is trigger-guarded (intake gate) — go through the crud +
    # a real token like every other test (never raw-insert into task_states).
    from genesis.db.crud import task_states
    from genesis.db.crud.task_states import create_intake_token

    token = await create_intake_token(db)
    await task_states.create(
        db, task_id=task_id, description="d", current_phase=phase, intake_token=token
    )


async def _seed_outcome_event(db, *, ref_id: str, polarity: str):
    await db.execute(
        "INSERT INTO outcome_events (id, source, ref_type, ref_id, signal_type, signal_tier,"
        " polarity, occurred_at) VALUES (?, 'autonomy', 'task', ?, 'execution_outcome', 1, ?,"
        " '2026-07-16T10:00:00+00:00')",
        (f"oe-{ref_id}-{polarity}", ref_id, polarity),
    )
    await db.commit()


async def _seed_job_run(db, job: str, *, status: str, duration_ms=None, n=0):
    await db.execute(
        "INSERT INTO job_run_events (id, job_name, status, duration_ms, recorded_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (f"jre-{job}-{status}-{n}", job, status, duration_ms, f"{DAY}T0{n}:00:00+00:00"),
    )
    await db.commit()


async def _seed_build(db, cid: str, decision=None):
    await db.execute(
        "INSERT INTO build_candidates (id, item_key, item_title, source_file, verdict,"
        " user_decision) VALUES (?, 'k', 't', 'f', 'build', ?)",
        (cid, decision),
    )
    await db.commit()


async def _seed_proposal(db, pid: str, status: str):
    await db.execute(
        "INSERT INTO ego_proposals (id, action_type, content, created_at, status)"
        " VALUES (?, 'research', 'c', '2026-07-16T00:00:00+00:00', ?)",
        (pid, status),
    )
    await db.commit()


# ── registry shape ───────────────────────────────────────────────────────────


def test_registry_is_exactly_the_v1_set():
    assert set(REGISTRY) == {
        "reply_received",
        "positive_engagement",
        "completed",
        "completed_first_attempt",
        "acceptance_pass",
        "runs_clean_day",
        "runtime_ms_le",
        "user_greenlights",
        "approved_and_executes",
    }


def test_registry_specs_are_coherent():
    ddl_classes = {
        "outreach_send",
        "task_execution",
        "scheduled_job",
        "build_verdict",
        "ego_proposal",
    }
    for name, spec in REGISTRY.items():
        assert spec.action_class in ddl_classes, name
        assert spec.comparator_domain <= {"is_true", "le", "ge"} and spec.comparator_domain, name
        assert spec.absence_semantics in ("zero", "void", "fuzzy_pending"), name
        assert spec.default_horizon > timedelta(0), name
        assert callable(spec.resolver_fn), name
    assert REGISTRY["acceptance_pass"].fuzzy is True
    assert REGISTRY["runtime_ms_le"].comparator_domain == {"le"}


def test_no_llm_import_path():
    """Importing the ledger must not pull ``genesis.routing`` (the no-LLM
    lock — the P2 grader inherits it). Checked in a fresh interpreter because
    this test process has long since imported routing via other suites."""
    import subprocess
    import sys

    code = (
        "import sys; import genesis.ledger.metrics; "
        "bad = sorted(n for n in sys.modules if n.startswith('genesis.routing')); "
        "print('pulled:', bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


# ── reply_received: every §2.3.1 lane ────────────────────────────────────────


@pytest.mark.parametrize(
    ("seed", "expected_outcome", "expected_lane"),
    [
        ({"signal": "user_reply"}, 1, "user_reply"),
        ({"user_response": "sounds good"}, 1, "user_reply"),
        ({"outcome": "useful"}, 1, "legacy_outcome:useful"),  # pre-signal-column reply row
        ({"signal": "timeout", "outcome": "ignored"}, 0, "signal:timeout"),
        ({"signal": "implicit_activity"}, 0, "signal:implicit_activity"),
        ({"signal": "auto_digest"}, 0, "signal:auto_digest"),
        ({"signal": "acted_on"}, 0, "signal:acted_on"),
        ({"signal": "acknowledged"}, 0, "signal:acknowledged"),
        ({"outcome": "ambivalent"}, 0, "outcome:ambivalent"),
        ({"outcome": "ignored"}, 0, "outcome:ignored"),
    ],
)
async def test_reply_received_signal_lanes(db, seed, expected_outcome, expected_lane):
    await _seed_outreach(db, "o-1", **seed)
    res = await _resolve("reply_received", db, _pred("o-1"))
    assert (res.outcome_value, res.lane) == (expected_outcome, expected_lane)
    assert res.evidence_ref == "outreach_history:o-1"


async def test_reply_received_absence_and_open(db):
    await _seed_outreach(db, "o-silent")
    past = await _resolve("reply_received", db, _pred("o-silent", PAST_DEADLINE))
    assert (past.outcome_value, past.lane) == (0, "mechanical_absence")
    open_ = await _resolve("reply_received", db, _pred("o-silent", FUTURE_DEADLINE))
    assert (open_.outcome_value, open_.lane) == (None, "open")


async def test_reply_received_signal_beats_outcome_label(db):
    """A captured reply wins even when an earlier label said ambivalent."""
    await _seed_outreach(db, "o-up", signal="user_reply", outcome="ambivalent")
    res = await _resolve("reply_received", db, _pred("o-up"))
    assert (res.outcome_value, res.lane) == (1, "user_reply")


async def test_reply_received_broken_evidence(db):
    missing = await _resolve("reply_received", db, _pred("o-ghost"))
    assert (missing.outcome_value, missing.lane) == (None, "unresolvable:subject_missing")
    await _seed_outreach(db, "o-bad")
    bad = await _resolve("reply_received", db, _pred("o-bad", "not-a-date"))
    assert (bad.outcome_value, bad.lane) == (None, "unresolvable:bad_deadline")


# ── positive_engagement ──────────────────────────────────────────────────────


@pytest.mark.parametrize("outcome", ["useful", "engaged", "acted_on", "acknowledged"])
async def test_positive_engagement_canonical_positives(db, outcome):
    await _seed_outreach(db, "o-pos", outcome=outcome)
    res = await _resolve("positive_engagement", db, _pred("o-pos"))
    assert (res.outcome_value, res.lane) == (1, f"positive_outcome:{outcome}")


async def test_positive_engagement_negative_label_waits_for_deadline(db):
    """record_reply upgrades an early 'ignored' unconditionally — only the
    deadline closes the question."""
    await _seed_outreach(db, "o-ig", signal="timeout", outcome="ignored")
    open_ = await _resolve("positive_engagement", db, _pred("o-ig", FUTURE_DEADLINE))
    assert (open_.outcome_value, open_.lane) == (None, "open")
    past = await _resolve("positive_engagement", db, _pred("o-ig", PAST_DEADLINE))
    assert (past.outcome_value, past.lane) == (0, "mechanical_absence")


# ── task_execution ───────────────────────────────────────────────────────────


async def test_completed_lanes(db):
    await _seed_task(db, "t-done", "completed")
    assert (await _resolve("completed", db, _pred("t-done"))).outcome_value == 1
    await _seed_task(db, "t-fail", "failed")
    res = await _resolve("completed", db, _pred("t-fail"))
    assert (res.outcome_value, res.lane) == (0, "phase:failed")
    await _seed_task(db, "t-run", "executing")
    assert (await _resolve("completed", db, _pred("t-run", FUTURE_DEADLINE))).lane == "open"
    late = await _resolve("completed", db, _pred("t-run", PAST_DEADLINE))
    assert (late.outcome_value, late.lane) == (0, "mechanical_absence")
    ghost = await _resolve("completed", db, _pred("t-ghost"))
    assert ghost.lane == "unresolvable:subject_missing"


async def test_completed_first_attempt(db):
    await _seed_task(db, "t-clean", "completed")
    clean = await _resolve("completed_first_attempt", db, _pred("t-clean"))
    assert (clean.outcome_value, clean.lane) == (1, "completed_first_attempt")

    await _seed_task(db, "t-retry", "completed")
    await _seed_outcome_event(db, ref_id="t-retry", polarity="negative")
    retried = await _resolve("completed_first_attempt", db, _pred("t-retry"))
    assert (retried.outcome_value, retried.lane) == (0, "failed_attempt_evidence")

    # a positive event is not a failed attempt
    await _seed_task(db, "t-pos", "completed")
    await _seed_outcome_event(db, ref_id="t-pos", polarity="positive")
    assert (await _resolve("completed_first_attempt", db, _pred("t-pos"))).outcome_value == 1

    # non-completed passes the base resolution through
    await _seed_task(db, "t-nf", "failed")
    assert (await _resolve("completed_first_attempt", db, _pred("t-nf"))).lane == "phase:failed"


async def test_acceptance_pass_is_fuzzy(db):
    res = await _resolve("acceptance_pass", db, _pred("t-any"))
    assert (res.outcome_value, res.evidence_ref, res.lane) == (None, None, "fuzzy_pending")


# ── scheduled_job ────────────────────────────────────────────────────────────


async def test_runs_clean_day_lanes(db):
    subject = f"dream_job:{DAY}"
    # no runs, deadline passed → void (premise absent, separately alarmed)
    void = await _resolve("runs_clean_day", db, _pred(subject, PAST_DEADLINE))
    assert (void.outcome_value, void.lane) == (None, "void:no_runs")

    await _seed_job_run(db, "dream_job", status="success", n=1)
    open_ = await _resolve("runs_clean_day", db, _pred(subject, FUTURE_DEADLINE))
    assert open_.lane == "open"
    clean = await _resolve("runs_clean_day", db, _pred(subject, PAST_DEADLINE))
    assert (clean.outcome_value, clean.lane) == (1, "clean_day")

    # any failed run is conclusive regardless of deadline
    await _seed_job_run(db, "dream_job", status="failed", n=2)
    failed = await _resolve("runs_clean_day", db, _pred(subject, FUTURE_DEADLINE))
    assert (failed.outcome_value, failed.lane) == (0, "failed_runs")

    bad = await _resolve("runs_clean_day", db, _pred("no-day-here"))
    assert bad.lane == "unresolvable:bad_subject_ref"


async def test_runtime_ms_le_lanes(db):
    subject = f"dream_job:{DAY}"
    missing = await _resolve("runtime_ms_le", db, _pred(subject, PAST_DEADLINE))
    assert missing.lane == "unresolvable:missing_threshold"

    # no measured durations → void (honest instrument, never a free pass)
    void = await _resolve("runtime_ms_le", db, _pred(subject, PAST_DEADLINE, threshold=5000))
    assert (void.outcome_value, void.lane) == (None, "void:no_duration_data")

    await _seed_job_run(db, "dream_job", status="success", duration_ms=3000, n=1)
    within = await _resolve("runtime_ms_le", db, _pred(subject, PAST_DEADLINE, threshold=5000))
    assert (within.outcome_value, within.lane) == (1, "within_threshold")

    await _seed_job_run(db, "dream_job", status="success", duration_ms=9000, n=2)
    over = await _resolve("runtime_ms_le", db, _pred(subject, FUTURE_DEADLINE, threshold=5000))
    assert (over.outcome_value, over.lane) == (0, "exceeded_threshold")


# ── build_verdict / ego_proposal ─────────────────────────────────────────────


async def test_user_greenlights_lanes(db):
    await _seed_build(db, "b-yes", "approved")
    assert (await _resolve("user_greenlights", db, _pred("b-yes"))).outcome_value == 1
    await _seed_build(db, "b-no", "rejected")
    res = await _resolve("user_greenlights", db, _pred("b-no"))
    assert (res.outcome_value, res.lane) == (0, "decision:rejected")
    await _seed_build(db, "b-wait")
    assert (await _resolve("user_greenlights", db, _pred("b-wait", FUTURE_DEADLINE))).lane == "open"
    late = await _resolve("user_greenlights", db, _pred("b-wait", PAST_DEADLINE))
    assert (late.outcome_value, late.lane) == (0, "mechanical_absence")


async def test_approved_and_executes_lanes(db):
    await _seed_proposal(db, "p-exec", "executed")
    assert (await _resolve("approved_and_executes", db, _pred("p-exec"))).outcome_value == 1
    await _seed_proposal(db, "p-rej", "rejected")
    res = await _resolve("approved_and_executes", db, _pred("p-rej"))
    assert (res.outcome_value, res.lane) == (0, "status:rejected")
    # approved-but-not-executed: the metric is approved AND executes
    await _seed_proposal(db, "p-appr", "approved")
    assert (
        await _resolve("approved_and_executes", db, _pred("p-appr", FUTURE_DEADLINE))
    ).lane == "open"
    late = await _resolve("approved_and_executes", db, _pred("p-appr", PAST_DEADLINE))
    assert (late.outcome_value, late.lane) == (0, "mechanical_absence")
