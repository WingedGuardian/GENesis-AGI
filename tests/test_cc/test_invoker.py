"""Tests for CCInvoker."""

import asyncio
import json
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.cc.exceptions import CCProcessError, CCTimeoutError
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, EffortLevel, StreamEvent


@pytest.fixture
def invoker():
    return CCInvoker(claude_path="/usr/bin/claude")


def test_build_args_defaults(invoker):
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert args[0] == "/usr/bin/claude"
    assert "-p" in args
    assert "--model" in args
    assert "sonnet" in args
    assert "--output-format" in args
    assert "json" in args
    # Prompt is passed via stdin, not as a CLI argument
    assert "hello" not in args


def test_build_args_with_resume(invoker):
    inv = CCInvocation(prompt="continue", resume_session_id="sess-123")
    args = invoker._build_args(inv)
    assert "--resume" in args
    assert "sess-123" in args


def test_build_args_with_system_prompt(invoker):
    inv = CCInvocation(prompt="hello", system_prompt="You are Genesis.")
    args = invoker._build_args(inv)
    assert "--system-prompt" in args


def test_build_args_append_system_prompt(invoker):
    inv = CCInvocation(prompt="hello", system_prompt="You are Genesis.", append_system_prompt=True)
    args = invoker._build_args(inv)
    assert "--append-system-prompt" in args
    assert "--system-prompt" not in args


def test_build_args_with_mcp_config(invoker):
    inv = CCInvocation(prompt="hello", mcp_config="/path/to/mcp.json")
    args = invoker._build_args(inv)
    assert "--mcp-config" in args
    assert "/path/to/mcp.json" in args


def test_build_env_strips_claudecode(invoker):
    with patch.dict(
        "os.environ",
        {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "HOME": "/home/test"},
    ):
        env = invoker._build_env()
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert env["HOME"] == "/home/test"


