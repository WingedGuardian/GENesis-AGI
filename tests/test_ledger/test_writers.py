"""WS-2 P1b writer-hook tests: rows written per action class, the
fire-and-forget contract (a hook failure never escapes), dedupe re-entry,
and the health-alert surfacing of the failure counter."""

from __future__ import annotations

import pytest

from genesis.db.crud import ledger_predictions as lp
from genesis.ledger import writers


@pytest.fixture(autouse=True)
def _clean_counters():
    writers._reset_failure_counts_for_tests()
    yield
    writers._reset_failure_counts_for_tests()


async def _rows(db, action_class, subject):
    return await lp.list_by_subject(db, action_class=action_class, subject_ref_id=subject)


# ── outreach ─────────────────────────────────────────────────────────────────


async def test_outreach_hook_writes_both_metrics_policy_prior(db):
    await writers.on_outreach_delivered(db, outreach_id="o-1", category="insight")
    rows = await _rows(db, "outreach_send", "o-1")
    assert {r["metric"] for r in rows} == {"reply_received", "positive_engagement"}
    for r in rows:
        assert r["provenance"] == "policy_prior"
        assert r["confidence"] == 0.02  # measured base-rate seed, NOT 0.5
        assert r["domain"] == "outreach.insight"
        assert r["predictor"] == "outreach_pipeline"
        assert r["status"] == "open"


async def test_outreach_hook_stated_confidence(db):
    await writers.on_outreach_delivered(
        db, outreach_id="o-2", category="digest", stated_confidence=0.4
    )
    rows = await _rows(db, "outreach_send", "o-2")
    assert all(r["provenance"] == "stated" and r["confidence"] == 0.4 for r in rows)


async def test_outreach_hook_clamps_out_of_range_stated(db):
    await writers.on_outreach_delivered(
        db, outreach_id="o-3", category="digest", stated_confidence=1.7
    )
    rows = await _rows(db, "outreach_send", "o-3")
    assert all(r["confidence"] == 0.99 and r["provenance"] == "stated" for r in rows)


async def test_outreach_hook_default_72h_horizon(db):
    await writers.on_outreach_delivered(db, outreach_id="o-4", category="insight")
    row = (await _rows(db, "outreach_send", "o-4"))[0]
    from datetime import datetime, timedelta

    span = datetime.fromisoformat(row["deadline_at"]) - datetime.fromisoformat(row["created_at"])
    assert timedelta(hours=71) < span < timedelta(hours=73)


# ── tasks ────────────────────────────────────────────────────────────────────


async def test_task_hook_writes_both_metrics(db):
    await writers.on_task_claimed(db, task_id="t-1", source="user")
    rows = await _rows(db, "task_execution", "t-1")
    assert {r["metric"] for r in rows} == {"completed", "completed_first_attempt"}
    assert all(
        r["provenance"] == "policy_prior" and r["confidence"] == 0.5 and r["domain"] == "task.user"
        for r in rows
    )


async def test_task_hook_stated_seam(db):
    await writers.on_task_claimed(db, task_id="t-2", source="build_lane", stated_confidence=0.8)
    rows = await _rows(db, "task_execution", "t-2")
    assert all(r["provenance"] == "stated" and r["confidence"] == 0.8 for r in rows)


# ── build lane ───────────────────────────────────────────────────────────────


async def test_build_hook_label_map_stated(db):
    await writers.on_build_verdict(db, candidate_id="b-1", verdict="build", confidence_label="high")
    (row,) = await _rows(db, "build_verdict", "b-1")
    assert (row["metric"], row["provenance"], row["confidence"]) == (
        "user_greenlights",
        "stated",
        0.85,
    )
    assert '"confidence_label": "high"' in row["metadata"]


async def test_build_hook_complement_for_negative_verdict(db):
    """A dont_build verdict predicts the user will NOT greenlight — the
    label confidence inverts."""
    await writers.on_build_verdict(
        db, candidate_id="b-2", verdict="dont_build", confidence_label="high"
    )
    (row,) = await _rows(db, "build_verdict", "b-2")
    assert row["confidence"] == pytest.approx(0.15)
    assert row["provenance"] == "stated"


