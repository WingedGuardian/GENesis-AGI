"""Tests for recon_sources MCP tool."""

import pytest
import yaml

from genesis.mcp import recon_mcp
from genesis.mcp.recon_mcp import mcp


@pytest.fixture
def source_files(tmp_path, monkeypatch):
    sources_path = tmp_path / "recon_sources.yaml"
    sources_path.write_text(yaml.safe_dump({"sources": []}))
    monkeypatch.setattr(recon_mcp, "_SOURCES_PATH", sources_path)

    watchlist_path = tmp_path / "recon_watchlist.yaml"
    watchlist_path.write_text(yaml.safe_dump({
        "projects": [
            {"name": "TestProject", "repo": "test/repo", "track": ["releases"], "priority": "high"},
        ]
    }))
    monkeypatch.setattr(recon_mcp, "_WATCHLIST_PATH", watchlist_path)
    return sources_path


@pytest.fixture
async def tools(source_files):
    return await mcp.get_tools()


async def test_list_returns_watchlist_and_dynamic(tools):
    result = await tools["recon_sources"].fn(action="list")
    assert any(s["name"] == "TestProject" and s["origin"] == "watchlist" for s in result)


async def test_add_dynamic_source(tools, source_files):
    result = await tools["recon_sources"].fn(
        action="add", source={"name": "NewSource", "url": "https://example.com", "type": "rss"},
    )
    assert result["added"] == "NewSource"
    assert result["total_dynamic"] == 1

    listed = await tools["recon_sources"].fn(action="list")
    assert any(s["name"] == "NewSource" and s["origin"] == "dynamic" for s in listed)


async def test_remove_dynamic_source(tools, source_files):
    await tools["recon_sources"].fn(
        action="add", source={"name": "Removable", "url": "x", "type": "rss"},
    )
    result = await tools["recon_sources"].fn(
        action="remove", source={"name": "Removable"},
    )
    assert result["found"] is True
    assert result["total_dynamic"] == 0


async def test_cannot_remove_watchlist_entry(tools):
    result = await tools["recon_sources"].fn(
        action="remove", source={"name": "TestProject"},
    )
    assert "error" in result
    assert "immutable" in result["error"].lower() or "watchlist" in result["error"].lower()


async def test_list_merges_both(tools):
    await tools["recon_sources"].fn(
        action="add", source={"name": "Dynamic1", "url": "x", "type": "web"},
    )
    result = await tools["recon_sources"].fn(action="list")
    origins = {s["origin"] for s in result}
    assert "watchlist" in origins
    assert "dynamic" in origins
