"""Tests for CC types."""

from genesis.cc.types import (
    CCInvocation,
    CCModel,
    CCOutput,
    ChannelType,
    EffortLevel,
    IntentResult,
    MessageType,
    SessionStatus,
    SessionType,
    StreamEvent,
    background_session_dir,
)


def test_enums_have_expected_values():
    assert SessionType.FOREGROUND == "foreground"
    assert SessionStatus.CHECKPOINTED == "checkpointed"
    assert MessageType.QUESTION == "question"
    assert CCModel.OPUS == "opus"
    assert EffortLevel.HIGH == "high"


def test_cc_invocation_defaults():
    inv = CCInvocation(prompt="hello")
    assert inv.model == CCModel.SONNET
    assert inv.effort == EffortLevel.MEDIUM
    assert inv.output_format == "json"
    assert inv.timeout_s == 600
    assert inv.working_dir is None


def test_cc_invocation_working_dir():
    inv = CCInvocation(prompt="hello", working_dir="/tmp/bg")
    assert inv.working_dir == "/tmp/bg"


def test_cc_output_frozen():
    out = CCOutput(
        session_id="s1",
        text="hi",
        model_used="sonnet",
        cost_usd=0.0,
        input_tokens=10,
        output_tokens=5,
        duration_ms=100,
        exit_code=0,
    )
    assert out.text == "hi"


def test_intent_result_defaults():
    r = IntentResult(raw_text="hello")
    assert r.model_override is None
    assert r.effort_override is None
    assert not r.resume_requested
    assert not r.task_requested
    assert r.cleaned_text == ""


def test_channel_type():
    assert ChannelType.TELEGRAM == "telegram"
    assert ChannelType.TERMINAL == "terminal"


# --- StreamEvent tests ---


def test_stream_event_from_text():
    raw = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello world"}]},
    }
    ev = StreamEvent.from_raw(raw)
    assert ev.event_type == "text"
    assert ev.text == "hello world"


def test_stream_event_from_tool_use():
    raw = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
            ]
        },
    }
    ev = StreamEvent.from_raw(raw)
    assert ev.event_type == "tool_use"
    assert ev.tool_name == "Bash"
    assert ev.tool_input == {"command": "ls"}


def test_stream_event_from_result():
    raw = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-1",
        "result": "done",
        "total_cost_usd": 0.05,
    }
    ev = StreamEvent.from_raw(raw)
    assert ev.event_type == "result"
    assert ev.session_id == "sess-1"
    assert ev.text == "done"


def test_stream_event_from_init():
    raw = {"type": "system", "subtype": "init", "session_id": "sess-2"}
    ev = StreamEvent.from_raw(raw)
    assert ev.event_type == "init"
    assert ev.session_id == "sess-2"


def test_stream_event_from_tool_result():
    raw = {
        "type": "user",
        "message": {
            "content": [{"tool_use_id": "t1", "type": "tool_result", "content": "ok"}]
        },
    }
    ev = StreamEvent.from_raw(raw)
    assert ev.event_type == "tool_result"


def test_stream_event_from_unknown():
    raw = {"type": "rate_limit_event", "info": {}}
    ev = StreamEvent.from_raw(raw)
    assert ev.event_type == "rate_limit_event"


def test_stream_event_assistant_no_content():
    raw = {"type": "assistant", "message": {"content": []}}
    ev = StreamEvent.from_raw(raw)
    assert ev.event_type == "assistant"


def test_stream_event_thinking_block():
    raw = {
        "type": "assistant",
        "message": {
            "content": [{"type": "thinking", "thinking": "Let me reason..."}]
        },
    }
    event = StreamEvent.from_raw(raw)
    assert event.event_type == "thinking"
    assert event.text == "Let me reason..."


def test_stream_event_thinking_before_text():
    """Thinking block is detected before text block in content list."""
    raw = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "answer"},
            ]
        },
    }
    event = StreamEvent.from_raw(raw)
    assert event.event_type == "thinking"


# --- background_session_dir tests ---


def test_background_session_dir_returns_valid_path():
    """background_session_dir() returns a path ending in .background-sessions and creates it."""
    from pathlib import Path

    result = background_session_dir()
    assert result.endswith(".background-sessions")
    assert Path(result).is_dir()
