"""Tests for outreach MCP tool implementations."""

import pytest

from genesis.mcp.outreach_mcp import mcp


@pytest.mark.asyncio
async def test_tools_registered():
    tools = await mcp.get_tools()
    expected = {"outreach_send", "outreach_queue", "outreach_engagement",
                "outreach_preferences", "outreach_digest"}
    assert expected.issubset(set(tools.keys()))


@pytest.mark.asyncio
async def test_outreach_send_without_pipeline():
    tools = await mcp.get_tools()
    result = await tools["outreach_send"].fn(
        message="Test", category="surplus", channel="telegram",
    )
    assert "not initialized" in result.lower() or "error" in result.lower()


@pytest.mark.asyncio
async def test_preferences_set_keeps_fields_it_was_not_given(monkeypatch):
    """A set that touches only quiet hours must not reset other fields to defaults."""
    import dataclasses

    import genesis.mcp.outreach_mcp as m
    import genesis.outreach.config as c

    current = dataclasses.replace(
        c._DEFAULTS,
        agent_daily_per_peer=2,
        notification_daily=7,
        voice_hours=(10, 22),
    )
    saved = []
    monkeypatch.setattr(m, "_config", current)
    monkeypatch.setattr(m, "_pipeline", None)
    monkeypatch.setattr(c, "save_outreach_config", lambda cfg, path=None: saved.append(cfg))

    tools = await mcp.get_tools()
    result = await tools["outreach_preferences"].fn(
        action="set", preferences={"quiet_hours": {"start": "23:00", "end": "06:00"}},
    )

    assert result["status"] == "ok"
    (cfg,) = saved
    assert cfg.quiet_hours.start == "23:00"
    assert cfg.agent_daily_per_peer == 2
    assert cfg.notification_daily == 7
    assert cfg.voice_hours == (10, 22)
    got = await tools["outreach_preferences"].fn(action="get")
    assert got["rate_limits"]["agent_daily_per_peer"] == 2


@pytest.mark.asyncio
async def test_preferences_set_can_change_the_peer_cap(monkeypatch):
    import genesis.mcp.outreach_mcp as m
    import genesis.outreach.config as c

    saved = []
    monkeypatch.setattr(m, "_config", c._DEFAULTS)
    monkeypatch.setattr(m, "_pipeline", None)
    monkeypatch.setattr(c, "save_outreach_config", lambda cfg, path=None: saved.append(cfg))

    tools = await mcp.get_tools()
    await tools["outreach_preferences"].fn(
        action="set", preferences={"rate_limits": {"agent_daily_per_peer": 5}},
    )
    assert saved[0].agent_daily_per_peer == 5
