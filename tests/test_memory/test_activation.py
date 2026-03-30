from __future__ import annotations

import pytest

from genesis.memory.activation import compute_activation

NOW = "2026-03-08T00:00:00+00:00"


def test_fresh_memory_half_confidence():
    result = compute_activation(
        confidence=1.0, created_at=NOW, retrieved_count=0, link_count=0, now=NOW,
    )
    assert result.final_score == pytest.approx(0.5, abs=0.01)
    assert result.recency_factor == pytest.approx(1.0, abs=0.001)


def test_old_memory_low_recency():
    old = "2025-03-08T00:00:00+00:00"  # 365 days ago
    result = compute_activation(
        confidence=1.0, created_at=old, retrieved_count=0, link_count=0, now=NOW,
    )
    assert result.recency_factor < 0.001
    assert result.final_score < 0.001


def test_high_retrieved_count():
    result = compute_activation(
        confidence=1.0, created_at=NOW, retrieved_count=20, link_count=0, now=NOW,
    )
    assert result.access_frequency == pytest.approx(1.0, abs=0.01)
    # 0.5 + 0.3*1.0 + 0.2*0.0 = 0.8
    assert result.final_score == pytest.approx(0.8, abs=0.01)


def test_high_link_count():
    result = compute_activation(
        confidence=1.0, created_at=NOW, retrieved_count=0, link_count=10, now=NOW,
    )
    assert result.connectivity_factor == pytest.approx(1.0, abs=0.01)
    # 0.5 + 0.3*0.0 + 0.2*1.0 = 0.7
    assert result.final_score == pytest.approx(0.7, abs=0.01)


def test_custom_half_life():
    one_day_ago = "2026-03-07T00:00:00+00:00"
    result = compute_activation(
        confidence=1.0, created_at=one_day_ago, retrieved_count=0, link_count=0,
        half_life_days=1.0, now=NOW,
    )
    assert result.recency_factor == pytest.approx(0.5, abs=0.01)


def test_zero_confidence():
    result = compute_activation(
        confidence=0.0, created_at=NOW, retrieved_count=10, link_count=5, now=NOW,
    )
    assert result.final_score == 0.0


def test_all_fields_populated():
    result = compute_activation(
        confidence=0.9, created_at=NOW, retrieved_count=5, link_count=3, now=NOW,
    )
    assert result.memory_id == ""
    assert result.base_score == 0.9
    assert result.recency_factor > 0
    assert result.access_frequency > 0
    assert result.connectivity_factor > 0
    assert result.final_score > 0


def test_combined_high_scores():
    result = compute_activation(
        confidence=1.0, created_at=NOW, retrieved_count=20, link_count=10, now=NOW,
    )
    # 1.0 * 1.0 * (0.5 + 0.3 + 0.2) = 1.0
    assert result.final_score == pytest.approx(1.0, abs=0.01)
