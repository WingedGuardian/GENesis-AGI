"""Tests for ego notification pipeline.

Covers:
- _process_notifications() — submitting notifications to outreach pipeline
- Cycle output wiring — notifications parsed after proposals
- Governance behavior for NOTIFICATION category
- Self-notification hack removal
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.outreach.types import OutreachCategory, OutreachRequest

# ---------------------------------------------------------------------------
# _process_notifications tests
# ---------------------------------------------------------------------------


class TestProcessNotifications:
    """Tests for EgoSession._process_notifications()."""

    @pytest.fixture()
    def session(self):
        """Minimal EgoSession mock with outreach pipeline."""
        from genesis.ego.session import EgoSession

        s = object.__new__(EgoSession)
        s._outreach_pipeline = AsyncMock()
        s._outreach_pipeline.submit = AsyncMock()
        s._db = AsyncMock()
        return s

    @pytest.mark.asyncio
    async def test_notification_submits_to_pipeline(self, session):
        """Valid notification should be submitted as NOTIFICATION category."""
        notifications = [{"content": "Backup complete", "urgency": "low"}]
        await session._process_notifications(notifications)

        session._outreach_pipeline.submit.assert_called_once()
        request = session._outreach_pipeline.submit.call_args[0][0]
        assert isinstance(request, OutreachRequest)
        assert request.category == OutreachCategory.NOTIFICATION
        assert request.signal_type == "ego_notification"
        assert request.topic == "Backup complete"
        assert request.context == "Backup complete"

    @pytest.mark.asyncio
    async def test_empty_notifications_no_calls(self, session):
        """Empty list should not trigger any pipeline calls."""
        await session._process_notifications([])
        session._outreach_pipeline.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_notification_skipped(self, session):
        """Non-dict and empty-content notifications are skipped."""
        notifications = [
            "not a dict",
            {"content": ""},
            {"content": "  "},
            {"no_content_key": True},
        ]
        await session._process_notifications(notifications)
        session._outreach_pipeline.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_error_does_not_crash(self, session):
        """Pipeline errors should be logged, not raised."""
        session._outreach_pipeline.submit.side_effect = RuntimeError("boom")
        notifications = [{"content": "Test notification"}]
        # Should not raise
        await session._process_notifications(notifications)

    @pytest.mark.asyncio
    async def test_pipeline_unavailable_logs_warning(self):
        """When pipeline is None, notifications are skipped with warning."""
        from genesis.ego.session import EgoSession

        s = object.__new__(EgoSession)
        s._outreach_pipeline = None
        s._db = AsyncMock()

        with patch("genesis.ego.session.logger") as mock_logger:
            await s._process_notifications([{"content": "Test"}])
            mock_logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_multiple_notifications_counted(self, session):
        """Multiple valid notifications should all be submitted."""
        notifications = [
            {"content": "First notification"},
            {"content": "Second notification"},
            {"content": "Third notification"},
        ]
        await session._process_notifications(notifications)
        assert session._outreach_pipeline.submit.call_count == 3

    @pytest.mark.asyncio
    async def test_topic_truncated_to_100_chars(self, session):
        """Topic field should be first 100 chars of content."""
        long_content = "x" * 200
        notifications = [{"content": long_content}]
        await session._process_notifications(notifications)

        request = session._outreach_pipeline.submit.call_args[0][0]
        assert len(request.topic) == 100
        assert request.context == long_content


# ---------------------------------------------------------------------------
# NOTIFICATION governance config tests
# ---------------------------------------------------------------------------


class TestNotificationGovernance:
    """Tests for NOTIFICATION category governance behavior."""

    def test_notification_category_exists(self):
        """NOTIFICATION should be a valid OutreachCategory."""
        assert OutreachCategory.NOTIFICATION == "notification"

    def test_notification_dedup_window(self):
        """ego_notification signal type should have 12h dedup window."""
        from genesis.outreach.governance import _DEDUP_WINDOWS

        assert _DEDUP_WINDOWS.get("ego_notification") == 12

    def test_notification_config_field(self):
        """OutreachConfig should have notification_daily field."""
        import dataclasses

        from genesis.outreach.config import OutreachConfig
        fields = {f.name for f in dataclasses.fields(OutreachConfig)}
        assert "notification_daily" in fields

    def test_notification_threshold_zero(self):
        """Default config should have notification threshold 0.0."""
        from genesis.outreach.config import load_outreach_config

        # Load from repo config
        config = load_outreach_config()
        assert config.thresholds.get("notification") == 0.0


# ---------------------------------------------------------------------------
# Self-notification hack removal verification
# ---------------------------------------------------------------------------


class TestSelfNotificationHackRemoved:
    """Verify the auto-complete hack for zero-cost outreach is gone."""

    def test_no_self_notification_pattern(self):
        """The 'auto-completed: delivered via proposal digest' pattern
        should no longer exist in sweep_approved_proposals."""
        import inspect

        from genesis.ego.session import EgoSession

        source = inspect.getsource(EgoSession.sweep_approved_proposals)
        assert "self-notification" not in source
        assert "auto-completed: delivered via proposal digest" not in source
