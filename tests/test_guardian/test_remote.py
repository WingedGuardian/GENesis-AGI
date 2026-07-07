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


class TestSyncGateway:
    @pytest.mark.asyncio
    async def test_parses_json(self, remote):
        with patch.object(
            remote, "_ssh_command",
            return_value=(True,
                          '{"ok": true, "action": "sync-gateway", '
                          '"old_sha": "aaa", "new_sha": "bbb"}'),
        ):
            result = await remote.sync_gateway()
        assert result["ok"] is True
        assert result["new_sha"] == "bbb"

    @pytest.mark.asyncio
    async def test_non_json_success(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(True, "not json")):
            result = await remote.sync_gateway()
        assert result["ok"] is True
        assert "raw" in result

    @pytest.mark.asyncio
    async def test_ssh_failure(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(False, "timeout")):
            result = await remote.sync_gateway()
        assert result["ok"] is False
        assert "error" in result


class TestRehardenKey:
    """Verb call + confirm flow: a changed key must be proven by a SECOND
    fresh SSH connection (which doubles as the dead-man's-switch cancel)."""

    @pytest.mark.asyncio
    async def test_unchanged_needs_no_confirm(self, remote):
        with patch.object(
            remote, "_ssh_command",
            return_value=(True, '{"ok": true, "changed": false}'),
        ) as ssh:
            result = await remote.reharden_key()
        assert result["ok"] is True
        assert result["changed"] is False
        ssh.assert_awaited_once()  # idempotent no-op → no second connection

    @pytest.mark.asyncio
    async def test_changed_confirms_with_second_connection(self, remote):
        with patch.object(
            remote, "_ssh_command",
            side_effect=[
                (True, '{"ok": true, "changed": true, "has_from": true}'),
                (True, '{"ok": true, "changed": false}'),
            ],
        ) as ssh:
            result = await remote.reharden_key()
        assert ssh.await_count == 2
        assert result["ok"] is True
        assert result["changed"] is True
        assert result["confirmed"] is True

    @pytest.mark.asyncio
    async def test_confirm_failure_reports_restore_pending(self, remote):
        """Second connection failing means the rewritten line may not work —
        the host's dead-man's-switch will restore it; surface that instead
        of claiming success."""
        with patch.object(
            remote, "_ssh_command",
            side_effect=[
                (True, '{"ok": true, "changed": true, "has_from": true}'),
                (False, "timeout"),
            ],
        ):
            result = await remote.reharden_key()
        assert result["ok"] is False
        assert result["changed"] is True
        assert result["confirmed"] is False
        assert result["restore_pending"] is True

    @pytest.mark.asyncio
    async def test_ssh_failure(self, remote):
        with patch.object(remote, "_ssh_command", return_value=(False, "timeout")):
            result = await remote.reharden_key()
        assert result["ok"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_non_json_response_is_failure(self, remote):
        """Unlike sync-gateway, an unparseable reharden response is NOT a
        success — we can't know whether the key file changed."""
        with patch.object(remote, "_ssh_command", return_value=(True, "not json")):
            result = await remote.reharden_key()
        assert result["ok"] is False


class TestAddressFamilyPin:
    """_ssh_command must pin AddressFamily=inet for v4/hostname targets so a
    dual-stack resolution can't flip the source address sshd sees (which
    from= matches against). A v6-literal host_ip must NOT get the pin."""

    async def _spawn_args(self, remote_obj) -> list[str]:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b'{"ok": true}', b"")
        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc,
        ) as spawn:
            await remote_obj._ssh_command("status")
        return [str(a) for a in spawn.call_args.args]

    @pytest.mark.asyncio
    async def test_v4_literal_gets_pin(self, remote):
        args = await self._spawn_args(remote)
        assert "AddressFamily=inet" in args

    @pytest.mark.asyncio
    async def test_hostname_gets_pin(self):
        r = GuardianRemote(host_ip="guardian.example.internal", host_user="u",
                           key_path="/tmp/test_key")
        args = await self._spawn_args(r)
        assert "AddressFamily=inet" in args

    @pytest.mark.asyncio
    async def test_v6_literal_skips_pin(self):
        r = GuardianRemote(host_ip="fd00::1", host_user="u",
                           key_path="/tmp/test_key")
        args = await self._spawn_args(r)
        assert "AddressFamily=inet" not in args


class TestProvisioningExecutors:
    """EXECUTE-only executors: client-side validation + exact command + per-call
    timeout override + JSON parsing. Approval is NOT done here."""

    @pytest.mark.asyncio
    async def test_provision_status_reads(self, remote):
        with patch.object(remote, "_ssh_command",
                          AsyncMock(return_value=(True, '{"ok": true, "capacity": {}}'))) as m:
            res = await remote.provision_status()
        assert res["ok"] is True
        cmd, kw = m.call_args.args[0], m.call_args.kwargs
        assert cmd == "provision-status"
        assert kw["timeout"] == 70.0

    @pytest.mark.asyncio
    async def test_grow_disk_valid_sends_exact_command(self, remote):
        with patch.object(remote, "_ssh_command",
                          AsyncMock(return_value=(True, '{"ok": true, "stage": "executed"}'))) as m:
            res = await remote.request_grow_disk("scsi1", 32)
        assert res["ok"] is True
        assert m.call_args.args[0] == "provision-grow-disk scsi1 32"
        assert m.call_args.kwargs["timeout"] == 660.0

    @pytest.mark.asyncio
    async def test_grow_disk_bad_disk_never_dials(self, remote):
        with patch.object(remote, "_ssh_command", AsyncMock()) as m:
            res = await remote.request_grow_disk("sda1", 32)
        assert res["ok"] is False and "invalid disk" in res["error"]
        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_grow_disk_bad_gib_never_dials(self, remote):
        with patch.object(remote, "_ssh_command", AsyncMock()) as m:
            assert (await remote.request_grow_disk("scsi1", 0))["ok"] is False
            assert (await remote.request_grow_disk("scsi1", 1000))["ok"] is False
        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_grow_memory_validates_range(self, remote):
        with patch.object(remote, "_ssh_command",
                          AsyncMock(return_value=(True, '{"ok": true}'))) as m:
            assert (await remote.request_grow_memory(99))["ok"] is False   # too small
            m.assert_not_called()
            await remote.request_grow_memory(24576)
            assert m.call_args.args[0] == "provision-grow-memory 24576"
            assert m.call_args.kwargs["timeout"] == 180.0

    @pytest.mark.asyncio
    async def test_storage_expand_command(self, remote):
        with patch.object(remote, "_ssh_command",
                          AsyncMock(return_value=(True, '{"ok": true}'))) as m:
            await remote.storage_expand()
        assert m.call_args.args[0] == "storage-expand"
        assert m.call_args.kwargs["timeout"] == 660.0

    @pytest.mark.asyncio
    async def test_non_json_response_is_error(self, remote):
        with patch.object(remote, "_ssh_command",
                          AsyncMock(return_value=(True, "not json"))):
            res = await remote.provision_status()
        assert res["ok"] is False and res["error"] == "non-JSON response"

    @pytest.mark.asyncio
    async def test_ssh_failure_surfaces_as_error(self, remote):
        with patch.object(remote, "_ssh_command",
                          AsyncMock(return_value=(False, "timeout"))):
            res = await remote.request_grow_disk("scsi1", 32)
        assert res["ok"] is False and "timeout" in res["error"]
