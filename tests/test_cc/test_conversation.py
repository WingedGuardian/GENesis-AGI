"""Tests for ConversationLoop."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.cc.conversation import ConversationLoop
from genesis.cc.exceptions import CCProcessError, CCSessionError, CCTimeoutError
from genesis.cc.invoker import CCInvoker
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.cc.types import (
    CCModel,
    CCOutput,
    ChannelType,
    EffortLevel,
)
from genesis.db.crud import cc_sessions


def _make_output(text="Hello from Genesis", session_id="cc-sess-1", **kwargs):
    defaults = dict(
        session_id=session_id,
        text=text,
        model_used="sonnet",
        cost_usd=0.01,
        input_tokens=10,
        output_tokens=5,
        duration_ms=100,
        exit_code=0,
        is_error=False,
    )
    defaults.update(kwargs)
    return CCOutput(**defaults)


@pytest.fixture
def mock_invoker():
    inv = AsyncMock(spec=CCInvoker)
    inv.run = AsyncMock(return_value=_make_output())
    return inv


@pytest.fixture
def assembler(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Genesis.")
    (tmp_path / "CONVERSATION.md").write_text("Be concise.")
    return SystemPromptAssembler(identity_dir=tmp_path)


@pytest.fixture
async def loop(db, mock_invoker, assembler):
    return ConversationLoop(
        db=db,
        invoker=mock_invoker,
        assembler=assembler,
        day_boundary_hour=0,
    )


@pytest.mark.asyncio
async def test_first_message_creates_session(loop, mock_invoker):
    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert result == "Hello from Genesis"
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.system_prompt is not None
    assert "You are Genesis." in call_args.system_prompt
    assert call_args.resume_session_id is None


@pytest.mark.asyncio
async def test_second_message_resumes(loop, mock_invoker, db):
    await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    mock_invoker.run.return_value = _make_output(text="Second reply", session_id="cc-sess-1")
    result = await loop.handle_message("follow up", user_id="u1", channel=ChannelType.TERMINAL)
    assert result == "Second reply"
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.resume_session_id == "cc-sess-1"
    assert call_args.system_prompt is None


@pytest.mark.asyncio
async def test_cc_session_id_stored(loop, mock_invoker, db):
    await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    # Verify cc_session_id was stored
    rows = await cc_sessions.query_active(db)
    assert len(rows) == 1
    assert rows[0]["cc_session_id"] == "cc-sess-1"


@pytest.mark.asyncio
async def test_model_override(loop, mock_invoker):
    await loop.handle_message("/model opus hello", user_id="u1", channel=ChannelType.TERMINAL)
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.model == CCModel.OPUS


@pytest.mark.asyncio
async def test_effort_override(loop, mock_invoker):
    await loop.handle_message("/effort high hello", user_id="u1", channel=ChannelType.TERMINAL)
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.effort == EffortLevel.HIGH


@pytest.mark.asyncio
async def test_morning_reset(loop, mock_invoker, db):
    # Create a session from yesterday
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await cc_sessions.create(
        db,
        id="old-sess",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="terminal",
        started_at=yesterday,
        last_activity_at=yesterday,
        source_tag="foreground",
    )
    await cc_sessions.update_cc_session_id(db, "old-sess", cc_session_id="cc-old")

    result = await loop.handle_message("good morning", user_id="u1", channel=ChannelType.TERMINAL)
    assert result == "Hello from Genesis"
    # Old session should be completed
    old = await cc_sessions.get_by_id(db, "old-sess")
    assert old["status"] == "completed"
    # New invocation should NOT resume old session
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.resume_session_id is None
    assert call_args.system_prompt is not None


@pytest.mark.asyncio
async def test_cc_error_returns_message(loop, mock_invoker):
    mock_invoker.run.side_effect = CCProcessError("CC failed")
    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "[Genesis error:" in result


@pytest.mark.asyncio
async def test_cc_timeout_returns_friendly_message(loop, mock_invoker):
    mock_invoker.run.side_effect = CCTimeoutError("Timeout after 300s")
    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_empty_response(loop, mock_invoker):
    mock_invoker.run.return_value = _make_output(text="")
    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert result == ""


@pytest.mark.asyncio
async def test_cleaned_text_used_as_prompt(loop, mock_invoker):
    await loop.handle_message("/model opus What is 2+2?", user_id="u1", channel=ChannelType.TERMINAL)
    call_args = mock_invoker.run.call_args[0][0]
    assert call_args.prompt == "What is 2+2?"
    assert call_args.model == CCModel.OPUS


@pytest.mark.asyncio
async def test_model_persists_across_messages(loop, mock_invoker):
    """Model set on first message should persist to second message without override."""
    await loop.handle_message("/model opus hello", user_id="u1", channel=ChannelType.TERMINAL)
    first_call = mock_invoker.run.call_args[0][0]
    assert first_call.model == CCModel.OPUS

    mock_invoker.run.return_value = _make_output(text="Second reply")
    await loop.handle_message("follow up", user_id="u1", channel=ChannelType.TERMINAL)
    second_call = mock_invoker.run.call_args[0][0]
    assert second_call.model == CCModel.OPUS


@pytest.mark.asyncio
async def test_triage_pipeline_fires_on_response(db, mock_invoker, assembler):
    """When triage_pipeline is provided, it's called after successful response."""
    import asyncio

    triage_mock = AsyncMock()
    loop_with_triage = ConversationLoop(
        db=db,
        invoker=mock_invoker,
        assembler=assembler,
        day_boundary_hour=0,
        triage_pipeline=triage_mock,
    )
    await loop_with_triage.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)

    # Triage fires via asyncio.create_task — give it a tick to run
    await asyncio.sleep(0.05)

    triage_mock.assert_awaited_once()
    args = triage_mock.call_args[0]
    assert args[1] == "hello"  # user_text
    assert args[2] == "terminal"  # channel


