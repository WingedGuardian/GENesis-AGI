"""Tests for CCInvoker."""

import asyncio
import json
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.cc.exceptions import CCProcessError, CCTimeoutError
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import (
    CCInvocation,
    CCModel,
    EffortLevel,
    StreamEvent,
    clamp_effort,
    model_supports_effort,
)


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


def test_build_args_includes_span_settings(invoker, monkeypatch):
    """Dispatched sessions get --settings pointing at the span-hook file."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(
        inv_mod, "cc_span_settings_path", lambda: "/tmp/cc-span-settings.json",
    )
    args = invoker._build_args(CCInvocation(prompt="hi"))
    assert "--settings" in args
    assert args[args.index("--settings") + 1] == "/tmp/cc-span-settings.json"


def test_build_args_omits_span_settings_when_unavailable(invoker, monkeypatch):
    """No --settings when the span-hook file can't be generated (None)."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda: None)
    args = invoker._build_args(CCInvocation(prompt="hi"))
    assert "--settings" not in args


def _fake_genesis_hook_repo(tmp_path):
    """Create a fake repo root containing a genesis-hook launcher."""
    hook = tmp_path / "repo" / ".claude" / "hooks" / "genesis-hook"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/bash\n")
    return tmp_path / "repo", hook


def test_cc_span_settings_path_generates_file(monkeypatch, tmp_path):
    """Generates a minimal settings file with the span hook at an ABSOLUTE path."""
    import genesis.cc.invoker as inv_mod

    fake_repo, hook = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    result = inv_mod.cc_span_settings_path()
    assert result == str(out)
    data = json.loads(out.read_text())
    entry = data["hooks"]["PostToolUse"][0]
    assert entry["matcher"] == ".*"
    cmd = entry["hooks"][0]["command"]
    # Absolute launcher path, no ${CLAUDE_PROJECT_DIR} (unset in dispatched cwd).
    assert cmd == f"{hook} hooks/cc_span_hook.py"
    assert cmd.startswith("/")
    assert "${CLAUDE_PROJECT_DIR}" not in cmd


def test_cc_span_settings_path_none_when_hook_missing(monkeypatch, tmp_path):
    """Returns None (→ no --settings) when the launcher is absent."""
    import genesis.cc.invoker as inv_mod

    fake_repo = tmp_path / "repo"  # no .claude/hooks/genesis-hook
    fake_repo.mkdir()
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", tmp_path / "x.json")
    assert inv_mod.cc_span_settings_path() is None


def test_cc_span_settings_path_idempotent(monkeypatch, tmp_path):
    """Second call with unchanged content does not rewrite the file."""
    import genesis.cc.invoker as inv_mod

    fake_repo, _ = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    inv_mod.cc_span_settings_path()
    mtime1 = out.stat().st_mtime_ns
    inv_mod.cc_span_settings_path()
    assert out.stat().st_mtime_ns == mtime1


def test_cc_span_settings_path_rewrites_when_stale(monkeypatch, tmp_path):
    """A stale/corrupt file is rewritten to the correct content."""
    import genesis.cc.invoker as inv_mod

    fake_repo, _ = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    inv_mod.cc_span_settings_path()
    out.write_text("STALE")  # corrupt it
    inv_mod.cc_span_settings_path()  # should rewrite
    data = json.loads(out.read_text())
    assert data["hooks"]["PostToolUse"][0]["matcher"] == ".*"


def test_build_env_strips_claudecode(invoker):
    with patch.dict(
        "os.environ",
        {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "HOME": "/home/test"},
    ):
        env = invoker._build_env()
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert env["HOME"] == "/home/test"


def test_build_env_sets_anthropic_base_url(invoker):
    inv = CCInvocation(prompt="hello", anthropic_base_url="http://localhost:8100")
    env = invoker._build_env(inv)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8100"


