"""Tests for procedural promoter — promote, demote, quarantine, no-metrics-demote."""

from __future__ import annotations

import json

import pytest

from genesis.learning.procedural import trigger_cache
from genesis.learning.procedural.promoter import (
    _check_demotion,
    _compute_tier,
    _read_eligible_tier,
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


# ─── reads-as-signal: _read_eligible_tier (hybrid, dampened) ──────────────────


def test_read_eligible_tier_l3_on_reads_alone():
    """Effective metrics from reads alone qualify for L3 (passive surfacing)."""
    # eff_success=3, eff_conf=(3+1)/(3+0+2)=0.8 ≥ 0.65
    assert _read_eligible_tier(3, 0.8, 0) == "L3"


def test_read_eligible_tier_l2_requires_real_success():
    """L2 (the advisory-eligible tier) needs ≥1 *real* success — reads alone
    can't reach it."""
    # eff metrics qualify for L2 but real_success=0 → capped at L3.
    assert _read_eligible_tier(5, 0.78, 0) == "L3"
    # same effective metrics, but with one real success → L2.
    assert _read_eligible_tier(5, 0.78, 1) == "L2"


def test_read_eligible_tier_never_l1():
    """Reads can never buy L1 (always-on), no matter how high the effective
    metrics, even with a real success."""
    assert _read_eligible_tier(50, 0.99, 5) == "L2"


def test_read_eligible_tier_below_threshold_is_l4():
    assert _read_eligible_tier(2, 0.6, 0) == "L4"


# ─── reads-as-signal: promote_and_demote integration ─────────────────────────


@pytest.mark.asyncio
async def test_reads_promote_speculative_to_l3_but_stay_speculative(db):
    """A heavily-read draft (no real success) promotes to L3 on effective
    metrics, but stays speculative (de-spec needs a real success)."""
    await _insert(
        db, id="read-draft", task_type="t",
        success_count=0, failure_count=0, confidence=0.5,
        speculative=1, activation_tier="L4", invocation_count=15,  # eff_success=3
    )
    result = await promote_and_demote(db)
    assert result["promotions"] == 1
    cursor = await db.execute(
        "SELECT activation_tier, speculative FROM procedural_memory WHERE id = ?",
        ("read-draft",),
    )
    row = await cursor.fetchone()
    assert row[0] == "L3"
    assert row[1] == 1  # still speculative — reads don't de-speculate


@pytest.mark.asyncio
async def test_reads_without_real_success_do_not_reach_l2(db):
    """Effective metrics alone (no real success) cannot push past L3."""
    await _insert(
        db, id="read-only", task_type="t",
        success_count=0, failure_count=0, confidence=0.5,
        speculative=1, activation_tier="L3", invocation_count=25,  # eff_success=5
    )
    await promote_and_demote(db)
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", ("read-only",),
    )
    assert (await cursor.fetchone())[0] == "L3"


@pytest.mark.asyncio
async def test_reads_plus_real_success_promote_to_l2(db):
    """Reads + ≥1 real success promote to L2."""
    await _insert(
        db, id="read-proven", task_type="t",
        success_count=1, failure_count=0, confidence=2 / 3,
        speculative=0, activation_tier="L3", invocation_count=20,  # eff_success=5
    )
    result = await promote_and_demote(db)
    assert result["promotions"] == 1
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", ("read-proven",),
    )
    assert (await cursor.fetchone())[0] == "L2"


@pytest.mark.asyncio
async def test_reads_never_promote_to_l1(db):
    """No volume of reads lifts a procedure to L1 (always-on)."""
    await _insert(
        db, id="read-heavy", task_type="t",
        success_count=0, failure_count=0, confidence=0.5,
        speculative=0, activation_tier="L2", invocation_count=200,
        tool_trigger=json.dumps(["Bash"]),
    )
    await promote_and_demote(db)
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", ("read-heavy",),
    )
    assert (await cursor.fetchone())[0] == "L2"


@pytest.mark.asyncio
async def test_despeculate_on_real_success(db):
    """A speculative procedure with ≥1 real success and no failures graduates
    to validated (speculative=0) — closing the de-speculation gap."""
    await _insert(
        db, id="grad-1", task_type="t",
        success_count=1, failure_count=0, confidence=2 / 3,
        speculative=1, activation_tier="L3",
    )
    result = await promote_and_demote(db)
    assert result["despeculated"] == 1
    cursor = await db.execute(
        "SELECT speculative FROM procedural_memory WHERE id = ?", ("grad-1",),
    )
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_despeculate_blocked_by_failure(db):
    """A failure blocks de-speculation even with real successes."""
    await _insert(
        db, id="grad-fail", task_type="t",
        success_count=2, failure_count=1, confidence=0.6,
        speculative=1, activation_tier="L3",
    )
    result = await promote_and_demote(db)
    assert result["despeculated"] == 0
    cursor = await db.execute(
        "SELECT speculative FROM procedural_memory WHERE id = ?", ("grad-fail",),
    )
    assert (await cursor.fetchone())[0] == 1
