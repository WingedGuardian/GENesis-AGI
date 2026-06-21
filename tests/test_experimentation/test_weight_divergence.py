"""Tests for the Target-2 weight-divergence report (flip-counting logic).

compute_scores is stubbed here — the real scorer + weights_override integration
is covered by tests/test_awareness/test_scorer.py. This isolates the divergence
module's flip/gain/lost accounting.
"""

import json

import pytest

from genesis.awareness.types import Depth, DepthScore
from genesis.experimentation.weight_divergence import weight_divergence


def _ds(depth: Depth, triggered: bool) -> DepthScore:
    return DepthScore(
        depth=depth, raw_score=0.0, time_multiplier=1.0,
        final_score=0.0, threshold=0.5, triggered=triggered,
    )


async def _insert_ticks(db, n):
    sig = json.dumps([{"name": "sig_a", "value": 0.4, "source": "x", "collected_at": "2026-01-01T00:00:00+00:00"}])
    for i in range(n):
        await db.execute(
            """INSERT INTO awareness_ticks
               (id, source, signals_json, scores_json, classified_depth,
                trigger_reason, created_at, dispatched)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"tick_{i}", "scheduled", sig, "[]", "Micro", "test",
             f"2026-01-{i + 1:02d}T00:00:00+00:00", 0),
        )
    await db.commit()


async def test_divergence_counts_flips_and_gains(db, monkeypatch):
    await _insert_ticks(db, 3)

    async def fake_compute(dbc, signals, *, now=None, weights_override=None):
        # Under the variant weights, Light additionally triggers (Micro always on).
        if weights_override:
            return [_ds(Depth.MICRO, True), _ds(Depth.LIGHT, True)]
        return [_ds(Depth.MICRO, True), _ds(Depth.LIGHT, False)]

    monkeypatch.setattr(
        "genesis.experimentation.weight_divergence.compute_scores", fake_compute,
    )

    out = await weight_divergence(db, signal_weight_overrides={"sig_a": 5.0}, limit=10)
    assert out["n_ticks"] == 3
    assert out["n_flipped"] == 3
    assert out["flip_rate"] == 1.0
    assert out["depths_gained"] == {"Light": 3}
    assert out["depths_lost"] == {}
    assert out["n_skipped"] == 0


async def test_divergence_no_flip_when_identical(db, monkeypatch):
    await _insert_ticks(db, 2)

    async def fake_compute(dbc, signals, *, now=None, weights_override=None):
        return [_ds(Depth.MICRO, True), _ds(Depth.LIGHT, False)]

    monkeypatch.setattr(
        "genesis.experimentation.weight_divergence.compute_scores", fake_compute,
    )

    out = await weight_divergence(db, signal_weight_overrides={"sig_a": 5.0}, limit=10)
    assert out["n_flipped"] == 0
    assert out["flip_rate"] == 0.0


async def test_divergence_empty_override_raises(db):
    with pytest.raises(ValueError, match="non-empty"):
        await weight_divergence(db, signal_weight_overrides={}, limit=10)
