"""GuardianRemote — guardrailed SSH interface to host VM Guardian.

# GROUNDWORK(guardian-bidirectional): Container↔Host monitoring link

CONSTRAINT: This class ONLY calls guardian-gateway.sh operations via
command-restricted SSH. The SSH key on the host is locked to the gateway
script via an authorized_keys ``command=`` directive — even if this code
tried to run arbitrary commands, the host would reject them. OpenSSH
enforces this restriction, not our code.

Six operations: restart-timer, pause, resume, status, version, update.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class GuardianRemote:
    """SSH interface to the Guardian gateway on the host VM.

    All connection parameters are read from config — no hardcoded defaults
    for host_ip or host_user. If config is missing, the caller should not
    instantiate this class.
    """

    def __init__(
        self,
        host_ip: str,
        host_user: str,
        key_path: str = "~/.ssh/genesis_guardian_ed25519",
        timeout: float = 10.0,
    ) -> None:
        if not host_ip or not host_user:
            raise ValueError("host_ip and host_user are required")
        self._host_ip = host_ip
        self._host_user = host_user
        self._key_path = str(Path(key_path).expanduser())
        self._timeout = timeout

    async def _ssh_command(self, command: str) -> tuple[bool, str]:
        """Run a command via SSH to the guardian gateway.

        Returns (success, raw_stdout). The gateway returns JSON for all
        operations; failures return JSON on stderr with exit code 1.
        """
        cmd = [
            "ssh",
            "-i", self._key_path,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={int(self._timeout)}",
            "-o", "BatchMode=yes",
            f"{self._host_user}@{self._host_ip}",
            command,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout + 5,
            )
            output = stdout.decode().strip() or stderr.decode().strip()
            return proc.returncode == 0, output
        except TimeoutError:
            logger.warning(
                "SSH to %s@%s timed out after %.0fs",
                self._host_user, self._host_ip, self._timeout,
            )
            return False, "timeout"
        except OSError as exc:
            logger.warning("SSH command failed: %s", exc)
            return False, str(exc)

    async def status(self) -> dict:
        """Query Guardian state from the host."""
        ok, output = await self._ssh_command("status")
        if ok:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logger.warning("Guardian status returned non-JSON: %s", output[:200])
                return {"current_state": "unknown", "raw": output[:200]}
        return {"current_state": "unreachable", "error": output[:200]}

    async def restart(self) -> bool:
        """Restart the Guardian timer on the host. Returns True on success."""
        ok, output = await self._ssh_command("restart-timer")
        if ok:
            logger.info("Guardian restart-timer succeeded: %s", output[:200])
        else:
            logger.error("Guardian restart-timer failed: %s", output[:200])
        return ok

    async def pause(self) -> bool:
        """Pause Guardian checks on the host."""
        ok, output = await self._ssh_command("pause")
        if not ok:
            logger.error("Guardian pause failed: %s", output[:200])
        return ok

    async def resume(self) -> bool:
        """Resume Guardian checks on the host."""
        ok, output = await self._ssh_command("resume")
        if not ok:
            logger.error("Guardian resume failed: %s", output[:200])
        return ok

    async def version(self) -> dict:
        """Query Guardian version info (CC, Node, code) from the host."""
        ok, output = await self._ssh_command("version")
        if ok:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logger.warning("Guardian version returned non-JSON: %s", output[:200])
                return {"cc_version": "unknown", "raw": output[:200]}
        return {"cc_version": "unreachable", "error": output[:200]}

    async def update(self) -> dict:
        """Pull latest code on the host. Returns old/new commit hashes."""
        ok, output = await self._ssh_command("update")
        if ok:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logger.warning("Guardian update returned non-JSON: %s", output[:200])
                return {"ok": False, "raw": output[:200]}
        logger.error("Guardian update failed: %s", output[:200])
        return {"ok": False, "error": output[:200]}
