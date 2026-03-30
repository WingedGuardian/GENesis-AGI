"""Tests for ProviderActivityTracker."""

import time
from unittest.mock import patch

from genesis.observability.provider_activity import ProviderActivityTracker


def test_empty_summary():
    tracker = ProviderActivityTracker()
    result = tracker.summary("nonexistent")
    assert result["calls"] == 0
    assert result["errors"] == 0
    assert result["error_rate"] == 0.0
    assert result["avg_latency_ms"] == 0.0
    assert result["cache_hits"] == 0


def test_record_and_summary():
    tracker = ProviderActivityTracker()
    tracker.record("ollama", latency_ms=100, success=True)
    tracker.record("ollama", latency_ms=200, success=True)
    tracker.record("ollama", latency_ms=300, success=False)

    s = tracker.summary("ollama")
    assert s["calls"] == 3
    assert s["errors"] == 1
    assert s["error_rate"] == round(1 / 3, 4)
    assert s["avg_latency_ms"] == 200.0  # (100+200+300)/3
    assert s["cache_hits"] == 0


def test_cache_hit_tracking():
    tracker = ProviderActivityTracker()
    tracker.record("ollama", latency_ms=0, success=True, cache_hit=True)
    tracker.record("ollama", latency_ms=0, success=True, cache_hit=True)
    tracker.record("ollama", latency_ms=150, success=True)

    s = tracker.summary("ollama")
    assert s["calls"] == 3
    assert s["cache_hits"] == 2
    assert s["cache_hit_rate"] == round(2 / 3, 4)
    # Latency avg excludes cache hits
    assert s["avg_latency_ms"] == 150.0


def test_p95_latency():
    tracker = ProviderActivityTracker()
    # 20 calls: 1-20ms
    for i in range(1, 21):
        tracker.record("test", latency_ms=float(i), success=True)

    s = tracker.summary("test")
    # p95 index = int(20 * 0.95) - 1 = 18 → sorted[18] = 19.0
    assert s["p95_latency_ms"] == 19.0


def test_window_eviction():
    tracker = ProviderActivityTracker(window_seconds=10)

    # Patch time.monotonic to control timestamps
    base = time.monotonic()
    with patch("genesis.observability.provider_activity.time") as mock_time:
        mock_time.monotonic.return_value = base
        tracker.record("p", latency_ms=100, success=True)

        # Move forward 11 seconds — beyond window
        mock_time.monotonic.return_value = base + 11
        tracker.record("p", latency_ms=200, success=True)

        s = tracker.summary("p")
        assert s["calls"] == 1  # Old entry evicted
        assert s["avg_latency_ms"] == 200.0


def test_all_providers_summary():
    tracker = ProviderActivityTracker()
    tracker.record("ollama", latency_ms=100, success=True)
    tracker.record("mistral", latency_ms=200, success=True)

    results = tracker.summary()
    assert isinstance(results, list)
    assert len(results) == 2
    names = {r["provider"] for r in results}
    assert names == {"mistral", "ollama"}


def test_all_cache_hits_zero_latency():
    """When all calls are cache hits, latency stats should be zero."""
    tracker = ProviderActivityTracker()
    tracker.record("p", latency_ms=0, success=True, cache_hit=True)
    tracker.record("p", latency_ms=0, success=True, cache_hit=True)

    s = tracker.summary("p")
    assert s["avg_latency_ms"] == 0.0
    assert s["p95_latency_ms"] == 0.0
    assert s["cache_hit_rate"] == 1.0
