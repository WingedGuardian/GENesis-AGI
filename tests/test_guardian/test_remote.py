"""Tests for GuardianRemote SSH wrapper."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.remote import GuardianRemote


@pytest.fixture
def remote():
    return GuardianRemote(
        host_ip="192.168.1.100",
        host_user="testuser",
        key_path="/tmp/test_key",
        timeout=5.0,
    )


class TestInit:
    def test_requires_host_ip(self):
        with pytest.raises(ValueError, match="host_ip"):
            GuardianRemote(host_ip="", host_user="user")

    def test_requires_host_user(self):
        with pytest.raises(ValueError, match="host_user"):
            GuardianRemote(host_ip="1.2.3.4", host_user="")

    def test_expands_key_path(self):
        r = GuardianRemote(host_ip="1.2.3.4", host_user="u", key_path="~/my_key")
        assert "~" not in r._key_path


class TestSSHCommand:
    @pytest.mark.asyncio
    async def test_success(self, remote):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b'{"ok": true}', b"")
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            ok, output = await remote._ssh_command("status")
        assert ok is True
        assert output == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_failure(self, remote):
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b'{"ok": false, "error": "denied"}')
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            ok, output = await remote._ssh_command("bad-command")
        assert ok is False
        assert "denied" in output

    @pytest.mark.asyncio
    async def test_timeout(self, remote):
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()  # kill() is sync on asyncio.Process
        mock_proc.wait = AsyncMock()
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            ok, output = await remote._ssh_command("status")
        assert ok is False
        assert output == "timeout"
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_os_error(self, remote):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("No such file"),
        ):
            ok, output = await remote._ssh_command("status")
        assert ok is False
        assert "No such file" in output


class TestStatus:
    @pytest.mark.asyncio
    async def test_parses_json(self, remote):
        with patch.object(
            remote, "_ssh_command",
            return_value=(True, json.dumps({"current_state": "healthy"})),
        ):
            result = await remote.status()
        assert result["current_state"] == "healthy"

    @pytest.mark.asyncio
    async def test_non_json_response(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(True, "not json")):
            result = await remote.status()
        assert result["current_state"] == "unknown"

    @pytest.mark.asyncio
    async def test_unreachable(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(False, "timeout")):
            result = await remote.status()
        assert result["current_state"] == "unreachable"


class TestRestart:
    @pytest.mark.asyncio
    async def test_success(self, remote):
        with patch.object(
            remote, "_ssh_command",
            return_value=(True, '{"ok": true, "action": "restart-timer"}'),
        ):
            assert await remote.restart() is True

    @pytest.mark.asyncio
    async def test_failure(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(False, "timeout")):
            assert await remote.restart() is False


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(True, '{"ok": true}')):
            assert await remote.pause() is True

    @pytest.mark.asyncio
    async def test_resume(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(True, '{"ok": true}')):
            assert await remote.resume() is True
