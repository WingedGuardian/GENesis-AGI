"""Tests for the topic-root bare-decision nudge.

A bare "yes"/"approve" that quote-replies a decision topic's ROOT (not a
specific item) matches no pending item and, without this guard, would spawn a
context-free conversation session ("no charter — what are you confirming?").
The nudge intercepts it and asks the user to reply to the specific message —
without auto-resolving anything (which would risk acting on the wrong item).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.channels.telegram._handler_messages import (
    _is_topic_root_reply,
    _try_topic_root_decision_nudge,
)

THREADS = {"approvals": 100, "content_review": 200, "ego_proposals": 300}


def _msg(text, thread_id, reply_to_id):
    m = MagicMock()
    m.text = text
    m.message_thread_id = thread_id
    if reply_to_id is None:
        m.reply_to_message = None
    else:
        reply = MagicMock()
        reply.message_id = reply_to_id
        m.reply_to_message = reply
    m.reply_text = AsyncMock()
    return m


def _patch_topics(threads):
    tm = MagicMock()
    tm.get_thread_id.side_effect = lambda k: threads.get(k)
    rt = MagicMock()
    rt.outreach_pipeline = MagicMock()
    rt.outreach_pipeline.topic_manager = tm
    return patch("genesis.runtime.GenesisRuntime.instance", return_value=rt)


def test_is_topic_root_reply():
    assert _is_topic_root_reply(_msg("yes", 100, 100)) is True  # reply_to == thread
    assert _is_topic_root_reply(_msg("yes", 100, 101)) is False  # specific message
    assert _is_topic_root_reply(_msg("yes", None, 100)) is False  # not a topic
    assert _is_topic_root_reply(_msg("yes", 100, None)) is False  # not a reply


@pytest.mark.asyncio
async def test_nudge_fires_on_topic_root_bare_decision():
    msg = _msg("approve", 100, 100)  # topic-root reply in the Approvals topic
    with _patch_topics(THREADS):
        assert await _try_topic_root_decision_nudge(MagicMock(), msg) is True
    msg.reply_text.assert_awaited_once()
    assert "specific message" in msg.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_nudge_fires_in_content_review_and_ego_topics():
    for thread in (THREADS["content_review"], THREADS["ego_proposals"]):
        msg = _msg("yes", thread, thread)
        with _patch_topics(THREADS):
            assert await _try_topic_root_decision_nudge(MagicMock(), msg) is True


@pytest.mark.asyncio
async def test_nudge_skips_non_decision_text():
    """A substantive topic-root message still falls through to conversation."""
    msg = _msg("what is the status of the pending items here?", 100, 100)
    with _patch_topics(THREADS):
        assert await _try_topic_root_decision_nudge(MagicMock(), msg) is False
    msg.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_nudge_skips_specific_message_reply():
    """A quote-reply to a SPECIFIC message is left to the real resolvers."""
    msg = _msg("approve", 100, 555)
    with _patch_topics(THREADS):
        assert await _try_topic_root_decision_nudge(MagicMock(), msg) is False


@pytest.mark.asyncio
async def test_nudge_skips_non_decision_topic():
    """Topic-root bare decision in a non-decision topic → not our concern."""
    msg = _msg("approve", 999, 999)
    with _patch_topics(THREADS):
        assert await _try_topic_root_decision_nudge(MagicMock(), msg) is False
    msg.reply_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "yes, but shorten the title",
        "no, what alternatives do we have?",
        "ok change the wording though",
    ],
)
async def test_nudge_skips_decision_prefixed_substantive_reply(text):
    """A decision-token PREFIX followed by a real instruction must fall through
    to conversation, NOT be swallowed by the nudge. The nudge uses the
    whole-message matcher, so only a standalone bare decision triggers it —
    the leading-token matcher would wrongly consume the instruction here."""
    msg = _msg(text, 100, 100)  # topic-root reply in the Approvals topic
    with _patch_topics(THREADS):
        assert await _try_topic_root_decision_nudge(MagicMock(), msg) is False
    msg.reply_text.assert_not_called()
