"""Tests for ConversationLoop triage pipeline integration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from genesis.cc.conversation import ConversationLoop
from genesis.cc.types import CCOutput, ChannelType


def _make_loop(*, triage_pipeline=None):
    """Build a ConversationLoop with mocked dependencies."""
    db = AsyncMock()
    invoker = AsyncMock()
    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value="system prompt")
    invoker.run = AsyncMock(
        return_value=CCOutput(
            text="Hello!", session_id="sess-1",
            model_used="sonnet", cost_usd=0.0,
            input_tokens=10, output_tokens=5,
            duration_ms=100, exit_code=0,
            is_error=False, error_message=None,
        ),
    )
    loop = ConversationLoop(
        db=db,
        invoker=invoker,
        assembler=assembler,
        triage_pipeline=triage_pipeline,
    )
    # Mock session manager to return a fresh session (no cc_session_id yet)
    # Include model/effort so _persist_overrides doesn't try to update
    loop._session_mgr = AsyncMock()
    loop._session_mgr.get_or_create_foreground = AsyncMock(
        return_value={"id": "sess-0001", "cc_session_id": None, "model": None, "effort": None},
    )
    return loop


@pytest.mark.asyncio
async def test_triage_pipeline_called_after_response():
    """Triage pipeline should be called with output, user text, and channel."""
    called_with = []

    async def fake_triage(output, user_text, channel):
        called_with.append((output, user_text, channel))

    loop = _make_loop(triage_pipeline=fake_triage)

    with patch("genesis.cc.conversation.cc_sessions") as mock_crud:
        mock_crud.get_active_foreground = AsyncMock(return_value=None)
        mock_crud.update_cc_session_id = AsyncMock()
        mock_crud.update_model_effort = AsyncMock()
        result = await loop.handle_message(
            "hi there", user_id="u1", channel=ChannelType.TERMINAL,
        )

    # Let the fire-and-forget task run
    await asyncio.sleep(0)

    assert "Hello!" in result
    assert len(called_with) == 1
    output, text, channel = called_with[0]
    assert output.text == "Hello!"
    assert text == "hi there"
    assert channel == str(ChannelType.TERMINAL)


@pytest.mark.asyncio
async def test_triage_pipeline_failure_does_not_affect_response():
    """A failing triage pipeline must not crash the main flow."""

    async def bad_triage(output, user_text, channel):
        raise RuntimeError("triage boom")

    loop = _make_loop(triage_pipeline=bad_triage)

    with patch("genesis.cc.conversation.cc_sessions") as mock_crud:
        mock_crud.get_active_foreground = AsyncMock(return_value=None)
        mock_crud.update_cc_session_id = AsyncMock()
        mock_crud.update_model_effort = AsyncMock()
        result = await loop.handle_message(
            "hello", user_id="u1", channel=ChannelType.TERMINAL,
        )

    # Let the fire-and-forget task run (and fail silently)
    await asyncio.sleep(0)

    assert "Hello!" in result


@pytest.mark.asyncio
async def test_triage_pipeline_none_no_error():
    """When triage_pipeline is None, no error should occur."""
    loop = _make_loop(triage_pipeline=None)

    with patch("genesis.cc.conversation.cc_sessions") as mock_crud:
        mock_crud.get_active_foreground = AsyncMock(return_value=None)
        mock_crud.update_cc_session_id = AsyncMock()
        mock_crud.update_model_effort = AsyncMock()
        result = await loop.handle_message(
            "yo", user_id="u1", channel=ChannelType.TERMINAL,
        )

    assert "Hello!" in result