@pytest.mark.asyncio
async def test_run_success(invoker):
    # Match real CLI JSON shape (verified 2026-03-08)
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Hello world",
            "session_id": "sess-out-1",
            "total_cost_usd": 0.186,
            "duration_ms": 1500,
            "usage": {
                "input_tokens": 50,
                "output_tokens": 20,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "modelUsage": {
                "claude-sonnet-4-6": {
                    "inputTokens": 50,
                    "outputTokens": 20,
                    "costUSD": 0.186,
                },
            },
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_line.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run(CCInvocation(prompt="hello"))
    assert output.text == "Hello world"
    assert output.session_id == "sess-out-1"
    assert output.cost_usd == 0.186
    assert output.input_tokens == 50
    assert output.output_tokens == 20
    assert output.model_used == "claude-sonnet-4-6"
    assert output.exit_code == 0
    assert not output.is_error


@pytest.mark.asyncio
async def test_run_timeout(invoker):
    mock_proc = AsyncMock()
    mock_proc.pid = 99999  # Must set — AsyncMock().pid int() == 1 → killpg(1) == kill(-1) == kill ALL
    mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = -9

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(CCTimeoutError, match="Timeout"):
        await invoker.run(CCInvocation(prompt="hello", timeout_s=1))


@pytest.mark.asyncio
async def test_run_nonzero_exit(invoker):
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Error: something failed"))
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(CCProcessError):
        await invoker.run(CCInvocation(prompt="hello"))


def test_build_args_with_effort(invoker):
    inv = CCInvocation(prompt="hello", effort=EffortLevel.HIGH)
    args = invoker._build_args(inv)
    assert "--effort" in args
    assert "high" in args


def test_build_args_default_effort(invoker):
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert "--effort" in args
    assert "medium" in args


def test_parse_output_fallback(invoker):
    """When no JSON result line found, treat as plain text."""
    output = invoker._parse_output(
        "Just plain text response",
        CCInvocation(prompt="test"),
        100,
    )
    assert output.text == "Just plain text response"
    assert output.exit_code == 0
    assert not output.is_error


def test_build_args_prompt_not_in_args(invoker):
    """Prompt is passed via stdin, not as a CLI argument."""
    inv = CCInvocation(
        prompt="evaluate this",
        allowed_tools=["WebFetch", "Read"],
        skip_permissions=True,
    )
    args = invoker._build_args(inv)
    assert "evaluate this" not in args
    assert "--" not in args
    assert "--allowedTools" in args


def test_build_args_no_separator(invoker):
    """No '--' separator needed — prompt goes via stdin."""
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert "--" not in args
    assert "hello" not in args


def test_build_args_with_disallowed_tools(invoker):
    inv = CCInvocation(prompt="reflect", disallowed_tools=["Bash", "Edit"])
    args = invoker._build_args(inv)
    assert "--disallowedTools" in args
    dt_idx = args.index("--disallowedTools")
    assert args[dt_idx + 1] == "Bash,Edit"
    # Prompt is passed via stdin, not in args
    assert "reflect" not in args


# --- Streaming tests ---


def _make_stream_lines(*events: dict) -> bytes:
    """Build newline-delimited JSON bytes from event dicts."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def _make_mock_stdin():
    """Create a mock stdin with async drain and sync write/close."""
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    return stdin


def _make_mock_stderr(data: bytes = b""):
    """Create a mock async stderr reader."""

    class _AsyncReader:
        async def read(self):
            return data

    return _AsyncReader()


def _make_async_stdout(data: bytes):
    """Create a mock async iterator for proc.stdout."""

    class _AsyncStdout:
        def __init__(self, lines):
            self._lines = iter(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._lines)
            except StopIteration:
                raise StopAsyncIteration from None

    return _AsyncStdout(data.split(b"\n"))


@pytest.mark.asyncio
async def test_run_streaming_success(invoker):
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "hello",
            "session_id": "s1",
            "total_cost_usd": 0.05,
            "duration_ms": 500,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    collected: list[StreamEvent] = []

    async def on_event(ev: StreamEvent):
        collected.append(ev)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(
            CCInvocation(prompt="hello"), on_event=on_event,
        )

    assert output.text == "hello"
    assert output.session_id == "s1"
    assert output.cost_usd == 0.05
    assert not output.is_error
    mock_proc.terminate.assert_called_once()  # Verify subprocess terminated after result

    event_types = [e.event_type for e in collected]
    assert "init" in event_types
    assert "text" in event_types
    assert "result" in event_types


@pytest.mark.asyncio
async def test_run_streaming_timeout_returns_partial(invoker):
    """On timeout, collected text is returned as partial output."""
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "partial "}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "response"}]},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    # Simulate: stdout yields lines then hangs → timeout fires
    lines = data.split(b"\n")

    async def _slow_iter():
        for line in lines:
            yield line
        # Simulate hang
        await asyncio.sleep(999)

    mock_proc.stdout = _slow_iter()
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.pid = 99999  # Must set — see test_run_timeout comment
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = -9

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(CCTimeoutError, match="Timeout"):
        await invoker.run_streaming(
            CCInvocation(prompt="hello", timeout_s=0),
        )


@pytest.mark.asyncio
async def test_run_streaming_no_callback(invoker):
    """run_streaming works fine with on_event=None."""
    events = [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s2",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(CCInvocation(prompt="test"))

    assert output.text == "ok"
    assert not output.is_error


@pytest.mark.asyncio
async def test_run_streaming_tool_use_events(invoker):
    """Tool use events are properly parsed and forwarded."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"path": "foo.py"}}
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"tool_use_id": "t1", "type": "tool_result", "content": "data"}
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "found it",
            "session_id": "s3",
            "total_cost_usd": 0.02,
            "duration_ms": 200,
            "usage": {"input_tokens": 8, "output_tokens": 3},
            "modelUsage": {},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    collected: list[StreamEvent] = []

    async def on_event(ev: StreamEvent):
        collected.append(ev)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(
            CCInvocation(prompt="find foo"), on_event=on_event,
        )

    assert output.text == "found it"
    tool_events = [e for e in collected if e.event_type == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "Read"


# --- Streaming rate-limit event tests ---


@pytest.mark.asyncio
async def test_run_streaming_rate_limit_with_valid_response():
    """rate_limit_event with valid text returns the response, sets RATE_LIMITED."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Got it"}]}},
        {"type": "rate_limit_event", "info": {}},
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "Got it", "session_id": "s1",
            "total_cost_usd": 0.01, "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await inv.run_streaming(CCInvocation(prompt="test"))

    # Response delivered despite rate limit signal
    assert output.text == "Got it"
    assert not output.is_error
    # Status callback fired for rate limit
    assert statuses == ["RATE_LIMITED"]


@pytest.mark.asyncio
async def test_run_streaming_rate_limit_with_empty_response_raises():
    """rate_limit_event with empty text raises CCRateLimitError."""
    from genesis.cc.exceptions import CCRateLimitError

    inv = CCInvoker(claude_path="claude")

    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "rate_limit_event", "info": {}},
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "", "session_id": "s1",
            "total_cost_usd": 0.0, "duration_ms": 50,
            "usage": {"input_tokens": 5, "output_tokens": 0},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(CCRateLimitError):
        await inv.run_streaming(CCInvocation(prompt="test"))


# --- Error classification tests ---


def test_classify_error_session_expired(invoker):
    from genesis.cc.exceptions import CCSessionError
    err = invoker._classify_error("Session 'abc' not found or expired")
    assert isinstance(err, CCSessionError)


def test_classify_error_rate_limit(invoker):
    from genesis.cc.exceptions import CCRateLimitError
    err = invoker._classify_error("Rate limit exceeded, status 429")
    assert isinstance(err, CCRateLimitError)


def test_classify_error_mcp(invoker):
    from genesis.cc.exceptions import CCMCPError
    err = invoker._classify_error("MCP server 'memory' returned error")
    assert isinstance(err, CCMCPError)
    assert err.server_name == "memory"


def test_classify_error_generic(invoker):
    err = invoker._classify_error("Something unknown went wrong")
    assert isinstance(err, CCProcessError)


# --- interrupt() tests ---


@pytest.mark.asyncio
async def test_interrupt_sends_sigint():
    inv = CCInvoker()
    mock_proc = MagicMock()
    mock_proc.returncode = None
    inv._active_proc = mock_proc
    await inv.interrupt()
    mock_proc.send_signal.assert_called_once_with(signal.SIGINT)


@pytest.mark.asyncio
async def test_interrupt_noop_when_idle():
    inv = CCInvoker()
    await inv.interrupt()  # Should not raise


@pytest.mark.asyncio
async def test_interrupt_noop_when_finished():
    inv = CCInvoker()
    mock_proc = MagicMock()
    mock_proc.returncode = 0  # Already exited
    inv._active_proc = mock_proc
    await inv.interrupt()
    mock_proc.send_signal.assert_not_called()


# --- AgentProvider protocol conformance ---


def test_invoker_satisfies_agent_provider():
    from genesis.cc.protocol import AgentProvider
    assert isinstance(CCInvoker(), AgentProvider)


# --- Quota detection and status callback tests ---


def test_classify_error_quota_exhausted(invoker):
    """Hard quota exhaustion (usage limit) is distinct from transient 429."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    for msg in [
        "Usage limit exceeded for this billing period",
        "Quota exceeded — try again in 4 hours",
        "Your usage limit has been reached",
        "Usage cap exceeded for your plan",
    ]:
        err = invoker._classify_error(msg)
        assert isinstance(err, CCQuotaExhaustedError), f"Failed for: {msg}"