@pytest.mark.asyncio
async def test_should_reset_handles_naive_timestamp(loop, mock_invoker, db):
    """Morning reset should handle naive (no timezone) ISO timestamps."""
    yesterday_naive = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    await cc_sessions.create(
        db,
        id="naive-sess",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="terminal",
        started_at=yesterday_naive,
        last_activity_at=yesterday_naive,
        source_tag="foreground",
    )
    await cc_sessions.update_cc_session_id(db, "naive-sess", cc_session_id="cc-naive")

    # Should not crash on naive datetime comparison
    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert result == "Hello from Genesis"
    old = await cc_sessions.get_by_id(db, "naive-sess")
    assert old["status"] == "completed"


# --- Resume-failure recovery tests ---


async def _setup_session_with_cc_sid(db, *, user_id="u1", cc_session_id="cc-stale"):
    """Create an active foreground session that already has a cc_session_id."""
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        db,
        id="resume-sess",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="active",
        user_id=user_id,
        channel="terminal",
        started_at=now,
        last_activity_at=now,
        source_tag="foreground",
    )
    await cc_sessions.update_cc_session_id(db, "resume-sess", cc_session_id=cc_session_id)


@pytest.mark.asyncio
async def test_resume_failure_retries_fresh(loop, mock_invoker, db):
    """When resume fails (raises CCError), loop recovers and retries fresh."""
    await _setup_session_with_cc_sid(db)

    # First call (resume) raises, second call (fresh) succeeds
    call_count = 0
    async def _run_side_effect(inv):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise CCSessionError("session not found")
        return _make_output(text="Fresh response", session_id="cc-new-1")

    mock_invoker.run = AsyncMock(side_effect=_run_side_effect)

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert result == "Fresh response"

    # Old session should be failed
    old = await cc_sessions.get_by_id(db, "resume-sess")
    assert old["status"] == "failed"

    # Invoker was called twice: once with resume, once without
    assert call_count == 2
    first_call = mock_invoker.run.call_args_list[0][0][0]
    assert first_call.resume_session_id == "cc-stale"
    second_call = mock_invoker.run.call_args_list[1][0][0]
    assert second_call.resume_session_id is None
    assert second_call.system_prompt is not None