def test_build_env_omits_anthropic_base_url_when_none(invoker):
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        env = invoker._build_env(inv)
        assert "ANTHROPIC_BASE_URL" not in env


def test_build_env_strips_parent_anthropic_base_url(invoker):
    """Parent env ANTHROPIC_BASE_URL must not leak when field is None."""
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "http://leaked:8100"}):
        env = invoker._build_env(inv)
        assert "ANTHROPIC_BASE_URL" not in env


def test_build_env_sets_bash_allowlist(invoker):
    """Steward-style invocations export GENESIS_BASH_ALLOWLIST for the hook."""
    inv = CCInvocation(prompt="hello", bash_allowlist=("gh",))
    env = invoker._build_env(inv)
    assert env["GENESIS_BASH_ALLOWLIST"] == "gh"


def test_build_env_omits_bash_allowlist_when_empty(invoker):
    """Default (no allowlist) must NOT set the env var, and must not leak parent."""
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {"GENESIS_BASH_ALLOWLIST": "leaked"}):
        env = invoker._build_env(inv)
        assert "GENESIS_BASH_ALLOWLIST" not in env


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
    assert not output.via_proxy


@pytest.mark.asyncio
async def test_run_via_proxy_sets_flag(invoker):
    """When anthropic_base_url is set, output.via_proxy should be True."""
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "proxied response",
            "session_id": "sess-proxy-1",
            "total_cost_usd": 0.05,
            "duration_ms": 1000,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {"claude-sonnet-4-6": {}},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_line.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run(
            CCInvocation(
                prompt="hello",
                anthropic_base_url="http://localhost:8100",
            )
        )
    assert output.via_proxy is True
    assert output.text == "proxied response"


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


def test_classify_error_thinking_block(invoker):
    """Thinking-block corruption on resume classified as session error."""
    from genesis.cc.exceptions import CCSessionError

    err = invoker._classify_error(
        "thinking blocks cannot be modified after initial creation"
    )
    assert isinstance(err, CCSessionError)


def test_classify_error_thinking_block_from_stdout(invoker):
    """Thinking-block signal can appear in stdout (streaming mode)."""
    from genesis.cc.exceptions import CCSessionError

    err = invoker._classify_error(
        "",
        stdout_text="Error: thinking blocks cannot be modified after initial creation",
    )
    assert isinstance(err, CCSessionError)


# --- interrupt() tests ---


def _live_proc():
    p = MagicMock()
    p.returncode = None
    return p


@pytest.mark.asyncio
async def test_interrupt_sends_sigint():
    inv = CCInvoker()
    mock_proc = _live_proc()
    inv._register_proc("k", mock_proc)
    await inv.interrupt()  # no key → most-recent live
    mock_proc.send_signal.assert_called_once_with(signal.SIGINT)


@pytest.mark.asyncio
async def test_interrupt_noop_when_idle():
    inv = CCInvoker()
    await inv.interrupt()  # empty registry — should not raise


@pytest.mark.asyncio
async def test_interrupt_noop_when_finished():
    inv = CCInvoker()
    mock_proc = MagicMock()
    mock_proc.returncode = 0  # Already exited
    inv._active_procs["k"] = mock_proc  # bypass prune to assert no-signal
    await inv.interrupt()
    mock_proc.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_targets_keyed_proc_not_others():
    """cc-loop-01: /stop with a session key hits THAT proc, not a concurrent one."""
    inv = CCInvoker()
    proc_a, proc_b = _live_proc(), _live_proc()
    inv._register_proc("session-a", proc_a)
    inv._register_proc("session-b", proc_b)
    await inv.interrupt("session-a")
    proc_a.send_signal.assert_called_once_with(signal.SIGINT)
    proc_b.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_no_key_targets_most_recent_live():
    inv = CCInvoker()
    proc_a, proc_b = _live_proc(), _live_proc()
    inv._register_proc("background", proc_a)
    inv._register_proc("foreground", proc_b)  # registered last
    await inv.interrupt()
    proc_b.send_signal.assert_called_once_with(signal.SIGINT)
    proc_a.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_unknown_key_is_noop():
    inv = CCInvoker()
    inv._register_proc("session-a", _live_proc())
    await inv.interrupt("does-not-exist")  # no matching proc — no raise, no signal
    assert inv._active_procs["session-a"].send_signal.call_count == 0