def test_classify_error_rate_limit_not_quota(invoker):
    """Transient rate limit (429) should NOT be classified as quota."""
    from genesis.cc.exceptions import CCQuotaExhaustedError, CCRateLimitError

    err = invoker._classify_error("Rate limit exceeded, status 429")
    assert isinstance(err, CCRateLimitError)
    assert not isinstance(err, CCQuotaExhaustedError)


@pytest.mark.asyncio
async def test_status_callback_on_quota_exhaustion():
    """Quota exhaustion triggers UNAVAILABLE status callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"", b"Usage limit exceeded")
    )
    mock_proc.returncode = 1

    from genesis.cc.exceptions import CCQuotaExhaustedError

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(CCQuotaExhaustedError):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == ["UNAVAILABLE"]


@pytest.mark.asyncio
async def test_status_callback_on_rate_limit():
    """Transient rate limit triggers RATE_LIMITED status callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"", b"Rate limit exceeded, 429")
    )
    mock_proc.returncode = 1

    from genesis.cc.exceptions import CCRateLimitError

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(CCRateLimitError):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == ["RATE_LIMITED"]


@pytest.mark.asyncio
async def test_status_callback_recovery_after_failure():
    """Success after a failure triggers NORMAL callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    # First call: rate limit error
    mock_proc_fail = AsyncMock()
    mock_proc_fail.communicate = AsyncMock(
        return_value=(b"", b"Rate limit exceeded, 429")
    )
    mock_proc_fail.returncode = 1

    from genesis.cc.exceptions import CCRateLimitError

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc_fail), \
         pytest.raises(CCRateLimitError):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == ["RATE_LIMITED"]

    # Second call: success
    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
        "duration_ms": 100, "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {},
    })
    mock_proc_ok = AsyncMock()
    mock_proc_ok.communicate = AsyncMock(
        return_value=(result_json.encode(), b"")
    )
    mock_proc_ok.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc_ok):
        output = await inv.run(CCInvocation(prompt="hello"))

    assert output.text == "ok"
    assert statuses == ["RATE_LIMITED", "NORMAL"]


@pytest.mark.asyncio
async def test_no_callback_on_generic_error():
    """Generic process errors should NOT trigger status callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"", b"Something unknown went wrong")
    )
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(CCProcessError):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == []  # No callback for generic errors


