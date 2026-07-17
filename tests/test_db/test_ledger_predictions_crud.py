"""CRUD gate tests for ``ledger_predictions`` (WS-2 P1a, falsifiability gate 1).

The writer must make unmeasurable predictions unwritable: every rejection in
the matrix below is a ``ValueError`` raised BEFORE any SQL executes. All
clocks are injected — no wall-clock dependence.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import ledger_predictions as lp

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
DEADLINE = (NOW + timedelta(hours=24)).isoformat()


async def _mk(db, **overrides) -> dict:
    kwargs = dict(
        action_class="outreach_send",
        subject_ref_type="outreach",
        subject_ref_id="o-1",
        domain="outreach.reminder",
        metric="reply_received",
        confidence=0.5,
        deadline_at=DEADLINE,
        provenance="policy_prior",
        predictor="test",
        now=NOW,
    )
    kwargs.update(overrides)
    return await lp.create(db, **kwargs)


# ── round-trip ───────────────────────────────────────────────────────────────


async def test_create_round_trip(db):
    row = await _mk(db, rationale="because", metadata={"seed": "base_rate"})
    assert row["status"] == "open"
    assert row["comparator"] == "is_true"
    assert row["confidence"] == 0.5
    assert row["metric"] == "reply_received"
    # canonical UTC-offset ISO on both stamped timestamps
    assert row["created_at"].endswith("+00:00")
    assert row["deadline_at"].endswith("+00:00")
    assert row["brier"] is None
    fetched = await lp.get_by_id(db, row["id"])
    assert fetched == row


async def test_get_by_id_missing(db):
    assert await lp.get_by_id(db, "nope") is None


# ── the rejection matrix (gate 1) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"metric": "vibes_good"}, "unknown metric"),
        # metric/action_class cross-wiring
        ({"action_class": "task_execution"}, "belongs to action_class"),
        # comparator outside the metric's domain
        ({"comparator": "le", "threshold": 5.0}, "not allowed for metric"),
        # threshold pairing, both directions
        ({"threshold": 5.0}, "threshold is required.*forbidden"),
        (
            {
                "metric": "runtime_ms_le",
                "action_class": "scheduled_job",
                "comparator": "le",
                "threshold": None,
            },
            "threshold is required",
        ),
        # confidence bounds
        ({"confidence": 0.0}, "confidence"),
        ({"confidence": 0.005}, "confidence"),
        ({"confidence": 0.995}, "confidence"),
        ({"confidence": 1.0}, "confidence"),
        # deadline horizon: past, exactly-now, beyond cap, garbage
        ({"deadline_at": (NOW - timedelta(hours=1)).isoformat()}, "deadline_at"),
        ({"deadline_at": NOW.isoformat()}, "deadline_at"),
        ({"deadline_at": (NOW + timedelta(days=31)).isoformat()}, "deadline_at"),
        ({"deadline_at": "not-a-date"}, "unparseable"),
        ({"provenance": "vibes"}, "provenance"),
    ],
)
async def test_writer_rejection_matrix(db, overrides, match):
    with pytest.raises(ValueError, match=match):
        await _mk(db, **overrides)
    # nothing was written
    assert await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-1") == []


async def test_dedupe_unique_key_raises(db):
    """Same (action_class, subject, metric) twice → IntegrityError for the
    caller to decide (P1b hooks debug-log legitimate re-entries)."""
    await _mk(db)
    with pytest.raises(sqlite3.IntegrityError):
        await _mk(db)
    # different metric on the same subject is a distinct prediction
    await _mk(db, metric="positive_engagement")


# ── grade-side helpers ───────────────────────────────────────────────────────


async def test_list_due_open_filters_and_orders(db):
    late = await _mk(
        db, subject_ref_id="o-late", deadline_at=(NOW + timedelta(hours=2)).isoformat()
    )
    early = await _mk(
        db, subject_ref_id="o-early", deadline_at=(NOW + timedelta(hours=1)).isoformat()
    )
    await _mk(db, subject_ref_id="o-notdue", deadline_at=(NOW + timedelta(hours=20)).isoformat())

    due = await lp.list_due_open(db, now=NOW + timedelta(hours=3))
    assert [r["id"] for r in due] == [early["id"], late["id"]]

    # resolved rows leave the queue
    assert await lp.resolve(
        db, early["id"], status="resolved", outcome_value=0, resolver="mechanical_absence"
    )
    due = await lp.list_due_open(db, now=NOW + timedelta(hours=3))
    assert [r["id"] for r in due] == [late["id"]]


async def test_list_by_subject(db):
    await _mk(db)
    await _mk(db, metric="positive_engagement")
    rows = await lp.list_by_subject(db, action_class="outreach_send", subject_ref_id="o-1")
    assert [r["metric"] for r in rows] == ["positive_engagement", "reply_received"]


async def test_resolve_sets_outcome_brier_and_is_idempotent(db):
    row = await _mk(db, confidence=0.8)
    ok = await lp.resolve(
        db,
        row["id"],
        status="resolved",
        outcome_value=1,
        resolver="mechanical",
        evidence_ref="outreach_history:o-1",
        now=NOW + timedelta(hours=25),
    )
    assert ok
    graded = await lp.get_by_id(db, row["id"])
    assert graded["status"] == "resolved"
    assert graded["outcome_value"] == 1
    assert graded["resolver"] == "mechanical"
    assert graded["evidence_ref"] == "outreach_history:o-1"
    assert graded["resolved_at"].endswith("+00:00")
    assert graded["brier"] == pytest.approx((0.8 - 1) ** 2)
    # double-grading is a no-op (guarded WHERE)
    assert not await lp.resolve(
        db, row["id"], status="resolved", outcome_value=0, resolver="mechanical"
    )
    assert (await lp.get_by_id(db, row["id"]))["outcome_value"] == 1


async def test_resolve_via_fuzzy_queue(db):
    """open → fuzzy_pending (queue) → fuzzy_resolved (the M8-bounded lane)."""
    row = await _mk(db)
    assert await lp.resolve(db, row["id"], status="fuzzy_pending")
    mid = await lp.get_by_id(db, row["id"])
    assert mid["status"] == "fuzzy_pending"
    assert mid["outcome_value"] is None
    assert await lp.resolve(
        db, row["id"], status="fuzzy_resolved", outcome_value=0, resolver="llm_fallback"
    )
    assert (await lp.get_by_id(db, row["id"]))["status"] == "fuzzy_resolved"


async def test_resolve_validation(db):
    row = await _mk(db)
    with pytest.raises(ValueError, match="invalid target status"):
        await lp.resolve(db, row["id"], status="open")
    with pytest.raises(ValueError, match="resolver is required"):
        await lp.resolve(db, row["id"], status="resolved", outcome_value=1)
    with pytest.raises(ValueError, match="invalid resolver"):
        await lp.resolve(db, row["id"], status="resolved", outcome_value=1, resolver="oracle")
    with pytest.raises(ValueError, match="outcome_value"):
        await lp.resolve(db, row["id"], status="resolved", outcome_value=2, resolver="mechanical")
    # row untouched by all rejected calls
    assert (await lp.get_by_id(db, row["id"]))["status"] == "open"