def test_register_prunes_dead_entries():
    """The registry only ever holds live procs (safety net for un-popped keys)."""
    inv = CCInvoker()
    dead = MagicMock()
    dead.returncode = 1
    inv._active_procs["stale"] = dead
    inv._register_proc("fresh", _live_proc())
    assert "stale" not in inv._active_procs
    assert "fresh" in inv._active_procs


@pytest.mark.asyncio
async def test_run_registers_under_session_key_and_clears(invoker):
    """End-to-end: run() registers the proc under invocation.session_key while
    executing, and unregisters it in finally (cc-loop-01)."""
    result_line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s", "total_cost_usd": 0.0,
        "duration_ms": 1, "usage": {
            "input_tokens": 1, "output_tokens": 1,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        },
    })
    captured: dict = {}
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.pid = 4242

    async def _capture(*_a, **_k):
        captured["keys"] = list(invoker._active_procs.keys())
        return (result_line.encode(), b"")

    mock_proc.communicate = _capture
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await invoker.run(CCInvocation(prompt="hi", session_key="tg:7:9"))

    assert captured["keys"] == ["tg:7:9"]  # registered under the session key mid-run
    assert invoker._active_procs == {}  # unregistered in finally


@pytest.mark.asyncio
async def test_run_streaming_registers_under_session_key_and_clears(invoker):
    """run_streaming registers under session_key during streaming and clears in finally."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "ok", "session_id": "s1", "total_cost_usd": 0.0,
            "duration_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0
    mock_proc.pid = 5555

    captured: dict = {}

    async def on_event(_ev):
        captured.setdefault("keys", list(invoker._active_procs.keys()))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await invoker.run_streaming(
            CCInvocation(prompt="hi", session_key="tg:3:4"), on_event=on_event,
        )

    assert captured["keys"] == ["tg:3:4"]  # registered during streaming
    assert invoker._active_procs == {}  # cleared in finally


@pytest.mark.asyncio
async def test_interrupt_real_procs_targets_correct_one():
    """E2E with REAL subprocesses + REAL signals: interrupt(keyA) kills A, B survives.

    Proves the per-session registry delivers SIGINT to the user's proc, not a
    concurrent one (cc-loop-01). (systemd-run scope propagation is unchanged by
    this fix — same signal path, different target — and verified at deploy.)
    """
    import os as _os

    inv = CCInvoker()
    procs: dict[str, object] = {}
    try:
        for key in ("session-a", "session-b"):
            p = await asyncio.create_subprocess_exec(
                "sleep", "30",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=_os.setpgrp,
            )
            inv._register_proc(key, p)
            procs[key] = p

        await inv.interrupt("session-a")

        try:
            await asyncio.wait_for(procs["session-a"].wait(), timeout=5)
        except TimeoutError:
            pytest.fail("proc A did not exit after interrupt('session-a')")
        assert procs["session-a"].returncode is not None  # A got SIGINT
        assert procs["session-b"].returncode is None  # B untouched
    finally:
        for p in procs.values():
            if p.returncode is None:
                p.kill()
                await p.wait()


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


def test_classify_error_rate_limit_from_stdout(invoker):
    """Rate-limit signal can appear in stdout (streaming-JSON mode) while
    stderr is empty. Classifier must check both. Observed in practice: CC
    exit=1, empty stderr, rate-limit text only on stdout — previously
    misclassified as CCProcessError and skipped retry path.
    """
    from genesis.cc.exceptions import CCRateLimitError

    err = invoker._classify_error(
        "",
        stdout_text='{"type": "error", "error": "You\'ve hit your limit · resets 8pm"}',
    )
    assert isinstance(err, CCRateLimitError)


def test_classify_error_falls_back_to_stderr_when_stdout_empty(invoker):
    """Backward compatibility: single-arg classifier (stderr only) still works."""
    from genesis.cc.exceptions import CCRateLimitError

    err = invoker._classify_error("hit your limit")
    assert isinstance(err, CCRateLimitError)


def test_classify_error_quota_from_stdout(invoker):
    """Quota exhaustion in stdout should also be classified correctly."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    err = invoker._classify_error(
        "",
        stdout_text="usage limit exceeded for this billing period",
    )
    assert isinstance(err, CCQuotaExhaustedError)


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


