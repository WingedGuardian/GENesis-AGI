"""Tests for AnalyticsTracker."""

from __future__ import annotations

import pytest

from genesis.modules.content_pipeline.analytics import AnalyticsTracker


@pytest.mark.asyncio
async def test_record_and_get_metrics(db):
    tracker = AnalyticsTracker(db)
    await tracker.record_metrics("c1", "linkedin", views=100, likes=10, shares=5)
    metrics = await tracker.get_metrics("c1")
    assert len(metrics) == 1
    assert metrics[0].views == 100
    assert metrics[0].likes == 10
    assert metrics[0].shares == 5


@pytest.mark.asyncio
async def test_multiple_snapshots(db):
    tracker = AnalyticsTracker(db)
    await tracker.record_metrics("c1", "linkedin", views=50)
    await tracker.record_metrics("c1", "linkedin", views=100)
    metrics = await tracker.get_metrics("c1")
    assert len(metrics) == 2


@pytest.mark.asyncio
async def test_generate_insights_empty(db):
    tracker = AnalyticsTracker(db)
    insights = await tracker.generate_insights()
    assert insights.period == "last_7_days"
    assert insights.top_performing == []
    assert len(insights.recommendations) == 1


@pytest.mark.asyncio
async def test_generate_insights_with_data(db):
    tracker = AnalyticsTracker(db)
    await tracker.record_metrics("c1", "linkedin", views=1000, likes=50, shares=20)
    await tracker.record_metrics("c2", "twitter", views=10, likes=1, shares=0)
    await tracker.record_metrics("c3", "email", views=500, likes=20, shares=10)
    await tracker.record_metrics("c4", "telegram", views=5, likes=0, shares=0)

    insights = await tracker.generate_insights(period_days=7)
    assert "c1" in insights.top_performing
    assert len(insights.top_performing) <= 3
