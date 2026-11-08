"""Tests for outreach MCP tool implementations."""

import pytest

from genesis.mcp.outreach_mcp import mcp


@pytest.mark.asyncio
async def test_tools_registered():
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    expected = {"outreach_send", "outreach_queue", "outreach_engagement",
                "outreach_preferences", "outreach_digest"}
    assert expected.issubset(set(tools.keys()))


@pytest.mark.asyncio
async def test_outreach_send_without_pipeline():
    tools = {tool.name: tool for tool in await mcp.list_tools()}
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

    tools = {tool.name: tool for tool in await mcp.list_tools()}
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

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    await tools["outreach_preferences"].fn(
        action="set", preferences={"rate_limits": {"agent_daily_per_peer": 5}},
    )
    assert saved[0].agent_daily_per_peer == 5


@pytest.mark.asyncio
async def test_preferences_set_round_trips_every_unmentioned_field(monkeypatch):
    """#3685: a set call must carry forward EVERY field it did not mention.

    The pre-#3687 set branch rebuilt OutreachConfig(...) field by field and
    omitted the required notification_daily, so every set raised TypeError.
    The fields a hand-listed rebuild can silently reset - voice_alert_ids,
    voice_hours, delivery_routing, immediate_escalation_alerts and
    agent_daily_per_peer - only survive because the merged config is built
    with dataclasses.replace(current, ...). Give every field a distinctive
    non-default value, set only quiet hours, and compare field by field so
    a future field added to OutreachConfig is covered automatically.
    """
    import dataclasses

    import genesis.mcp.outreach_mcp as m
    import genesis.outreach.config as c

    current = dataclasses.replace(
        c._DEFAULTS,
        quiet_hours=c.QuietHours(start="03:00", end="04:00"),
        channel_preferences={"default": "whatsapp", "marketing": "telegram", "custom": "discord"},
        thresholds={"blocker": 0.11, "alert": 0.22, "surplus": 0.33, "digest": 0.44},
        max_daily=111,
        surplus_daily=112,
        content_daily=113,
        notification_daily=114,
        morning_report_time="05:45",
        engagement_timeout_hours=115,
        engagement_poll_minutes=116,
        immediate_escalation_alerts=("custom:escalation",),
        voice_alert_ids=("custom:voice",),
        voice_hours=(11, 23),
        delivery_routing={"default": "dm", "alerts": "both"},
        agent_daily_per_peer=117,
    )
    saved = []
    monkeypatch.setattr(m, "_config", current)
    monkeypatch.setattr(m, "_pipeline", None)
    monkeypatch.setattr(c, "save_outreach_config", lambda cfg, path=None: saved.append(cfg))

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    result = await tools["outreach_preferences"].fn(
        action="set", preferences={"quiet_hours": {"start": "23:00", "end": "06:00"}},
    )

    assert result["status"] == "ok"
    (cfg,) = saved
    # The one field the caller mentioned changed...
    assert cfg.quiet_hours.start == "23:00"
    assert cfg.quiet_hours.end == "06:00"
    # ...and every other field round-tripped untouched, checked over the
    # dataclass itself so new fields are covered without editing this test.
    for field in dataclasses.fields(c.OutreachConfig):
        if field.name == "quiet_hours":
            continue
        assert getattr(cfg, field.name) == getattr(current, field.name), field.name
    # The fields #3685 named, asserted explicitly so the intent is greppable.
    assert cfg.notification_daily == 114
    assert cfg.voice_alert_ids == ("custom:voice",)
    assert cfg.voice_hours == (11, 23)
    assert cfg.delivery_routing == {"default": "dm", "alerts": "both"}
    assert cfg.agent_daily_per_peer == 117
