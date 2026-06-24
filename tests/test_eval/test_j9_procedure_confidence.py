"""Procedure-confidence signals must exclude speculative (unvalidated) procedures.

A flood of speculative L4 procedures (confidence ≈ 0, never invoked) dragged the
system composite down ~0.10 — a volume artifact, not a quality regression. The
composite's `procedure_mean_confidence` and the procedure dimension's
`mean_confidence` must measure VALIDATED procedures (speculative=0) only, while
`total_procedures` still counts the whole store (consistent with tier_distribution).
"""

from __future__ import annotations

import pytest

from genesis.eval.j9_aggregator import (
    _compute_procedural_effectiveness,
    _compute_system_composite,
)

_SINCE = "2000-01-01T00:00:00Z"
_UNTIL = "2099-01-01T00:00:00Z"


async def _seed_proc(db, pid, confidence, *, speculative, deprecated=0):
    await db.execute(
        "INSERT INTO procedural_memory "
        "(id, task_type, principle, steps, tools_used, context_tags, "
        " confidence, speculative, deprecated, created_at) "
        "VALUES (?, 'task', 'p', '[]', '[]', '[]', ?, ?, ?, '2026-06-01T00:00:00Z')",
        (pid, confidence, speculative, deprecated),
    )
    await db.commit()


async def test_procedural_mean_confidence_excludes_speculative(db):
    # 3 validated @0.8 + 3 speculative @0.0. Naive AVG would be 0.4.
    for i in range(3):
        await _seed_proc(db, f"val-{i}", 0.8, speculative=0)
    for i in range(3):
        await _seed_proc(db, f"spec-{i}", 0.0, speculative=1)

    metrics, _ = await _compute_procedural_effectiveness(db, _SINCE, _UNTIL)
    assert metrics["mean_confidence"] == pytest.approx(0.8, abs=1e-3)
    # total_procedures still counts the whole store (consistent w/ tier_distribution)
    assert metrics["total_procedures"] == 6


async def test_system_composite_procedure_signal_excludes_speculative(db):
    for i in range(3):
        await _seed_proc(db, f"val-{i}", 0.8, speculative=0)
    for i in range(3):
        await _seed_proc(db, f"spec-{i}", 0.0, speculative=1)

    metrics, _ = await _compute_system_composite(db, _SINCE, _UNTIL)
    assert metrics["procedure_mean_confidence"] == pytest.approx(0.8, abs=1e-3)


async def test_all_speculative_yields_null_mean_not_zero(db):
    # With only speculative rows, the validated-mean is undefined → None (not 0),
    # so the composite drops the signal rather than reporting a false 0.0.
    for i in range(3):
        await _seed_proc(db, f"spec-{i}", 0.0, speculative=1)

    proc, _ = await _compute_procedural_effectiveness(db, _SINCE, _UNTIL)
    assert proc["mean_confidence"] is None
    assert proc["total_procedures"] == 3  # store still counted

    comp, _ = await _compute_system_composite(db, _SINCE, _UNTIL)
    assert comp["procedure_mean_confidence"] is None


async def test_deprecated_still_excluded(db):
    # Regression guard: deprecated rows remain excluded regardless of speculative.
    await _seed_proc(db, "val", 0.8, speculative=0)
    await _seed_proc(db, "dep", 0.9, speculative=0, deprecated=1)
    metrics, _ = await _compute_procedural_effectiveness(db, _SINCE, _UNTIL)
    assert metrics["mean_confidence"] == pytest.approx(0.8, abs=1e-3)
    assert metrics["total_procedures"] == 1
