"""Tests for effective_confidence — reads as a dampened (fractional-success) signal.

A *read* (deliberate procedure_recall) is a soft positive signal. It counts as a
fractional success via READ_CONFIDENCE_DISCOUNT reads == 1 effective success, with
recorded failures as the counterweight. Stored `confidence` stays real Laplace;
this derived value is used ONLY for ranking + tier decisions.
"""

from __future__ import annotations

from genesis.learning.procedural.operations import (
    READ_CONFIDENCE_DISCOUNT,
    effective_confidence,
)


def test_no_reads_equals_plain_laplace():
    # 0/0 → 1/2; 1 success → 2/3 — identical to the real Laplace formula.
    assert effective_confidence(0, 0, 0) == 0.5
    assert abs(effective_confidence(1, 0, 0) - 2 / 3) < 1e-9


def test_discount_threshold_no_partial_credit():
    # Fewer than DISCOUNT reads earns zero effective success (integer floor).
    assert effective_confidence(0, 0, READ_CONFIDENCE_DISCOUNT - 1) == 0.5


def test_reads_add_fractional_success():
    # DISCOUNT reads == 1 effective success → same as one real success.
    assert abs(effective_confidence(0, 0, READ_CONFIDENCE_DISCOUNT) - 2 / 3) < 1e-9
    # 31 reads at discount 5 → 6 effective successes → (6+1)/(6+0+2) = 7/8.
    assert abs(effective_confidence(0, 0, 31) - 7 / 8) < 1e-9


def test_failures_are_the_counterweight():
    # No reads, 3 failures → (0+1)/(0+3+2) = 0.2.
    assert abs(effective_confidence(0, 3, 0) - 0.2) < 1e-9
    # 20 reads (4 eff successes) vs 3 failures → (4+1)/(4+3+2) = 5/9.
    assert abs(effective_confidence(0, 3, 20) - 5 / 9) < 1e-9


def test_real_successes_and_reads_compound():
    # 2 real + 17 reads (3 eff) → 5 eff successes → (5+1)/(5+0+2) = 6/7.
    assert abs(effective_confidence(2, 0, 17) - 6 / 7) < 1e-9
