"""Tests for CheckpointManager."""

from unittest.mock import AsyncMock

import pytest

from genesis.cc.checkpoint import CheckpointManager
from genesis.cc.types import CCOutput, MessageType
from genesis.db.crud import message_queue


@pytest.fixture
def mock_invoker():
    return AsyncMock()


@pytest.fixture
def mock_session_mgr():
    return AsyncMock()


@pytest.fixture
async def ckpt(db, mock_invoker, mock_session_mgr):
    return CheckpointManager(
        db=db, session_manager=mock_session_mgr, invoker=mock_invoker,
    )


async def test_poll_pending_messages(db, ckpt):
    await message_queue.create(
        db,
        id="mq-1",
        source="cc_background",
        target="user",
        message_type="question",
        priority="high",
        content='{"text":"which?"}',
        session_id="sess-1",
        created_at="2026-03-07T12:00:00",
    )
    msgs = await ckpt.poll_pending_messages(target="user")
    assert len(msgs) == 1
    assert msgs[0]["id"] == "mq-1"


async def test_deliver_response(db, ckpt):
    await message_queue.create(
        db,
        id="mq-1",
        source="cc_background",
        target="user",
        message_type="question",
        priority="high",
        content='{"text":"which?"}',
        session_id="sess-1",
        created_at="2026-03-07T12:00:00",
    )
    await ckpt.deliver_response(message_id="mq-1", response={"choice": 1})
    row = await message_queue.get_by_id(db, "mq-1")
    assert row["response"] is not None
    assert row["responded_at"] is not None


async def test_resume_session(db, ckpt, mock_invoker):
    mock_invoker.run = AsyncMock(
        return_value=CCOutput(
            session_id="sess-1",
            text="Resumed!",
            model_used="sonnet",
            cost_usd=0.01,
            input_tokens=50,
            output_tokens=20,
            duration_ms=1000,
            exit_code=0,
        ),
    )
    output = await ckpt.resume_session(
        session_id="sess-1", response_text="Option 1",
    )
    assert output.text == "Resumed!"
    mock_invoker.run.assert_called_once()


def test_should_checkpoint_question():
    assert CheckpointManager.should_checkpoint(MessageType.QUESTION) is True
    assert CheckpointManager.should_checkpoint(MessageType.DECISION) is True
    assert CheckpointManager.should_checkpoint(MessageType.ERROR) is True
    assert CheckpointManager.should_checkpoint(MessageType.FINDING) is False
    assert CheckpointManager.should_checkpoint(MessageType.PROGRESS) is False


def test_should_checkpoint_string():
    assert CheckpointManager.should_checkpoint("question") is True
    assert CheckpointManager.should_checkpoint("progress") is False
