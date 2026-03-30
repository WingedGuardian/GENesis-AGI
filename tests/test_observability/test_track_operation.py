"""Tests for track_operation / async_track_operation context managers."""

from __future__ import annotations

import pytest

from genesis.observability.provider_activity import (
    ProviderActivityTracker,
    async_track_operation,
    track_operation,
)


class TestTrackOperation:
    """Sync context manager tests."""

    def test_records_success(self):
        tracker = ProviderActivityTracker()
        with track_operation(tracker, "test.op"):
            pass  # successful operation
        summary = tracker.summary("test.op")
        assert summary["calls"] == 1
        assert summary["errors"] == 0
        assert summary["avg_latency_ms"] >= 0

    def test_records_failure(self):
        tracker = ProviderActivityTracker()
        with pytest.raises(ValueError, match="boom"), track_operation(tracker, "test.op"):
            raise ValueError("boom")
        summary = tracker.summary("test.op")
        assert summary["calls"] == 1
        assert summary["errors"] == 1
        assert summary["error_rate"] == 1.0

    def test_none_tracker_is_noop(self):
        """track_operation with None tracker should be a no-op."""
        with track_operation(None, "test.op"):
            pass  # no error, no tracking

    def test_caller_exception_propagates(self):
        """The caller's exception must propagate, not be swallowed."""
        tracker = ProviderActivityTracker()
        with pytest.raises(RuntimeError, match="real error"), track_operation(tracker, "test.op"):
            raise RuntimeError("real error")

    def test_tracker_error_swallowed(self):
        """If tracker.record() throws, it must not break the caller."""
        tracker = ProviderActivityTracker()
        # Monkey-patch record to raise
        def broken_record(*a, **kw):
            raise RuntimeError("tracker bug")
        tracker.record = broken_record

        # Should NOT raise — tracker error is swallowed
        with track_operation(tracker, "test.op"):
            pass  # operation succeeds despite tracker bug

    def test_tracker_error_does_not_mask_caller_error(self):
        """If both caller and tracker raise, the caller's error wins."""
        tracker = ProviderActivityTracker()
        def broken_record(*a, **kw):
            raise RuntimeError("tracker bug")
        tracker.record = broken_record

        with pytest.raises(ValueError, match="caller error"), track_operation(tracker, "test.op"):
            raise ValueError("caller error")

    def test_provider_name_recorded(self):
        tracker = ProviderActivityTracker()
        with track_operation(tracker, "qdrant.upsert"):
            pass
        with track_operation(tracker, "qdrant.search"):
            pass
        summaries = tracker.summary()
        providers = {s["provider"] for s in summaries}
        assert "qdrant.upsert" in providers
        assert "qdrant.search" in providers


@pytest.mark.asyncio
class TestAsyncTrackOperation:
    """Async context manager tests."""

    async def test_records_success(self):
        tracker = ProviderActivityTracker()
        async with async_track_operation(tracker, "test.async"):
            pass
        summary = tracker.summary("test.async")
        assert summary["calls"] == 1
        assert summary["errors"] == 0

    async def test_records_failure(self):
        tracker = ProviderActivityTracker()
        with pytest.raises(ValueError):
            async with async_track_operation(tracker, "test.async"):
                raise ValueError("async boom")
        summary = tracker.summary("test.async")
        assert summary["calls"] == 1
        assert summary["errors"] == 1

    async def test_none_tracker_is_noop(self):
        async with async_track_operation(None, "test.async"):
            pass
