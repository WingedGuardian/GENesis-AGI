"""Tests for PublishManager."""

from __future__ import annotations

import pytest

from genesis.modules.content_pipeline.publisher import PublishManager
from genesis.modules.content_pipeline.types import Script


@pytest.fixture
def sample_script():
    return Script(
        id="s1",
        idea_id="idea1",
        content="Test content for publishing",
        platform="linkedin",
        created_at="2026-04-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_publish_creates_entries(db, sample_script):
    mgr = PublishManager(db)
    results = await mgr.publish(sample_script, ["linkedin", "twitter"])
    assert len(results) == 2
    assert all(r.status == "draft" for r in results)
    assert results[0].platform == "linkedin"
    assert results[1].platform == "twitter"
    assert all(r.idea_id == "idea1" for r in results)


@pytest.mark.asyncio
async def test_get_publishes_by_idea(db, sample_script):
    mgr = PublishManager(db)
    await mgr.publish(sample_script, ["linkedin"])
    results = await mgr.get_publishes(idea_id="idea1")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_get_publishes_by_status(db, sample_script):
    mgr = PublishManager(db)
    pubs = await mgr.publish(sample_script, ["linkedin"])
    await mgr.update_publish_status(pubs[0].id, "published")

    drafts = await mgr.get_publishes(status="draft")
    assert len(drafts) == 0
    published = await mgr.get_publishes(status="published")
    assert len(published) == 1
    assert published[0].published_at is not None


@pytest.mark.asyncio
async def test_update_publish_status_missing(db):
    mgr = PublishManager(db)
    assert await mgr.update_publish_status("nope", "published") is False
