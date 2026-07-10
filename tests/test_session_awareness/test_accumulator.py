"""Accumulator unit tests — synthetic vectors, no I/O, no wall clock."""

from __future__ import annotations

import math

from genesis.session_awareness.accumulator import (
    ALPHA,
    MAX_ENTITIES,
    cosine,
    fold_turn,
    top_entities,
)
from genesis.session_awareness.statefiles import empty_state

DIM = 8
NOW = "2026-07-09T12:00:00+00:00"


def unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def blend(a: list[float], b: list[float], t: float) -> list[float]:
    return [(1 - t) * x + t * y for x, y in zip(a, b, strict=True)]


def test_cosine_basics():
    assert cosine(unit(0), unit(0)) == 1.0
    assert cosine(unit(0), unit(1)) == 0.0
    assert cosine([], unit(0)) == 0.0
    assert cosine([0.0] * DIM, unit(0)) == 0.0
    assert cosine(unit(0), unit(0)[:4]) == 0.0  # length mismatch


def test_first_fold_initializes():
    s = empty_state("s1")
    fold_turn(s, unit(0), ["alpha"], now_iso=NOW)
    assert s["ema"] == unit(0)
    assert s["ema_turns"] == 1
    assert s["ring"] == [unit(0)]
    assert s["updated_at"] == NOW


def test_ema_converges_toward_repeated_vector():
    s = empty_state("s1")
    fold_turn(s, unit(0), [], now_iso=NOW)
    target = blend(unit(0), unit(1), 0.5)
    for _ in range(12):
        fold_turn(s, target, [], now_iso=NOW)
    assert cosine(s["ema"], target) > 0.99
    assert s["ema_turns"] == 13


def test_ema_is_unit_norm_after_fold():
    s = empty_state("s1")
    fold_turn(s, [x * 7.5 for x in unit(0)], [], now_iso=NOW)
    fold_turn(s, [x * 0.02 for x in blend(unit(0), unit(1), 0.3)], [], now_iso=NOW)
    norm = math.sqrt(sum(x * x for x in s["ema"]))
    assert abs(norm - 1.0) < 1e-9


def test_outlier_vector_skipped_not_folded():
    s = empty_state("s1")
    fold_turn(s, unit(0), [], now_iso=NOW)
    ema_before = list(s["ema"])
    # Orthogonal vector: cosine 0.0 < OUTLIER_COS → skip
    fold_turn(s, unit(1), ["kw"], now_iso=NOW)
    assert s["ema"] == ema_before
    assert s["ema_turns"] == 1
    assert s["outlier_skips"] == 1
    # ...but the text lane still folded
    assert "kw" in s["entities"]


def test_near_theme_vector_folds_normally():
    s = empty_state("s1")
    fold_turn(s, unit(0), [], now_iso=NOW)
    nearby = blend(unit(0), unit(1), 0.2)
    fold_turn(s, nearby, [], now_iso=NOW)
    assert s["ema_turns"] == 2
    assert s["outlier_skips"] == 0
    expected_dir = blend(unit(0), nearby, ALPHA)
    assert cosine(s["ema"], expected_dir) > 0.999


def test_pivot_resets_ring_but_keeps_ema():
    s = empty_state("s1")
    for _ in range(3):
        fold_turn(s, unit(0), [], now_iso=NOW)
    assert len(s["ring"]) == 3
    fold_turn(s, blend(unit(0), unit(1), 0.2), [], pivoted=True, now_iso=NOW)
    assert len(s["ring"]) == 1  # cleared, then this fold appended
    assert s["ema_turns"] == 4


def test_entities_decay_weights_and_file_discount():
    s = empty_state("s1")
    fold_turn(s, unit(0), ["genesis"], ["retrieval"], now_iso=NOW)
    assert s["entities"]["genesis"] == 1.0
    assert abs(s["entities"]["retrieval"] - 0.3) < 1e-9
    fold_turn(s, unit(0), ["genesis"], [], now_iso=NOW)
    # decayed 0.9 then +1.0
    assert abs(s["entities"]["genesis"] - 1.9) < 1e-9
    assert abs(s["entities"]["retrieval"] - 0.27) < 1e-9


