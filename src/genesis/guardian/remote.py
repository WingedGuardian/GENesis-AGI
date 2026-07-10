"""GuardianRemote — CONTAINER-SIDE. Guardrailed SSH interface to host VM Guardian.

# GROUNDWORK(guardian-bidirectional): Container↔Host monitoring link

CONSTRAINT: This class ONLY calls guardian-gateway.sh operations via
command-restricted SSH. The SSH key on the host is locked to the gateway
script via an authorized_keys ``command=`` directive — even if this code
tried to run arbitrary commands, the host would reject them. OpenSSH
enforces this restriction, not our code.

Gateway allowlist: restart-timer, pause, resume, status, reset-state, version,
update, sync-gateway, redeploy, update-cc, update-node, test-approval,
disk-status, reharden-key, ping, provision-status, provision-grow-disk,
provision-grow-memory, storage-expand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Client-side validation (defense in depth — the gateway re-validates too).
_DISK_RE = re.compile(r"(scsi|virtio|sata)[0-9]{1,2}")

# Per-verb SSH wait timeouts. The grow/expand verbs run long host-side (a disk
# grow + LVM absorb), so the client waits slightly LONGER than the gateway's own
# `timeout N` so the gateway's timeout fires first with a clean JSON error
# rather than the client killing the connection mid-op.
_STATUS_TIMEOUT = 70.0       # gateway: timeout 60
# version now also runs `claude auth status` (node startup + a network round-trip,
# bounded host-side by `timeout 15`) on top of claude/node/git version reads, so
# the 10s instance default is too tight — a slow verb would return `unreachable`
# and silently skip EVERY drift/authkey/cc-auth reconciler that reuses this call.
_VERSION_TIMEOUT = 30.0      # gateway version verb: auth-status probe + version reads
_GROW_DISK_TIMEOUT = 660.0   # gateway: timeout 600 (PVE resize + storage-expand)
_GROW_MEM_TIMEOUT = 180.0    # gateway: timeout 120 (config PUT + pending check)
_EXPAND_TIMEOUT = 660.0      # gateway: timeout 600 (pvresize + autoextend profile)


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

    async def _ssh_command(
        self, command: str, timeout: float | None = None,
    ) -> tuple[bool, str]:
        """Run a command via SSH to the guardian gateway.

        Returns (success, raw_stdout). The gateway returns JSON for all
        operations; failures return JSON on stderr with exit code 1.

        ``timeout`` overrides the instance default for verbs that legitimately
        run long (a disk grow + storage-expand can take minutes host-side).
        The ConnectTimeout stays pinned to the short instance default — only
        the post-connect wait is extended.
        """
        wait_timeout = timeout if timeout is not None else self._timeout
        cmd = [
            "ssh",
            "-i", self._key_path,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={int(self._timeout)}",
            "-o", "BatchMode=yes",
        ]
        # Pin the address family for a v4 literal or a hostname so a dual-stack
        # resolution can't flip which source address the host's sshd sees — the
        # address the guardian key's from= is matched against. A v6 literal
        # (contains ':') is left alone: forcing inet would break the connection.
        if ":" not in self._host_ip:
            cmd += ["-o", "AddressFamily=inet"]
        cmd += [
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
                proc.communicate(), timeout=wait_timeout + 5,
            )
            output = stdout.decode().strip() or stderr.decode().strip()
            return proc.returncode == 0, output
        except TimeoutError:
            # Kill the orphaned SSH process to prevent accumulation
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            logger.warning(
                "SSH to %s@%s timed out after %.0fs",
                self._host_user, self._host_ip, wait_timeout,
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
        ok, output = await self._ssh_command("version", timeout=_VERSION_TIMEOUT)
        if ok:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logger.warning("Guardian version returned non-JSON: %s", output[:200])
                return {"cc_version": "unknown", "raw": output[:200]}
        return {"cc_version": "unreachable", "error": output[:200]}

    async def reset_state(self) -> dict:
        """Reset Guardian state to HEALTHY when stuck in confirmed_dead.

        The gateway only allows reset from stuck states (confirmed_dead,
        recovering, recovered). Returns the previous state on success.
        """
        ok, output = await self._ssh_command("reset-state")
        if ok:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logger.warning("Guardian reset-state returned non-JSON: %s", output[:200])
                return {"ok": True, "raw": output[:200]}
        logger.error("Guardian reset-state failed: %s", output[:200])
        return {"ok": False, "error": output[:200]}

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

    async def sync_gateway(self) -> dict:
        """Redeploy the gateway script from the install dir, without a git pull.

        Recovery lever for a stale/frozen deployed gateway when the `update`
        self-update path is unavailable. Returns old/new sha on success.
        """
        ok, output = await self._ssh_command("sync-gateway")
        if ok:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logger.warning("Guardian sync-gateway returned non-JSON: %s", output[:200])
                return {"ok": True, "raw": output[:200]}
        logger.error("Guardian sync-gateway failed: %s", output[:200])
        return {"ok": False, "error": output[:200]}

    async def reharden_key(self) -> dict:
        """Re-harden the guardian authorized_keys line on the host.

        The verb rewrites the key line to the canonical hardened options with a
        self-proving ``from=`` and arms a 120s dead-man's-switch that restores
        the previous file unless a fresh connection confirms the rewrite works.
        When the line actually changed we make that confirming connection HERE:
        a second call over a brand-new SSH process. Its success proves the
        rewritten key still authenticates AND cancels the pending restore
        (a no-op idempotent second reharden). If the confirm fails, we do NOT
        claim success — the host's switch will restore the known-good file, and
        we surface ``restore_pending`` so the next reconciler tick (5 min, well
        past the 120s window) observes the outcome rather than a wedge.

        Unlike sync-gateway, a non-JSON response is treated as failure: we must
        not guess whether the key file changed.
        """
        ok, output = await self._ssh_command("reharden-key")
        if not ok:
            logger.error("Guardian reharden-key failed: %s", output[:200])
            return {"ok": False, "error": output[:200]}
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            logger.warning("Guardian reharden-key returned non-JSON: %s", output[:200])
            return {"ok": False, "error": "non-JSON response", "raw": output[:200]}

        if not result.get("changed"):
            return result  # idempotent no-op — nothing to confirm

        # The line changed: prove it works with a fresh connection. This second
        # call is idempotent (changed:false) and cancels the restore switch.
        confirm_ok, confirm_out = await self._ssh_command("reharden-key")
        if confirm_ok:
            result["confirmed"] = True
            logger.info("Guardian reharden-key confirmed via fresh connection")
        else:
            result["ok"] = False
            result["confirmed"] = False
            result["restore_pending"] = True
            logger.error(
                "Guardian reharden-key confirm FAILED (%s) — host dead-man's-"
                "switch will restore the previous key", confirm_out[:200],
            )
        return result

    # ── provisioning executors (EXECUTE-ONLY — approval is the caller's job) ──
    # These invoke the host execute-core (fresh re-check + one attempt +
    # ledger) with NO Telegram gate. The container obtains APPROVE via its own
    # bot BEFORE calling these (see provisioning.container.coordinate_grow_*).

    @staticmethod
    def _as_json(ok: bool, output: str, action: str) -> dict:
        if not ok:
            return {"ok": False, "action": action, "error": output[:300]}
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            logger.warning("Guardian %s returned non-JSON: %s", action, output[:200])
            return {"ok": False, "action": action,
                    "error": "non-JSON response", "raw": output[:300]}

    async def provision_status(self) -> dict:
        """Read-only host capacity (audit token). No approval, no mutation."""
        ok, out = await self._ssh_command("provision-status", timeout=_STATUS_TIMEOUT)
        return self._as_json(ok, out, "provision-status")

    async def request_grow_disk(self, disk: str, add_gib: int) -> dict:
        """EXECUTE a pre-approved VM disk grow + absorb into the thin pool."""
        if not _DISK_RE.fullmatch(disk):
            return {"ok": False, "action": "provision-grow-disk",
                    "error": f"invalid disk {disk!r}"}
        if not 1 <= add_gib <= 999:
            return {"ok": False, "action": "provision-grow-disk",
                    "error": f"invalid GiB {add_gib} (1-999)"}
        ok, out = await self._ssh_command(
            f"provision-grow-disk {disk} {add_gib}", timeout=_GROW_DISK_TIMEOUT,
        )
        return self._as_json(ok, out, "provision-grow-disk")

    async def request_grow_memory(self, new_mib: int) -> dict:
        """EXECUTE a pre-approved VM memory grow (requires a later VM reboot)."""
        if not 100 <= new_mib <= 999999:
            return {"ok": False, "action": "provision-grow-memory",
                    "error": f"invalid MiB {new_mib} (100-999999)"}
        ok, out = await self._ssh_command(
            f"provision-grow-memory {new_mib}", timeout=_GROW_MEM_TIMEOUT,
        )
        return self._as_json(ok, out, "provision-grow-memory")

    async def storage_expand(self) -> dict:
        """Absorb an already-grown virtual disk into the LVM-thin pool."""
        ok, out = await self._ssh_command("storage-expand", timeout=_EXPAND_TIMEOUT)
        return self._as_json(ok, out, "storage-expand")
