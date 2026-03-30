"""Tests for tool_discovery — registry population and content-type routing."""

from __future__ import annotations

import pytest

from genesis.learning.tool_discovery import (
    CONTENT_TYPE_ROUTING,
    KNOWN_TOOLS,
    populate_tool_registry,
    record_capability_gap,
    route_by_content_type,
)


def test_route_by_content_type_known():
    tools = route_by_content_type("youtube_video")
    assert tools == ["gemini"]


def test_route_by_content_type_web_page():
    tools = route_by_content_type("web_page")
    assert "firecrawl" in tools
    assert "playwright" in tools


def test_route_by_content_type_unknown():
    assert route_by_content_type("unknown_type") == []


def test_content_type_routing_non_empty():
    for ct, tools in CONTENT_TYPE_ROUTING.items():
        assert len(tools) > 0, f"Content type '{ct}' has no tools"


def test_known_tools_have_required_fields():
    required = {"name", "category", "description", "tool_type"}
    for tool in KNOWN_TOOLS:
        missing = required - set(tool.keys())
        assert not missing, f"Tool {tool.get('name')} missing fields: {missing}"


@pytest.mark.asyncio
async def test_populate_tool_registry(db):
    count = await populate_tool_registry(db)
    assert count == len(KNOWN_TOOLS)


@pytest.mark.asyncio
async def test_populate_tool_registry_idempotent(db):
    await populate_tool_registry(db)
    count2 = await populate_tool_registry(db)
    assert count2 == len(KNOWN_TOOLS)


@pytest.mark.asyncio
async def test_populate_creates_rows(db):
    from genesis.db.crud import tool_registry

    await populate_tool_registry(db)
    all_tools = await tool_registry.list_all(db)
    assert len(all_tools) == len(KNOWN_TOOLS)


@pytest.mark.asyncio
async def test_record_capability_gap(db):
    from genesis.db.crud import capability_gaps

    gap_id = await record_capability_gap(db, "audio", ["whisper"])
    row = await capability_gaps.get_by_id(db, gap_id)
    assert row is not None
    assert "audio" in row["description"]
    assert "whisper" in row["description"]
    assert row["gap_type"] == "capability_gap"


@pytest.mark.asyncio
async def test_record_capability_gap_idempotent(db):
    from genesis.db.crud import capability_gaps

    gap_id1 = await record_capability_gap(db, "audio", ["whisper"])
    gap_id2 = await record_capability_gap(db, "audio", ["whisper", "speechmatics"])
    assert gap_id1 == gap_id2
    row = await capability_gaps.get_by_id(db, gap_id1)
    assert row["frequency"] == 2
