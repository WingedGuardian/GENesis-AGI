"""Tests for SshIPCAdapter — SSH-based IPC for remote Claude Code dispatch."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.modules.external.config import IPCConfig
from genesis.modules.external.ipc import SshIPCAdapter, create_ipc_adapter


class TestSshIPCAdapterInit:
    def test_requires_ssh_host(self):
        config = IPCConfig(method="ssh")
        with pytest.raises(ValueError, match="ssh_host"):
            SshIPCAdapter(config)

    def test_creates_with_valid_config(self):
        config = IPCConfig(
            method="ssh",
            ssh_host="user@host",
            ssh_key="~/.ssh/id_rsa",
            remote_working_dir="/home/user/project",
            remote_claude_path="/usr/local/bin/claude",
        )
        adapter = SshIPCAdapter(config)
        assert adapter.needs_start is False

    @pytest.mark.asyncio
    async def test_start_stop_are_noops(self):
        config = IPCConfig(method="ssh", ssh_host="user@host")
        adapter = SshIPCAdapter(config)
        # Should not raise
        await adapter.start()
        await adapter.stop()


class TestSshIPCAdapterBuildArgs:
    def test_build_ssh_args_with_key(self):
        config = IPCConfig(
            method="ssh",
            ssh_host="user@host",
            ssh_key="/home/test/.ssh/key",
            ssh_connect_timeout=15,
        )
        adapter = SshIPCAdapter(config)
        args = adapter._build_ssh_args("echo hello")
        assert args == [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            "-o", "BatchMode=yes",
            "-i", "/home/test/.ssh/key",
            "user@host",
            "echo hello",
        ]

    def test_build_ssh_args_without_key(self):
        config = IPCConfig(method="ssh", ssh_host="user@host")
        adapter = SshIPCAdapter(config)
        args = adapter._build_ssh_args("ls")
        assert "-i" not in args
        assert "user@host" in args
        assert args[-1] == "ls"


class TestSshIPCAdapterSend:
    @pytest.mark.asyncio
    async def test_send_cc_dispatch(self):
        config = IPCConfig(
            method="ssh",
            ssh_host="user@host",
            ssh_key="/tmp/key",
            remote_working_dir="/home/user/project",
            remote_claude_path="/usr/local/bin/claude",
            timeout=300,
        )
        adapter = SshIPCAdapter(config)

        cc_output = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Hello from remote",
            "session_id": "abc-123",
            "total_cost_usd": 0.05,
            "duration_ms": 2000,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "modelUsage": {"claude-sonnet-4-6": {}},
        })

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (cc_output.encode(), b"")
        mock_proc.returncode = 0

        with patch("genesis.modules.external.ipc.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            result = await adapter.send("dispatch", data={"prompt": "say hello"}, method="CC")

        assert result["text"] == "Hello from remote"
        assert result["session_id"] == "abc-123"
        assert result["cost_usd"] == 0.05
        assert result["input_tokens"] == 10
        assert result["output_tokens"] == 20
        assert result["model_used"] == "claude-sonnet-4-6"
        assert result["is_error"] is False

        # Verify SSH command was constructed correctly
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "ssh"
        # Verify prompt was piped to stdin
        mock_proc.communicate.assert_awaited_once()
        stdin_data = mock_proc.communicate.call_args[1]["input"]
        assert stdin_data == b"say hello"

    @pytest.mark.asyncio
    async def test_send_cc_requires_prompt(self):
        config = IPCConfig(method="ssh", ssh_host="user@host")
        adapter = SshIPCAdapter(config)
        result = await adapter.send("dispatch", data={}, method="CC")
        assert "error" in result
        assert "prompt" in result["error"]

    @pytest.mark.asyncio
    async def test_send_cc_timeout(self):
        config = IPCConfig(method="ssh", ssh_host="user@host", timeout=1)
        adapter = SshIPCAdapter(config)

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("genesis.modules.external.ipc.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await adapter.send(
                "dispatch",
                data={"prompt": "slow task", "timeout_s": 1},
                method="CC",
            )

        assert "error" in result
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_send_cc_nonzero_exit(self):
        config = IPCConfig(method="ssh", ssh_host="user@host")
        adapter = SshIPCAdapter(config)

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"Permission denied")
        mock_proc.returncode = 1

        with patch("genesis.modules.external.ipc.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await adapter.send("dispatch", data={"prompt": "test"}, method="CC")

        assert "error" in result
        assert "exited 1" in result["error"]

    @pytest.mark.asyncio
    async def test_send_shell_command(self):
        config = IPCConfig(method="ssh", ssh_host="user@host", ssh_key="/tmp/key")
        adapter = SshIPCAdapter(config)

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"2.1.126 (Claude Code)\n", b"")
        mock_proc.returncode = 0

        with patch("genesis.modules.external.ipc.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await adapter.send("claude --version", method="SHELL")

        assert result["output"] == "2.1.126 (Claude Code)"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_send_unsupported_method(self):
        config = IPCConfig(method="ssh", ssh_host="user@host")
        adapter = SshIPCAdapter(config)
        result = await adapter.send("/api/foo", method="GET")
        assert "error" in result
        assert "does not support" in result["error"]


class TestSshIPCAdapterParseCCOutput:
    def test_parse_valid_result(self):
        output = json.dumps({
            "type": "result",
            "result": "hello",
            "session_id": "s1",
            "total_cost_usd": 0.1,
            "duration_ms": 1000,
            "usage": {"input_tokens": 5, "output_tokens": 10},
            "modelUsage": {"claude-sonnet-4-6": {}},
            "is_error": False,
        })
        result = SshIPCAdapter._parse_cc_output(output)
        assert result["text"] == "hello"
        assert result["cost_usd"] == 0.1
        assert result["model_used"] == "claude-sonnet-4-6"

    def test_parse_multiline_with_noise(self):
        """Non-JSON lines before the result should be ignored."""
        raw = "some debug output\nwarning: something\n" + json.dumps({
            "type": "result", "result": "ok", "session_id": "s2",
            "total_cost_usd": 0.0, "duration_ms": 100,
            "usage": {}, "modelUsage": {}, "is_error": False,
        })
        result = SshIPCAdapter._parse_cc_output(raw)
        assert result["text"] == "ok"

    def test_parse_fallback_no_json(self):
        result = SshIPCAdapter._parse_cc_output("just plain text output")
        assert result["text"] == "just plain text output"
        assert result["parse_fallback"] is True


class TestSshIPCAdapterHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self):
        config = IPCConfig(
            method="ssh", ssh_host="user@host",
            remote_claude_path="/usr/local/bin/claude",
        )
        adapter = SshIPCAdapter(config)

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"2.1.126\n", b"")
        mock_proc.returncode = 0

        with patch("genesis.modules.external.ipc.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            assert await adapter.health_check("/version", 200) is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        config = IPCConfig(
            method="ssh", ssh_host="user@host",
            remote_claude_path="/usr/local/bin/claude",
        )
        adapter = SshIPCAdapter(config)

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"command not found")
        mock_proc.returncode = 127

        with patch("genesis.modules.external.ipc.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            assert await adapter.health_check("/version", 200) is False


class TestIPCFactory:
    def test_create_ssh_adapter(self):
        config = IPCConfig(method="ssh", ssh_host="user@host")
        adapter = create_ipc_adapter(config)
        assert isinstance(adapter, SshIPCAdapter)