# --- on_spawn callback tests ---


def test_invocation_on_spawn_construction():
    """CCInvocation accepts on_spawn as a callable field."""
    async def my_callback(pid: int) -> None:
        pass

    inv = CCInvocation(prompt="hello", on_spawn=my_callback)
    assert inv.on_spawn is my_callback


def test_invocation_on_spawn_excluded_from_eq():
    """on_spawn is excluded from __eq__ (compare=False)."""
    async def cb1(pid: int) -> None:
        pass

    async def cb2(pid: int) -> None:
        pass

    inv1 = CCInvocation(prompt="hello", on_spawn=cb1)
    inv2 = CCInvocation(prompt="hello", on_spawn=cb2)
    assert inv1 == inv2  # compare=False means callbacks don't affect equality


def test_invocation_on_spawn_excluded_from_repr():
    """on_spawn is excluded from repr (repr=False)."""
    async def cb(pid: int) -> None:
        pass

    inv = CCInvocation(prompt="hello", on_spawn=cb)
    assert "on_spawn" not in repr(inv)


@pytest.mark.asyncio
async def test_run_fires_on_spawn_with_pid(invoker):
    """on_spawn callback is called with the subprocess PID."""
    spawned_pids: list[int] = []

    async def on_spawn(pid: int) -> None:
        spawned_pids.append(pid)

    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
        "duration_ms": 100, "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {},
    })
    mock_proc = AsyncMock()
    mock_proc.pid = 42000
    mock_proc.communicate = AsyncMock(
        return_value=(result_json.encode(), b"")
    )
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await invoker.run(CCInvocation(prompt="hello", on_spawn=on_spawn))

    assert spawned_pids == [42000]


@pytest.mark.asyncio
async def test_run_on_spawn_exception_does_not_abort(invoker):
    """on_spawn failure must not kill the subprocess or abort the run."""
    async def bad_callback(pid: int) -> None:
        raise RuntimeError("DB write failed")

    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "ok", "session_id": "s1", "total_cost_usd": 0.01,
        "duration_ms": 100, "usage": {"input_tokens": 5, "output_tokens": 2},
        "modelUsage": {},
    })
    mock_proc = AsyncMock()
    mock_proc.pid = 42001
    mock_proc.communicate = AsyncMock(
        return_value=(result_json.encode(), b"")
    )
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run(CCInvocation(prompt="hello", on_spawn=bad_callback))

    assert output.text == "ok"  # Run completed despite callback failure


@pytest.mark.asyncio
async def test_run_no_on_spawn_callback(invoker):
    """Without on_spawn, run() works exactly as before (backward compat)."""
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
        output = await invoker.run(CCInvocation(prompt="hello"))

    assert output.text == "ok"


# ---------------------------------------------------------------------------
# Model-aware effort guard
# ---------------------------------------------------------------------------