@pytest.mark.asyncio
async def test_resume_failure_streaming_retries_fresh(loop, mock_invoker, db):
    """Streaming path also recovers from resume failure."""
    await _setup_session_with_cc_sid(db)

    call_count = 0
    async def _stream_side_effect(inv, on_event=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise CCSessionError("session expired")
        return _make_output(text="Fresh streaming", session_id="cc-new-2")

    mock_invoker.run_streaming = AsyncMock(side_effect=_stream_side_effect)

    result = await loop.handle_message_streaming(
        "hello", user_id="u1", channel=ChannelType.TERMINAL,
    )
    assert result == "Fresh streaming"

    old = await cc_sessions.get_by_id(db, "resume-sess")
    assert old["status"] == "failed"

    assert call_count == 2


@pytest.mark.asyncio
async def test_fresh_session_failure_not_retried(loop, mock_invoker):
    """Error on first message (no resume) returns error without retry."""
    mock_invoker.run.side_effect = CCProcessError("process crashed")

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "[Genesis error:" in result

    # Only one call — no retry since it wasn't a resume attempt
    assert mock_invoker.run.call_count == 1


@pytest.mark.asyncio
async def test_resume_retry_also_fails(loop, mock_invoker, db):
    """When both resume and fresh retry fail, error is returned (no loop)."""
    await _setup_session_with_cc_sid(db)

    mock_invoker.run.side_effect = CCProcessError("process crashed")

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "[Genesis error:" in result
    assert "process crashed" in result

    # Exactly 2 calls — resume attempt + one retry, no infinite loop
    assert mock_invoker.run.call_count == 2


# --- Session lock tests ---


@pytest.mark.asyncio
async def test_session_lock_identity(loop):
    """Same session_id returns same lock object; different IDs return different locks."""
    lock1 = loop._get_lock("sess-1")
    lock2 = loop._get_lock("sess-1")
    assert lock1 is lock2
    lock3 = loop._get_lock("sess-2")
    assert lock3 is not lock1


# --- Model/effort change feedback tests ---


@pytest.mark.asyncio
async def test_model_change_emits_system_notice(loop, mock_invoker, db):
    """Switching model in streaming emits a system_notice event."""
    from genesis.cc.types import StreamEvent

    mock_invoker.run_streaming = AsyncMock(return_value=_make_output(text="Done"))

    collected: list[StreamEvent] = []

    async def on_event(ev: StreamEvent):
        collected.append(ev)

    # First message: sets session to default sonnet
    await loop.handle_message_streaming(
        "hello", user_id="u1", channel=ChannelType.TERMINAL,
        on_event=on_event,
    )

    collected.clear()

    # Second message: switch to opus
    await loop.handle_message_streaming(
        "/model opus what now?", user_id="u1", channel=ChannelType.TERMINAL,
        on_event=on_event,
    )

    notices = [e for e in collected if e.event_type == "system_notice"]
    assert len(notices) >= 1
    assert "Opus" in notices[0].text


@pytest.mark.asyncio
async def test_effort_change_emits_system_notice(loop, mock_invoker, db):
    """Switching effort in streaming emits a system_notice event."""
    from genesis.cc.types import StreamEvent

    mock_invoker.run_streaming = AsyncMock(return_value=_make_output(text="Done"))

    collected: list[StreamEvent] = []

    async def on_event(ev: StreamEvent):
        collected.append(ev)

    # First message: default medium effort
    await loop.handle_message_streaming(
        "hello", user_id="u1", channel=ChannelType.TERMINAL,
        on_event=on_event,
    )

    collected.clear()

    # Second message: switch effort
    await loop.handle_message_streaming(
        "/effort high tell me more", user_id="u1", channel=ChannelType.TERMINAL,
        on_event=on_event,
    )

    notices = [e for e in collected if e.event_type == "system_notice"]
    assert len(notices) >= 1
    assert "high" in notices[0].text


# ── Contingency fallback tests ─────────────────────────────────────────


def _make_contingency_mock(*, success=True, content="Kimi fallback response", model="kimi-k2.5"):
    from genesis.cc.contingency import ContingencyResult

    mock = AsyncMock()
    mock.dispatch_conversation = AsyncMock(
        return_value=ContingencyResult(
            success=success,
            content=content,
            model=model,
            reason="" if success else "routing failed",
        )
    )
    return mock


@pytest.fixture
async def loop_with_contingency(db, mock_invoker, assembler):
    contingency = _make_contingency_mock()
    return ConversationLoop(
        db=db,
        invoker=mock_invoker,
        assembler=assembler,
        day_boundary_hour=0,
        contingency=contingency,
    ), contingency


@pytest.mark.asyncio
async def test_quota_exhausted_triggers_contingency(loop_with_contingency, mock_invoker):
    """When CC raises CCQuotaExhaustedError, contingency dispatcher is called."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    loop, contingency = loop_with_contingency
    mock_invoker.run.side_effect = CCQuotaExhaustedError("usage limit reached")

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "Contingency mode" in result
    assert "Kimi fallback response" in result
    contingency.dispatch_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_triggers_contingency(loop_with_contingency, mock_invoker):
    """When CC raises CCRateLimitError, contingency dispatcher is called."""
    from genesis.cc.exceptions import CCRateLimitError

    loop, contingency = loop_with_contingency
    mock_invoker.run.side_effect = CCRateLimitError("429 rate limited")

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "Contingency mode" in result
    contingency.dispatch_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_quota_exhausted_contingency_fails(loop_with_contingency, mock_invoker):
    """When both CC and contingency fail, error string is returned."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    loop, contingency = loop_with_contingency
    mock_invoker.run.side_effect = CCQuotaExhaustedError("usage limit")
    contingency.dispatch_conversation = AsyncMock(
        return_value=_make_contingency_mock(success=False).dispatch_conversation.return_value
    )

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "Rate limit reached" in result


@pytest.mark.asyncio
async def test_timeout_skips_contingency(loop_with_contingency, mock_invoker):
    """CCTimeoutError should NOT trigger contingency (timeout is not an availability issue)."""
    loop, contingency = loop_with_contingency
    mock_invoker.run.side_effect = CCTimeoutError("Timeout after 300s")

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "timed out" in result.lower()
    contingency.dispatch_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_contingency_configured(db, mock_invoker, assembler):
    """When no contingency dispatcher is configured, error string is returned directly."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    loop = ConversationLoop(
        db=db,
        invoker=mock_invoker,
        assembler=assembler,
        day_boundary_hour=0,
        contingency=None,
    )
    mock_invoker.run.side_effect = CCQuotaExhaustedError("usage limit")

    result = await loop.handle_message("hello", user_id="u1", channel=ChannelType.TERMINAL)
    assert "Rate limit reached" in result


@pytest.mark.asyncio
async def test_streaming_quota_contingency(loop_with_contingency, mock_invoker):
    """Streaming handler also triggers contingency on quota exhaustion."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    loop, contingency = loop_with_contingency
    mock_invoker.run_streaming = AsyncMock(side_effect=CCQuotaExhaustedError("usage limit"))

    result = await loop.handle_message_streaming(
        "hello", user_id="u1", channel=ChannelType.TERMINAL,
    )
    assert "Contingency mode" in result
    contingency.dispatch_conversation.assert_awaited_once()