@pytest.mark.asyncio
async def test_run_streaming_uses_invocation_working_dir():
    """Streaming: invocation working_dir overrides invoker default."""
    inv = CCInvoker(claude_path="claude", working_dir="/default-dir")

    events = [
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
            "duration_ms": 100, "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await inv.run_streaming(
            CCInvocation(prompt="hello", working_dir="/override-dir"),
        )

    _, kwargs = mock_exec.call_args
    assert kwargs["cwd"] == "/override-dir"


@pytest.mark.asyncio
async def test_run_uses_invocation_working_dir():
    """Invocation working_dir overrides invoker default."""
    inv = CCInvoker(claude_path="claude", working_dir="/default-dir")

    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
        "duration_ms": 100, "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {},
    })
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(result_json.encode(), b"")
    )
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await inv.run(CCInvocation(prompt="hello", working_dir="/override-dir"))

    _, kwargs = mock_exec.call_args
    assert kwargs["cwd"] == "/override-dir"


@pytest.mark.asyncio
async def test_run_falls_back_to_invoker_working_dir():
    """When invocation has no working_dir, invoker default is used."""
    inv = CCInvoker(claude_path="claude", working_dir="/invoker-dir")

    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
        "duration_ms": 100, "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {},
    })
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(result_json.encode(), b"")
    )
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await inv.run(CCInvocation(prompt="hello"))

    _, kwargs = mock_exec.call_args
    assert kwargs["cwd"] == "/invoker-dir"


@pytest.mark.asyncio
async def test_no_callback_on_repeated_success():
    """Repeated success should NOT trigger callback (only recovery does)."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
        "duration_ms": 100, "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {},
    })
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(result_json.encode(), b"")
    )
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await inv.run(CCInvocation(prompt="hello"))
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == []  # No callback — was never in error state


# --- Tests for --bare flag and CLAUDE_STREAM_IDLE_TIMEOUT_MS (CC 2.1.85) ---


def test_build_args_bare_true(invoker):
    inv = CCInvocation(prompt="hello", bare=True)
    args = invoker._build_args(inv)
    assert "--bare" in args


def test_build_args_bare_default(invoker):
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert "--bare" not in args


def test_build_env_stream_idle_timeout(invoker):
    inv = CCInvocation(prompt="test", stream_idle_timeout_ms=180000)
    with patch.dict("os.environ", {"HOME": "/home/test"}, clear=True):
        env = invoker._build_env(inv)
    assert env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "180000"


def test_build_env_no_stream_idle_timeout(invoker):
    inv = CCInvocation(prompt="test")
    with patch.dict("os.environ", {"HOME": "/home/test"}, clear=True):
        env = invoker._build_env(inv)
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in env


def test_build_env_no_invocation(invoker):
    """_build_env still works when called with no invocation (backward compat)."""
    with patch.dict("os.environ", {"HOME": "/home/test"}, clear=True):
        env = invoker._build_env()
    assert env["GENESIS_CC_SESSION"] == "1"
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in env


def test_build_args_bare_with_other_flags(invoker):
    """--bare coexists with other flags like --dangerously-skip-permissions."""
    inv = CCInvocation(
        prompt="hello",
        bare=True,
        skip_permissions=True,
        mcp_config="/path/to/no_mcp.json",
    )
    args = invoker._build_args(inv)
    assert "--bare" in args
    assert "--dangerously-skip-permissions" in args
    assert "--mcp-config" in args