async def test_build_hook_unknown_label_rides_prior(db):
    await writers.on_build_verdict(db, candidate_id="b-3", verdict="build", confidence_label=None)
    (row,) = await _rows(db, "build_verdict", "b-3")
    assert (row["provenance"], row["confidence"]) == ("policy_prior", 0.5)


# ── ego ──────────────────────────────────────────────────────────────────────


async def test_ego_hook_zero_confidence_is_absent(db):
    """0.0 is the pipeline's missing-confidence default — policy_prior, never
    a stated zero."""
    await writers.on_ego_proposal(db, proposal_id="p-1", action_type="research", confidence=0.0)
    (row,) = await _rows(db, "ego_proposal", "p-1")
    assert (row["metric"], row["provenance"], row["confidence"], row["domain"]) == (
        "approved_and_executes",
        "policy_prior",
        0.5,
        "ego.research",
    )


async def test_ego_hook_stated(db):
    await writers.on_ego_proposal(db, proposal_id="p-2", action_type="outreach", confidence=0.7)
    (row,) = await _rows(db, "ego_proposal", "p-2")
    assert (row["provenance"], row["confidence"]) == ("stated", 0.7)


# ── the fire-and-forget contract ─────────────────────────────────────────────


async def test_hook_failure_never_escapes_and_is_counted(db, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(lp, "create", _boom)
    # must NOT raise
    await writers.on_outreach_delivered(db, outreach_id="o-f", category="insight")
    await writers.on_task_claimed(db, task_id="t-f", source="user")
    await writers.on_build_verdict(db, candidate_id="b-f", verdict="build")
    await writers.on_ego_proposal(db, proposal_id="p-f", action_type="x")
    counts = writers.write_failure_counts()
    # outreach loops two metrics through the guarded writer → 2 counted
    assert counts == {
        "outreach_send": 2,
        "task_execution": 2,
        "build_verdict": 1,
        "ego_proposal": 1,
    }


async def test_dedupe_reentry_is_silent_and_uncounted(db):
    await writers.on_outreach_delivered(db, outreach_id="o-d", category="insight")
    await writers.on_outreach_delivered(db, outreach_id="o-d", category="insight")  # resend
    rows = await _rows(db, "outreach_send", "o-d")
    assert len(rows) == 2  # still exactly one row per metric
    assert writers.write_failure_counts() == {}


async def test_validation_failure_is_counted_not_raised(db):
    """A ValueError from gate 1 (here: a bogus category is fine, but a
    confidence the clamp can't save is impossible — force one via a bad
    metric path by monkeypatching the seed) — simulate with a broken seed."""
    import unittest.mock as mock

    with mock.patch.dict(writers._PRIOR_SEEDS, {"reply_received": 5.0}):
        await writers.on_outreach_delivered(db, outreach_id="o-v", category="insight")
    # reply_received row rejected (confidence out of bounds) and counted;
    # positive_engagement row still landed (isolation is per-write)
    rows = await _rows(db, "outreach_send", "o-v")
    assert {r["metric"] for r in rows} == {"positive_engagement"}
    assert writers.write_failure_counts() == {"outreach_send": 1}


# ── health surfacing ─────────────────────────────────────────────────────────


async def test_compute_alerts_surfaces_failure_counter(monkeypatch):
    """_compute_alerts emits ledger:write_failed:<class> while the counter is
    nonzero — the M10 awareness-tick writer then owns the alert lifecycle."""
    from genesis.mcp.health import errors as health_errors

    writers._write_failures["outreach_send"] = 3
    alerts, current_ids = await health_errors._compute_alerts()
    assert "ledger:write_failed:outreach_send" in current_ids
    (alert,) = [a for a in alerts if a["id"] == "ledger:write_failed:outreach_send"]
    assert alert["severity"] == "WARNING"
    assert "3 ledger prediction write(s) failed" in alert["message"]

    writers._reset_failure_counts_for_tests()
    alerts, current_ids = await health_errors._compute_alerts()
    assert not any(i.startswith("ledger:write_failed") for i in current_ids)
