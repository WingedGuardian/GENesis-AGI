"""Tests for genesis.content.drafter."""

import pytest

from genesis.content.drafter import ContentDrafter
from genesis.content.types import DraftRequest, FormatTarget


class TestLoadVoice:
    def test_loads_voice_md(self):
        """_load_voice returns VOICE.md content when present."""
        result = ContentDrafter._load_voice()
        assert result is not None
        assert "Genesis Voice" in result


class TestContentDrafter:
    @pytest.mark.asyncio
    async def test_no_router_fallback(self):
        drafter = ContentDrafter(router=None)
        req = DraftRequest(topic="AI trends", target=FormatTarget.LINKEDIN)
        result = await drafter.draft(req)
        assert result.content.text == "AI trends"
        assert result.raw_draft == "AI trends"

    @pytest.mark.asyncio
    async def test_no_router_formats_for_target(self):
        drafter = ContentDrafter(router=None)
        long_topic = "x" * 5000
        req = DraftRequest(topic=long_topic, target=FormatTarget.TWITTER)
        result = await drafter.draft(req)
        assert len(result.content.text) <= 280
        assert result.content.truncated

    @pytest.mark.asyncio
    async def test_no_router_prefers_context_over_opaque_topic(self):
        """With no router, fall back to the real context (the actual message),
        not the opaque topic like 'Task 3f2a1b9c'."""
        drafter = ContentDrafter(router=None)
        req = DraftRequest(
            topic="Task 3f2a1b9c", context="Task failed: boom",
            target=FormatTarget.TELEGRAM,
        )
        result = await drafter.draft(req)
        assert "Task failed: boom" in result.content.text
        assert "3f2a1b9c" not in result.content.text

    @pytest.mark.asyncio
    async def test_router_failure_falls_back_to_context(self):
        """If the router raises, degrade to the context, never the opaque
        topic (the secondary notification bug)."""

        class BoomRouter:
            async def route_call(self, **kwargs):
                raise RuntimeError("router down")

        drafter = ContentDrafter(router=BoomRouter())
        req = DraftRequest(
            topic="Task 3f2a1b9c", context="Task failed: boom",
            target=FormatTarget.TELEGRAM,
        )
        result = await drafter.draft(req)
        assert "Task failed: boom" in result.content.text
