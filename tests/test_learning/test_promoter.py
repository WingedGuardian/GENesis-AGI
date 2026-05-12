"""Tests for procedural promoter — promote, demote, quarantine, no-metrics-demote."""

from __future__ import annotations

import json

import pytest

from genesis.learning.procedural import trigger_cache
from genesis.learning.procedural.promoter import (
    _check_demotion,
    _compute_tier,
    promote_and_demote,
)


@pytest.fixture(autouse=True)
def _isolate_trigger_cache_path(tmp_path, monkeypatch):
    """Redirect trigger_cache writes to a tmp path for every test in this file.

    promote_and_demote() calls trigger_cache.regenerate() on any tier change,
    which writes to <repo>/config/procedure_triggers.yaml by default. Without
    this patch, test runs stomp the real tracked config file with the test
    db's (empty) procedure set, causing downstream test_procedure_advisor
    failures in the same sweep.
    """
    monkeypatch.setattr(
        trigger_cache, "_CACHE_PATH", tmp_path / "procedure_triggers.yaml",
    )

# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _insert(db, *, id: str, task_type: str, **fields) -> None:
    """Insert a procedural_memory row with explicit field overrides.

    Defaults to a fresh L4 speculative procedure unless overridden.
    """
    defaults = dict(
        person_id=None,
        principle="p",
        steps=json.dumps(["s"]),
        tools_used=json.dumps([]),
        context_tags=json.dumps([]),
        success_count=0,
        failure_count=0,
        confidence=0.0,
        speculative=1,
        activation_tier="L4",
        tool_trigger=None,
        failure_modes=None,
        attempted_workarounds=None,
        version=1,
        deprecated=0,
        quarantined=0,
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(fields)
    cols = ["id", "task_type", *defaults.keys()]
    placeholders = ", ".join(["?"] * len(cols))
    values = (id, task_type, *defaults.values())
    await db.execute(
        f"INSERT INTO procedural_memory ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )
    await db.commit()


# ─── _compute_tier unit semantics ─────────────────────────────────────────────


def test_compute_tier_promotes_to_l3():
    row = {
        "success_count": 3, "confidence": 0.65, "speculative": 0,
        "tool_trigger": None, "activation_tier": "L4",
    }
    assert _compute_tier(row) == "L3"


def test_compute_tier_promotes_to_l2():
    row = {
        "success_count": 5, "confidence": 0.78, "speculative": 0,
        "tool_trigger": None, "activation_tier": "L4",
    }
    assert _compute_tier(row) == "L2"


def test_compute_tier_promotes_to_l1_with_trigger():
    row = {
        "success_count": 8, "confidence": 0.86, "speculative": 0,
        "tool_trigger": ["Bash"], "activation_tier": "L4",
    }
    assert _compute_tier(row) == "L1"


def test_compute_tier_no_l1_without_trigger():
    """L1 requires tool_trigger; without it, fall back to L2."""
    row = {
        "success_count": 8, "confidence": 0.86, "speculative": 0,
        "tool_trigger": None, "activation_tier": "L4",
    }
    assert _compute_tier(row) == "L2"


def test_compute_tier_explicit_teach_stays_at_l3():
    """A user-taught procedure (s=1, conf=0.667, L3) must stay at L3.

    No promotion threshold qualifies, but `_compute_tier` must NOT fall
    through to L4 — it preserves the explicitly-stored tier.
    """
    row = {
        "success_count": 1, "confidence": 2 / 3, "speculative": 0,
        "tool_trigger": None, "activation_tier": "L3",
    }
    assert _compute_tier(row) == "L3"


def test_compute_tier_l1_with_drift_stays_at_l1():
    """An L1 procedure whose confidence drifts below 0.85 must NOT
    metric-demote even if a lower tier (L2) still matches.

    This is the strict promote-only guarantee: only `_check_demotion`
    or quarantine can demote — never tier drift.
    """
    row = {
        "success_count": 9, "confidence": 0.83, "speculative": 0,
        "tool_trigger": ["Bash"], "activation_tier": "L1",
    }
    # L1 fails (conf<0.85), L2 matches (s>=5, conf>=0.75) — but we hold L1.
    assert _compute_tier(row) == "L1"


def test_compute_tier_speculative_procedure_can_promote_to_l3():
    """Speculative procedures can promote to L3 if they meet thresholds."""
    row = {
        "success_count": 5, "confidence": 0.7, "speculative": 1,
        "tool_trigger": None, "activation_tier": "L4",
    }
    # speculative flag is provenance metadata, not a promotion gate.
    assert _compute_tier(row) == "L3"


# ─── _check_demotion ──────────────────────────────────────────────────────────


def test_check_demotion_true_when_failures_exceed():
    row = {
        "success_count": 1, "failure_count": 4,
        "failure_modes": json.dumps([
            {"description": "x", "times_hit": 3},
        ]),
    }
    assert _check_demotion(row) is True


def test_check_demotion_false_when_no_failure_modes():
    row = {
        "success_count": 1, "failure_count": 4,
        "failure_modes": None,
    }
    assert _check_demotion(row) is False


# ─── promote_and_demote integration ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_and_demote_does_not_demote_explicit_teach(db):
    """Critical regression test: a freshly-stored explicit-teach procedure
    (s=1, conf=2/3, L3) must survive a promoter run unchanged."""
    await _insert(
        db, id="explicit-1", task_type="user-teach",
        success_count=1, failure_count=0, confidence=2 / 3,
        speculative=0, activation_tier="L3",
    )
    result = await promote_and_demote(db)
    assert result["demotions"] == 0
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?",
        ("explicit-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "L3"


@pytest.mark.asyncio
async def test_promote_and_demote_promotes_on_thresholds(db):
    """A procedure that earns its way to L2 must be promoted on the
    next promoter run."""
    await _insert(
        db, id="earned-1", task_type="t",
        success_count=5, failure_count=0, confidence=0.78,
        speculative=0, activation_tier="L4",
    )
    result = await promote_and_demote(db)
    assert result["promotions"] == 1
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?",
        ("earned-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "L2"


@pytest.mark.asyncio
async def test_promote_and_demote_demotes_on_failure_evidence(db):
    """A procedure with 3+ failure-mode hits AND failure_count >=
    success_count + 3 must be demoted by exactly one rank."""
    await _insert(
        db, id="failing-1", task_type="t",
        success_count=1, failure_count=4, confidence=0.4,
        speculative=0, activation_tier="L3",
        failure_modes=json.dumps([
            {"description": "timeout", "conditions": "timeout",
             "times_hit": 3, "transient": False},
        ]),
    )
    result = await promote_and_demote(db)
    assert result["demotions"] == 1
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?",
        ("failing-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "L4"


@pytest.mark.asyncio
async def test_promote_and_demote_quarantines_low_confidence(db):
    """A procedure with confidence<0.3 and 3+ total samples must be
    quarantined, not just demoted."""
    await _insert(
        db, id="bad-1", task_type="t",
        success_count=1, failure_count=10, confidence=0.15,
        speculative=0, activation_tier="L3",
    )
    result = await promote_and_demote(db)
    assert result["quarantined"] == 1
    cursor = await db.execute(
        "SELECT quarantined FROM procedural_memory WHERE id = ?",
        ("bad-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_promote_and_demote_does_not_metric_demote_l1(db):
    """An L1 procedure with confidence drift below the L1 threshold
    must NOT be demoted to L2 — only `_check_demotion` can demote."""
    await _insert(
        db, id="drifty-1", task_type="t",
        success_count=9, failure_count=2, confidence=0.83,
        speculative=0, activation_tier="L1",
        tool_trigger=json.dumps(["Bash"]),
    )
    result = await promote_and_demote(db)
    assert result["demotions"] == 0
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?",
        ("drifty-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "L1"