class TestEffortClamping:
    """clamp_effort() / model_supports_effort() and _build_args() effort gating.

    Verified live against the claude CLI on 2026-07-02: `sonnet` → claude-sonnet-5,
    `fable` → claude-fable-5, `opus` → claude-opus-4-8 all accept the full
    low..max range (incl. xhigh/max); haiku (claude-haiku-4-5) uses no effort
    setting, so --effort is omitted for it entirely.
    """

    def test_clamp_effort_opus_passes_xhigh(self):
        assert clamp_effort(CCModel.OPUS, EffortLevel.XHIGH) == EffortLevel.XHIGH

    def test_clamp_effort_opus_passes_max(self):
        assert clamp_effort(CCModel.OPUS, EffortLevel.MAX) == EffortLevel.MAX

    def test_clamp_effort_sonnet_passes_xhigh(self):
        assert clamp_effort(CCModel.SONNET, EffortLevel.XHIGH) == EffortLevel.XHIGH

    def test_clamp_effort_sonnet_passes_max(self):
        assert clamp_effort(CCModel.SONNET, EffortLevel.MAX) == EffortLevel.MAX

    def test_clamp_effort_fable_passes_max(self):
        assert clamp_effort(CCModel.FABLE, EffortLevel.MAX) == EffortLevel.MAX

    def test_clamp_effort_sonnet_low_unchanged(self):
        assert clamp_effort(CCModel.SONNET, EffortLevel.LOW) == EffortLevel.LOW

    def test_haiku_uses_no_effort(self):
        assert model_supports_effort(CCModel.HAIKU) is False
        for model in (CCModel.OPUS, CCModel.SONNET, CCModel.FABLE):
            assert model_supports_effort(model) is True

    def test_build_args_sonnet_xhigh_passthrough(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.SONNET, effort=EffortLevel.XHIGH)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "xhigh"

    def test_build_args_sonnet_max_passthrough(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.SONNET, effort=EffortLevel.MAX)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "max"

    def test_build_args_haiku_omits_effort(self):
        haiku_invoker = CCInvoker(claude_path="/usr/bin/claude")
        inv = CCInvocation(prompt="hi", model=CCModel.HAIKU, effort=EffortLevel.MAX)
        args = haiku_invoker._build_args(inv)
        assert "--effort" not in args

    def test_build_args_opus_xhigh_unchanged(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.OPUS, effort=EffortLevel.XHIGH)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "xhigh"

    def test_build_args_opus_max_unchanged(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.OPUS, effort=EffortLevel.MAX)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "max"

    def test_build_args_fable_max(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.FABLE, effort=EffortLevel.MAX)
        args = invoker._build_args(inv)
        assert args[args.index("--model") + 1] == "fable"
        assert args[args.index("--effort") + 1] == "max"

    def test_build_args_no_clamp_warning_for_sonnet_xhigh(self, invoker, caplog):
        import logging
        inv = CCInvocation(prompt="hi", model=CCModel.SONNET, effort=EffortLevel.XHIGH)
        with caplog.at_level(logging.WARNING, logger="genesis.cc.invoker"):
            invoker._build_args(inv)
        assert not any("clamping" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_streaming_cancelled_kills_subprocess(invoker):
    """Cancellation mid-stream must terminate the CC child.

    The streaming loop's CancelledError path previously only unregistered
    the proc — the child kept running (spending tokens, editing files)
    after the session row was finalized. Mirrors the guarded-killpg
    pattern the TimeoutError path already uses.
    """
    mock_proc = AsyncMock()
    mock_proc.pid = 99999  # real int — killpg(1) would signal EVERYTHING
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()
    mock_proc.terminate = MagicMock()
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    mock_proc.stdin = stdin

    class _CancelledStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            # Simulate task.cancel() delivered at the stdout await point
            raise asyncio.CancelledError

    mock_proc.stdout = _CancelledStream()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
         pytest.raises(asyncio.CancelledError):
        await invoker.run_streaming(CCInvocation(prompt="hello"))

    # getpgid(99999) raises ProcessLookupError -> falls back to proc.kill()
    assert mock_proc.kill.called, (
        "cancelled streaming run must kill the CC subprocess"
    )