def test_entities_pruned_to_cap_and_min_weight():
    s = empty_state("s1")
    fold_turn(s, unit(0), [f"kw{i}" for i in range(MAX_ENTITIES + 20)], now_iso=NOW)
    assert len(s["entities"]) == MAX_ENTITIES
    # A single 0.3 file keyword decays below 0.05 in ~19 empty turns
    s2 = empty_state("s2")
    fold_turn(s2, unit(0), [], ["fleeting"], now_iso=NOW)
    for _ in range(19):
        fold_turn(s2, unit(0), [], [], now_iso=NOW)
    assert "fleeting" not in s2["entities"]


def test_file_keywords_never_touch_ema():
    s = empty_state("s1")
    fold_turn(s, unit(0), [], ["onlyfile"], now_iso=NOW)
    ema_before = list(s["ema"])
    fold_turn(s, unit(0), [], ["another", "batch", "of", "files"], now_iso=NOW)
    assert s["ema"] == ema_before  # same vector folded; files only hit ledger


def test_empty_vector_updates_ledger_only():
    s = empty_state("s1")
    fold_turn(s, [], ["kw"], now_iso=NOW)
    assert s["ema"] is None
    assert s["ema_turns"] == 0
    assert "kw" in s["entities"]


def test_top_entities_ranked():
    s = empty_state("s1")
    fold_turn(s, unit(0), ["alpha", "beta"], ["gamma"], now_iso=NOW)
    fold_turn(s, unit(0), ["alpha"], [], now_iso=NOW)
    top = top_entities(s, n=2)
    assert top[0] == "alpha"
    assert top[1] == "beta"


def test_pivot_bypasses_outlier_guard():
    """A corroborated pivot to an orthogonal topic must FOLD, not skip —
    otherwise the EMA stays anchored to the dead topic and every new-topic
    turn keeps reading as an outlier until the 24h reset (Codex P2, #972)."""
    s = empty_state("s1")
    for _ in range(3):
        fold_turn(s, unit(0), [], now_iso=NOW)
    fold_turn(s, unit(1), [], pivoted=True, now_iso=NOW)
    assert s["outlier_skips"] == 0
    assert s["ema_turns"] == 4
    assert len(s["ring"]) == 1  # ring reset for the new theme
    assert cosine(s["ema"], unit(1)) > 0.2  # EMA moving toward the new topic


def test_consecutive_outlier_run_escapes_as_theme_change():
    """3 consecutive 'outliers' = a real theme change Jaccard missed —
    the run escapes into a fold with ring reset. Singles stay guarded."""
    s = empty_state("s1")
    for _ in range(3):
        fold_turn(s, unit(0), [], now_iso=NOW)
    fold_turn(s, unit(1), [], now_iso=NOW)
    fold_turn(s, unit(1), [], now_iso=NOW)
    assert s["outlier_skips"] == 2
    assert s["consecutive_outliers"] == 2
    assert s["ema_turns"] == 3  # still skipping
    fold_turn(s, unit(1), [], now_iso=NOW)  # third in the run → escape
    assert s["ema_turns"] == 4
    assert s["consecutive_outliers"] == 0
    assert len(s["ring"]) == 1
    assert cosine(s["ema"], unit(1)) > 0.2


def test_outlier_run_broken_by_normal_turn_resets_counter():
    s = empty_state("s1")
    for _ in range(2):
        fold_turn(s, unit(0), [], now_iso=NOW)
    fold_turn(s, unit(1), [], now_iso=NOW)  # outlier 1
    fold_turn(s, unit(0), [], now_iso=NOW)  # back on theme
    assert s["consecutive_outliers"] == 0
    fold_turn(s, unit(1), [], now_iso=NOW)  # isolated outlier again
    assert s["consecutive_outliers"] == 1
    assert s["outlier_skips"] == 2


def test_dimension_change_reseeds_on_escape():
    """A backend model swap (different embedding dim) can't be folded —
    on pivot/escape the EMA reseeds in the new space instead of raising."""
    s = empty_state("s1")
    for _ in range(3):
        fold_turn(s, unit(0), [], now_iso=NOW)
    short = [1.0, 0.0, 0.0, 0.0]  # different dimension → cosine 0.0
    fold_turn(s, short, [], now_iso=NOW)
    fold_turn(s, short, [], now_iso=NOW)
    assert s["ema_turns"] == 3  # guarded
    fold_turn(s, short, [], now_iso=NOW)  # escape → reseed, not zip-crash
    assert s["ema"] == short
    assert s["ema_turns"] == 1
    assert s["ring"] == [short]
