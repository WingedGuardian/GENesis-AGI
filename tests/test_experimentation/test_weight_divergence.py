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

    async def fake_compute(dbc, signals, *, now=None, weights_override=None, decay_factors=None):
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

    async def fake_compute(dbc, signals, *, now=None, weights_override=None, decay_factors=None):
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


async def test_replay_does_not_mutate_live_staleness(db):
    """The replay path (decay_factors=) must NOT touch the live awareness loop's
    module-level staleness counters — the code-review P2 fix."""
    import contextlib

    from genesis.awareness import scorer
    from genesis.awareness.types import SignalReading

    signals = [SignalReading(name="x", value=0.5, source="replay", collected_at="")]

    # Replay path: decay_factors provided -> _update_staleness is bypassed.
    scorer._signal_unchanged_counts.clear()
    with contextlib.suppress(Exception):  # missing seeded thresholds is fine
        await scorer.compute_scores(db, signals, decay_factors={})
    assert scorer._signal_unchanged_counts == {}, "replay must not mutate live staleness"

    # Contrast — the live path (decay_factors=None) DOES update the counters.
    scorer._signal_unchanged_counts.clear()
    with contextlib.suppress(Exception):
        await scorer.compute_scores(db, signals)
    assert "x" in scorer._signal_unchanged_counts, "live path updates staleness as before"
