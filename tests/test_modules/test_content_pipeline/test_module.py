"""Tests for ContentPipelineModule."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.modules.base import CapabilityModule
from genesis.modules.content_pipeline.module import ContentPipelineModule


@pytest.fixture
def module():
    return ContentPipelineModule()


@pytest.fixture
async def registered_module(db):
    """Module registered with a mock runtime that has a real db."""
    mod = ContentPipelineModule()
    runtime = AsyncMock()
    runtime.db = db
    runtime.content_drafter = None
    await mod.register(runtime)
    return mod


def test_protocol_conformance(module):
    assert isinstance(module, CapabilityModule)


def test_name(module):
    assert module.name == "content_pipeline"


def test_enabled_default(module):
    assert module.enabled is False


def test_enabled_setter(module):
    module.enabled = False
    assert module.enabled is False


def test_research_profile(module):
    assert module.get_research_profile_name() == "content-pipeline"


@pytest.mark.asyncio
async def test_register_initializes_components(registered_module):
    mod = registered_module
    assert mod.idea_bank is not None
    assert mod.planner is not None
    assert mod.script_engine is not None
    assert mod.publisher is not None
    assert mod.analytics is not None


@pytest.mark.asyncio
async def test_deregister_cleans_up(registered_module):
    mod = registered_module
    await mod.deregister()
    assert mod.idea_bank is None
    assert mod.planner is None


@pytest.mark.asyncio
async def test_handle_opportunity_content_idea(registered_module):
    mod = registered_module
    result = await mod.handle_opportunity({
        "type": "content_idea",
        "content": "Write about AI safety",
        "tags": ["ai", "safety"],
    })
    assert result is not None
    assert result["type"] == "content_idea_captured"
    assert result["idea_id"]


@pytest.mark.asyncio
async def test_handle_opportunity_trend(registered_module):
    mod = registered_module
    # auto_capture_trends defaults to False — trend is rejected
    result = await mod.handle_opportunity({
        "type": "trend",
        "content": "Trending topic in AI",
    })
    assert result is None

    # Enable the toggle — trend is now captured
    mod.update_config({"auto_capture_trends": True})
    result = await mod.handle_opportunity({
        "type": "trend",
        "content": "Trending topic in AI",
    })
    assert result is not None


@pytest.mark.asyncio
async def test_handle_opportunity_irrelevant(registered_module):
    mod = registered_module
    result = await mod.handle_opportunity({
        "type": "market_signal",
        "content": "BTC up 5%",
    })
    assert result is None


@pytest.mark.asyncio
async def test_handle_opportunity_no_content(registered_module):
    mod = registered_module
    result = await mod.handle_opportunity({"type": "content_idea"})
    assert result is None


@pytest.mark.asyncio
async def test_record_outcome(registered_module):
    mod = registered_module
    await mod.record_outcome({
        "content_id": "c1",
        "platform": "linkedin",
        "views": 100,
        "likes": 10,
    })
    metrics = await mod.analytics.get_metrics("c1")
    assert len(metrics) == 1


@pytest.mark.asyncio
async def test_extract_generalizable_low_engagement(registered_module):
    mod = registered_module
    result = await mod.extract_generalizable({
        "views": 1, "likes": 0, "shares": 0,
    })
    assert result is None


@pytest.mark.asyncio
async def test_extract_generalizable_high_engagement(registered_module):
    mod = registered_module
    result = await mod.extract_generalizable({
        "views": 100, "likes": 20, "shares": 10,
        "platform": "linkedin", "content_type": "article",
    })
    assert result is not None
    assert len(result) > 0
    assert result[0]["source"] == "module:content_pipeline"
