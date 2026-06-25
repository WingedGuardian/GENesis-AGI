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

    Defaults to a fresh DORMANT draft procedure unless overridden.
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
        draft=1,
        activation_tier="DORMANT",
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


def test_compute_tier_promotes_to_library():
    row = {
        "success_count": 3, "confidence": 0.65, "draft": 0,
        "tool_trigger": None, "activation_tier": "DORMANT",
    }
    assert _compute_tier(row) == "LIBRARY"


def test_compute_tier_promotes_to_advisory():
    row = {
        "success_count": 5, "confidence": 0.78, "draft": 0,
        "tool_trigger": None, "activation_tier": "DORMANT",
    }
    assert _compute_tier(row) == "ADVISORY"


def test_compute_tier_promotes_to_core_with_trigger():
    row = {
        "success_count": 8, "confidence": 0.86, "draft": 0,
        "tool_trigger": ["Bash"], "activation_tier": "DORMANT",
    }
    assert _compute_tier(row) == "CORE"


def test_compute_tier_no_core_without_trigger():
    """CORE requires tool_trigger; without it, fall back to ADVISORY."""
    row = {
        "success_count": 8, "confidence": 0.86, "draft": 0,
        "tool_trigger": None, "activation_tier": "DORMANT",
    }
    assert _compute_tier(row) == "ADVISORY"


def test_compute_tier_explicit_teach_stays_at_library():
    """A user-taught procedure (s=1, conf=0.667, LIBRARY) must stay at LIBRARY.

    No promotion threshold qualifies, but `_compute_tier` must NOT fall
    through to DORMANT — it preserves the explicitly-stored tier.
    """
    row = {
        "success_count": 1, "confidence": 2 / 3, "draft": 0,
        "tool_trigger": None, "activation_tier": "LIBRARY",
    }
    assert _compute_tier(row) == "LIBRARY"


def test_compute_tier_core_with_drift_stays_at_core():
    """A CORE procedure whose confidence drifts below 0.85 must NOT
    metric-demote even if a lower tier (ADVISORY) still matches.

    This is the strict promote-only guarantee: only `_check_demotion`
    or quarantine can demote — never tier drift.
    """
    row = {
        "success_count": 9, "confidence": 0.83, "draft": 0,
        "tool_trigger": ["Bash"], "activation_tier": "CORE",
    }
    # CORE fails (conf<0.85), ADVISORY matches (s>=5, conf>=0.75) — but we hold CORE.
    assert _compute_tier(row) == "CORE"


def test_compute_tier_draft_procedure_can_promote_to_library():
    """Draft procedures can promote to LIBRARY if they meet thresholds."""
    row = {
        "success_count": 5, "confidence": 0.7, "draft": 1,
        "tool_trigger": None, "activation_tier": "DORMANT",
    }
    # draft flag is provenance metadata, not a promotion gate.
    assert _compute_tier(row) == "LIBRARY"


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
    (s=1, conf=2/3, LIBRARY) must survive a promoter run unchanged."""
    await _insert(
        db, id="explicit-1", task_type="user-teach",
        success_count=1, failure_count=0, confidence=2 / 3,
        draft=0, activation_tier="LIBRARY",
    )
    result = await promote_and_demote(db)
    assert result["demotions"] == 0
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?",
        ("explicit-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "LIBRARY"


@pytest.mark.asyncio
async def test_promote_and_demote_promotes_on_thresholds(db):
    """A procedure that earns its way to ADVISORY must be promoted on the
    next promoter run."""
    await _insert(
        db, id="earned-1", task_type="t",
        success_count=5, failure_count=0, confidence=0.78,
        draft=0, activation_tier="DORMANT",
    )
    result = await promote_and_demote(db)
    assert result["promotions"] == 1
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?",
        ("earned-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "ADVISORY"


@pytest.mark.asyncio
async def test_promote_and_demote_demotes_on_failure_evidence(db):
    """A procedure with 3+ failure-mode hits AND failure_count >=
    success_count + 3 must be demoted by exactly one rank."""
    await _insert(
        db, id="failing-1", task_type="t",
        success_count=1, failure_count=4, confidence=0.4,
        draft=0, activation_tier="LIBRARY",
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
    assert row[0] == "DORMANT"


@pytest.mark.asyncio
async def test_promote_and_demote_quarantines_low_confidence(db):
    """A procedure with confidence<0.3 and 3+ total samples must be
    quarantined, not just demoted."""
    await _insert(
        db, id="bad-1", task_type="t",
        success_count=1, failure_count=10, confidence=0.15,
        draft=0, activation_tier="LIBRARY",
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
async def test_promote_and_demote_does_not_metric_demote_core(db):
    """A CORE procedure with confidence drift below the CORE threshold
    must NOT be demoted to ADVISORY — only `_check_demotion` can demote."""
    await _insert(
        db, id="drifty-1", task_type="t",
        success_count=9, failure_count=2, confidence=0.83,
        draft=0, activation_tier="CORE",
        tool_trigger=json.dumps(["Bash"]),
    )
    result = await promote_and_demote(db)
    assert result["demotions"] == 0
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?",
        ("drifty-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == "CORE"


# ─── reads-as-signal: _read_eligible_tier (hybrid, dampened) ──────────────────


def test_read_eligible_tier_library_on_reads_alone():
    """Effective metrics from reads alone qualify for LIBRARY (passive surfacing)."""
    # eff_success=3, eff_conf=(3+1)/(3+0+2)=0.8 ≥ 0.65
    assert _read_eligible_tier(3, 0.8, 0) == "LIBRARY"


def test_read_eligible_tier_advisory_requires_real_success():
    """ADVISORY (the advisory-eligible tier) needs ≥1 *real* success — reads alone
    can't reach it."""
    # eff metrics qualify for ADVISORY but real_success=0 → capped at LIBRARY.
    assert _read_eligible_tier(5, 0.78, 0) == "LIBRARY"
    # same effective metrics, but with one real success → L2.
    assert _read_eligible_tier(5, 0.78, 1) == "ADVISORY"


