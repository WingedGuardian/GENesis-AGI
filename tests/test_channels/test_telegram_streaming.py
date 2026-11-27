"""DraftStreamer: one ``[Model / Effort]`` header per reply (#4885).

The relay adds its own prefix to drafts and to the final reply, so a model
that writes the header itself must have its copy dropped — but only when it
names the running model. An ordinary heading such as ``[Step 2 / max]`` is
never a header.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from genesis.channels.telegram._handler_messages import _with_model_prefix
from genesis.channels.telegram.transport.streaming import _strip_duplicate_header, DraftStreamer


def _streamer(prefix="[Sonnet / Medium]"):
    return DraftStreamer(bot=MagicMock(), chat_id=1, draft_id=3, prefix=prefix)


def test_strips_exact_duplicate_of_the_prefix():
    assert _strip_duplicate_header("[Sonnet / Medium]\nHello", "[Sonnet / Medium]") == "Hello"


def test_strips_new_family_label_matching_the_running_model():
    # claude-mythos-5, shown as "Mythos 5": the old four-family list missed it.
    assert (
        _strip_duplicate_header("[Mythos 5 / High]\nHello", "[Mythos 5 / High]") == "Hello"
    )


def test_strips_provider_qualified_label_matching_the_running_model():
    assert (
        _strip_duplicate_header(
            "[Openrouter/Claude-Mythos-5 / High]\nHello",
            "[Openrouter/Claude-Mythos-5 / High]",
        )
        == "Hello"
    )


def test_keeps_ordinary_heading():
    # The wrong fix: widening the match to any "Word N" label deletes headings.
    assert (
        _strip_duplicate_header("[Step 2 / max]\nHello", "[Sonnet / Medium]")
        == "[Step 2 / max]\nHello"
    )


def test_keeps_other_models_header():
    assert (
        _strip_duplicate_header("[Opus / High]\nHello", "[Sonnet / Medium]")
        == "[Opus / High]\nHello"
    )


def test_keeps_header_when_no_prefix():
    assert (
        _strip_duplicate_header("[Sonnet / Medium]\nHello", "")
        == "[Sonnet / Medium]\nHello"
    )


def test_header_only_leaves_empty():
    assert _strip_duplicate_header("[Sonnet / Medium]", "[Sonnet / Medium]") == ""


def test_header_on_second_line_is_kept():
    text = "Hello\n[Sonnet / Medium]\nWorld"
    assert _strip_duplicate_header(text, "[Sonnet / Medium]") == text


@pytest.mark.asyncio
async def test_draft_shows_one_header_when_model_writes_its_own():
    streamer = _streamer()
    await streamer.on_text("[Sonnet / Medium]\nHello")
    assert streamer._compose_draft() == "[Sonnet / Medium]\n\nHello"
    # The accumulated text itself is unchanged; only the render strips.
    assert streamer.accumulated_text == "[Sonnet / Medium]\nHello"


@pytest.mark.asyncio
async def test_draft_with_new_family_label():
    streamer = _streamer(prefix="[Mythos 5 / High]")
    await streamer.on_text("[Mythos 5 / High]\nHello")
    assert streamer._compose_draft() == "[Mythos 5 / High]\n\nHello"


@pytest.mark.asyncio
async def test_draft_without_prefix_keeps_model_header():
    streamer = _streamer(prefix="")
    await streamer.on_text("[Sonnet / Medium]\nHello")
    assert streamer._compose_draft() == "[Sonnet / Medium]\nHello"


def test_final_reply_shows_one_header():
    assert (
        _with_model_prefix(_streamer(), "[Sonnet / Medium]\nHello")
        == "[Sonnet / Medium]\nHello"
    )


def test_final_reply_without_self_header_gets_prefix():
    assert _with_model_prefix(_streamer(), "Hello") == "[Sonnet / Medium]\nHello"


def test_final_reply_without_streamer_is_unchanged():
    assert _with_model_prefix(None, "Hello") == "Hello"
