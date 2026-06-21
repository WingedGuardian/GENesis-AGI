"""Tests for the dark cognitive-drift dimension (Phase 7, j9_aggregator)."""

import pytest

from genesis.eval.j9_aggregator import _compute_cognitive_drift

_PS = "2026-06-01T00:00:00+00:00"
_PE = "2026-06-30T00:00:00+00:00"


async def _insert(db, pid, action_type, alternatives="", verdict=None,
                  created_at="2026-06-15T00:00:00+00:00"):
    await db.execute(
        """INSERT INTO ego_proposals (id, action_type, content, created_at,
               alternatives, realist_verdict)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (pid, action_type, "content", created_at, alternatives, verdict),
    )


async def test_drift_metrics(db):
    # action_types a,a,b,c ; verdicts pass,amend,reject,None ; alts on p1,p3
    await _insert(db, "p1", "a", alternatives="x", verdict="pass")
    await _insert(db, "p2", "a", alternatives="", verdict="amend")
    await _insert(db, "p3", "b", alternatives="y", verdict="reject")
    await _insert(db, "p4", "c", alternatives="", verdict=None)
    await db.commit()

    metrics, n = await _compute_cognitive_drift(db, _PS, _PE)
    assert n == 4
    # 3 verdicts present; dissent (amend+reject) = 2 -> 2/3
    assert metrics["n_with_verdict"] == 3
    assert metrics["dissent_rate"] == pytest.approx(0.6667, abs=1e-3)
    # 2 of 4 proposals recorded an alternative
    assert metrics["alternative_rate"] == 0.5
    # distinct action_types a,b,c
    assert metrics["distinct_action_types"] == 3
    assert 0.0 < metrics["diversity_entropy"] <= 1.0


async def test_drift_single_action_type_zero_entropy(db):
    for i in range(3):
        await _insert(db, f"s{i}", "same", verdict="pass")
    await db.commit()
    metrics, n = await _compute_cognitive_drift(db, _PS, _PE)
    assert n == 3
    assert metrics["distinct_action_types"] == 1
    assert metrics["diversity_entropy"] == 0.0  # no diversity with one type
    assert metrics["dissent_rate"] == 0.0  # all 'pass' -> no dissent


async def test_drift_empty_window(db):
    metrics, n = await _compute_cognitive_drift(
        db, "2030-01-01T00:00:00+00:00", "2030-12-31T00:00:00+00:00",
    )
    assert n == 0
    assert metrics["dissent_rate"] is None
    assert metrics["diversity_entropy"] is None