def test_read_eligible_tier_never_core():
    """Reads can never buy CORE (always-on), no matter how high the effective
    metrics, even with a real success."""
    assert _read_eligible_tier(50, 0.99, 5) == "ADVISORY"


def test_read_eligible_tier_below_threshold_is_dormant():
    assert _read_eligible_tier(2, 0.6, 0) == "DORMANT"


# ─── reads-as-signal: promote_and_demote integration ─────────────────────────


@pytest.mark.asyncio
async def test_reads_promote_draft_to_library_but_stay_draft(db):
    """A heavily-read draft (no real success) promotes to LIBRARY on effective
    metrics, but stays draft (de-spec needs a real success)."""
    await _insert(
        db, id="read-draft", task_type="t",
        success_count=0, failure_count=0, confidence=0.5,
        draft=1, activation_tier="DORMANT", invocation_count=15,  # eff_success=3
    )
    result = await promote_and_demote(db)
    assert result["promotions"] == 1
    cursor = await db.execute(
        "SELECT activation_tier, draft FROM procedural_memory WHERE id = ?",
        ("read-draft",),
    )
    row = await cursor.fetchone()
    assert row[0] == "LIBRARY"
    assert row[1] == 1  # still draft — reads don't clear the draft flag


@pytest.mark.asyncio
async def test_reads_without_real_success_do_not_reach_advisory(db):
    """Effective metrics alone (no real success) cannot push past LIBRARY."""
    await _insert(
        db, id="read-only", task_type="t",
        success_count=0, failure_count=0, confidence=0.5,
        draft=1, activation_tier="LIBRARY", invocation_count=25,  # eff_success=5
    )
    await promote_and_demote(db)
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", ("read-only",),
    )
    assert (await cursor.fetchone())[0] == "LIBRARY"


@pytest.mark.asyncio
async def test_reads_plus_real_success_promote_to_advisory(db):
    """Reads + ≥1 real success promote to ADVISORY."""
    await _insert(
        db, id="read-proven", task_type="t",
        success_count=1, failure_count=0, confidence=2 / 3,
        draft=0, activation_tier="LIBRARY", invocation_count=20,  # eff_success=5
    )
    result = await promote_and_demote(db)
    assert result["promotions"] == 1
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", ("read-proven",),
    )
    assert (await cursor.fetchone())[0] == "ADVISORY"


@pytest.mark.asyncio
async def test_reads_never_promote_to_core(db):
    """No volume of reads lifts a procedure to CORE (always-on)."""
    await _insert(
        db, id="read-heavy", task_type="t",
        success_count=0, failure_count=0, confidence=0.5,
        draft=0, activation_tier="ADVISORY", invocation_count=200,
        tool_trigger=json.dumps(["Bash"]),
    )
    await promote_and_demote(db)
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", ("read-heavy",),
    )
    assert (await cursor.fetchone())[0] == "ADVISORY"


@pytest.mark.asyncio
async def test_draft_cleared_on_real_success(db):
    """A draft procedure with ≥1 real success and no failures graduates
    to validated (draft=0) — closing the draft-clearing gap."""
    await _insert(
        db, id="grad-1", task_type="t",
        success_count=1, failure_count=0, confidence=2 / 3,
        draft=1, activation_tier="LIBRARY",
    )
    result = await promote_and_demote(db)
    assert result["drafts_cleared"] == 1
    cursor = await db.execute(
        "SELECT draft FROM procedural_memory WHERE id = ?", ("grad-1",),
    )
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_reads_do_not_promote_a_failing_procedure(db):
    """A procedure in a failure state (trips _check_demotion) must NOT be
    read-promoted, even with enough reads to otherwise qualify. Reads are a
    soft signal; recorded failures are the hard counterweight.

    Sits in the 0.3–0.5 confidence band so it escapes the quarantine guard and
    actually reaches the promote/demote block.
    """
    await _insert(
        db, id="failing-read", task_type="t",
        success_count=1, failure_count=4, confidence=0.333,
        draft=0, activation_tier="DORMANT", invocation_count=40,  # eff_success=9
        failure_modes=json.dumps([
            {"description": "x", "conditions": "x", "times_hit": 3},
        ]),
    )
    result = await promote_and_demote(db)
    assert result["promotions"] == 0
    cursor = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", ("failing-read",),
    )
    assert (await cursor.fetchone())[0] == "DORMANT"  # reads can't promote a failing proc


@pytest.mark.asyncio
async def test_draft_clearing_blocked_by_failure(db):
    """A failure blocks draft clearing even with real successes."""
    await _insert(
        db, id="grad-fail", task_type="t",
        success_count=2, failure_count=1, confidence=0.6,
        draft=1, activation_tier="LIBRARY",
    )
    result = await promote_and_demote(db)
    assert result["drafts_cleared"] == 0
    cursor = await db.execute(
        "SELECT draft FROM procedural_memory WHERE id = ?", ("grad-fail",),
    )
    assert (await cursor.fetchone())[0] == 1
