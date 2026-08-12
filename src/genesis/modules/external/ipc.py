"""IPC adapters for communicating with external programs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Protocol

import httpx

from genesis.cc.types import CCModel, EffortLevel, model_supports_effort
from genesis.modules.external.config import IPCConfig

logger = logging.getLogger(__name__)

# Absolute adapter-level ceiling on a per-call ``--max-turns`` override, independent
# of any one caller's config validation. `send()` params can flow from the generic
# `module_call` path (adapter.py) too, so a mistaken/autonomous caller must not be
# able to request an unbounded turn count that keeps a costly remote agent running.
# Real flows need far less (the career-outreach gated flow measured ~42 turns; its
# config caps at 200); 500 is generous headroom that constrains nothing legitimate.
_MAX_TURNS_CEILING = 500

# Same threat, same defense for the sibling per-call ``timeout_s`` override: an
# unbounded timeout from the generic ``module_call`` passthrough keeps the remote
# ``claude -p`` (and the local SSH subprocess) alive arbitrarily long. The config
# layer caps the one real caller at 1800; 3600 is the adapter backstop for any
# caller that bypasses config validation. (Parity with _MAX_TURNS_CEILING.)
_MAX_TIMEOUT_CEILING = 3600


class IPCAdapter(Protocol):
    """Protocol for IPC communication with external programs."""

    @property
    def needs_start(self) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, path: str, data: dict | None = None, method: str = "GET") -> dict: ...
    async def health_check(self, endpoint: str, expected_status: int) -> bool: ...


class HttpIPCAdapter:
    """Communicates with external programs via HTTP REST API."""

    def __init__(self, config: IPCConfig) -> None:
        if not config.url:
            raise ValueError("HTTP IPC requires a url in config")
        self._url = config.url.rstrip("/")
        self._timeout = config.timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def needs_start(self) -> bool:
        return self._client is None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._url,
            timeout=self._timeout,
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send(self, path: str, data: dict | None = None, method: str = "GET") -> dict:
        if not self._client:
            raise RuntimeError("HTTP IPC not started")
        try:
            if method.upper() == "GET":
                resp = await self._client.get(path, params=data)
            elif method.upper() == "POST":
                resp = await self._client.post(path, json=data)
            elif method.upper() == "PUT":
                resp = await self._client.put(path, json=data)
            elif method.upper() == "DELETE":
                resp = await self._client.delete(path, params=data)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            resp.raise_for_status()
            result = resp.json()
            if not isinstance(result, dict):
                return {"data": result}
            return result
        except httpx.HTTPStatusError as exc:
            logger.warning("HTTP %s %s returned %d", method, path, exc.response.status_code)
            return {"error": str(exc), "status_code": exc.response.status_code}
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("HTTP %s %s failed: %s", method, path, exc)
            return {"error": str(exc)}

    async def health_check(self, endpoint: str, expected_status: int) -> bool:
        if not self._client:
            return False
        try:
            resp = await self._client.get(endpoint, timeout=10)
            return resp.status_code == expected_status
        except (httpx.ConnectError, httpx.TimeoutException):
            return False


class StdioIPCAdapter:
    """Communicates with external programs via JSON lines over stdin/stdout."""

    def __init__(self, config: IPCConfig) -> None:
        if not config.command:
            raise ValueError("stdio IPC requires a command in config")
        self._command = config.command
        self._cwd = config.working_dir
        # Merge with inherited environment so subprocess gets PATH, HOME, etc.
        self._env = {**os.environ, **config.env} if config.env else None
        self._timeout = config.timeout
        self._process: asyncio.subprocess.Process | None = None

    @property
    def needs_start(self) -> bool:
        return self._process is None or self._process.returncode is not None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )

    async def stop(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                self._process.kill()
            self._process = None

    async def send(self, path: str, data: dict | None = None, method: str = "GET") -> dict:
        if not self._process or self._process.returncode is not None:
            raise RuntimeError("stdio process not running")
        request = json.dumps({"path": path, "method": method, "data": data or {}})
        self._process.stdin.write((request + "\n").encode())
        await self._process.stdin.drain()
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=self._timeout,
            )
            if not line:
                return {"error": "process closed stdout"}
            result = json.loads(line.decode().strip())
            if not isinstance(result, dict):
                return {"data": result}
            return result
        except TimeoutError:
            return {"error": "timeout waiting for response"}
        except json.JSONDecodeError as exc:
            return {"error": f"invalid JSON response: {exc}"}

    async def health_check(self, endpoint: str, expected_status: int) -> bool:
        if not self._process or self._process.returncode is not None:
            return False
        try:
            result = await self.send(endpoint, method="GET")
            return "error" not in result
        except Exception:
            return False


class SshIPCAdapter:
    """Communicates with a remote Claude Code instance via SSH.

    Two operation modes based on the method passed to send():
    - CC: Runs ``claude -p`` on the remote machine with the prompt from
      data["prompt"]. Returns structured JSON output.
    - SHELL: Runs a raw command on the remote machine. Returns stdout + exit code.

    Uses OpenSSH CLI (not paramiko) following the Guardian SSH pattern.
    """

    def __init__(self, config: IPCConfig) -> None:
        if not config.ssh_host:
            raise ValueError("SSH IPC requires ssh_host in config")
        self._ssh_host = config.ssh_host
        self._ssh_key = str(Path(config.ssh_key).expanduser()) if config.ssh_key else None
        self._ssh_connect_timeout = config.ssh_connect_timeout
        self._remote_working_dir = config.remote_working_dir
        self._remote_claude_path = config.remote_claude_path
        self._timeout = config.timeout

    @property
    def needs_start(self) -> bool:
        return False  # connectionless — each send() opens its own SSH session

    async def start(self) -> None:
        pass  # no persistent connection

    async def stop(self) -> None:
        pass  # nothing to tear down

    def _build_ssh_args(self, remote_command: str) -> list[str]:
        """Build the SSH command array following the Guardian pattern."""
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={self._ssh_connect_timeout}",
            "-o", "BatchMode=yes",
        ]
        if self._ssh_key:
            cmd.extend(["-i", self._ssh_key])
        cmd.extend([self._ssh_host, remote_command])
        return cmd

    def _build_remote_command(self, model: str, effort: str, max_turns: int | None = None) -> str:
        """Build the remote ``cd … && claude -p …`` string with every value quoted.

        The command runs in the REMOTE shell (it relies on ``cd`` and ``&&``), so
        caller-supplied ``model``/``effort`` MUST be shell-quoted or they could
        inject arbitrary commands on the remote host (WS-1 / R2-001). ``shlex.quote``
        leaves valid enum values (e.g. ``sonnet``/``high``) untouched and wraps
        anything containing shell metacharacters as a single safe token.
        """
        model = str(model)
        effort = str(effort)
        valid_models = {m.value for m in CCModel}
        valid_efforts = {m.value for m in EffortLevel}
        if model not in valid_models:
            logger.warning(
                "SSH CC dispatch: unrecognized model %r (quoted and passed through)", model
            )
        if effort not in valid_efforts:
            logger.warning(
                "SSH CC dispatch: unrecognized effort %r (quoted and passed through)", effort
            )

        # Haiku does not use an effort setting — omit --effort for it (the CLI
        # tolerates the flag but it's a no-op). Unknown models fall through to
        # including --effort (best-effort passthrough, warned above).
        include_effort = (
            model not in valid_models or model_supports_effort(CCModel(model))
        )
        effort_seg = f" --effort {shlex.quote(effort)}" if include_effort else ""

        # Per-call turn budget. Default 25 keeps every existing caller unchanged; a
        # caller driving a long agentic flow (e.g. the career-outreach auto-run)
        # passes a larger value. A non-positive / non-int / bool value falls back to 25.
        turns = (
            max_turns
            if isinstance(max_turns, int) and not isinstance(max_turns, bool) and max_turns > 0
            else 25
        )
        if turns > _MAX_TURNS_CEILING:
            logger.warning(
                "SSH CC dispatch: max_turns=%d exceeds the adapter ceiling %d — clamping",
                turns, _MAX_TURNS_CEILING,
            )
            turns = _MAX_TURNS_CEILING

        parts: list[str] = []
        if self._remote_working_dir:
            parts.append(f"cd {shlex.quote(self._remote_working_dir)} &&")
        parts.append(
            f"{shlex.quote(self._remote_claude_path)} -p"
            f" --model {shlex.quote(model)}"
            f" --output-format json"
            f"{effort_seg}"
            f" --max-turns {turns}"
            f" --dangerously-skip-permissions"
        )
        return " ".join(parts)

    async def send(self, path: str, data: dict | None = None, method: str = "GET") -> dict:
        method_upper = method.upper()
        if method_upper == "CC":
            return await self._send_cc(data or {})
        if method_upper == "SHELL":
            return await self._send_shell(path)
        return {"error": f"SSH adapter does not support method '{method}'. Use CC or SHELL."}

    async def _send_cc(self, data: dict) -> dict:
        """Dispatch a prompt to remote Claude Code and return structured output."""
        prompt = data.get("prompt", "")
        if not prompt:
            return {"error": "CC dispatch requires a 'prompt' in params"}

        model = data.get("model", "sonnet")
        effort = data.get("effort", "high")
        max_turns = data.get("max_turns")

        # Clamp the per-call timeout to an adapter-level ceiling (parity with the
        # max_turns ceiling): a caller bypassing config validation must not keep the
        # remote agent alive without bound. A non-positive / non-numeric / bool value
        # falls back to the module default (itself clamped).
        raw_timeout = data.get("timeout_s", self._timeout)
        if (
            isinstance(raw_timeout, (int, float))
            and not isinstance(raw_timeout, bool)
            and raw_timeout > 0
        ):
            timeout_s = min(raw_timeout, _MAX_TIMEOUT_CEILING)
            if raw_timeout > _MAX_TIMEOUT_CEILING:
                logger.warning(
                    "SSH CC dispatch: timeout_s=%s exceeds the adapter ceiling %d — clamping",
                    raw_timeout, _MAX_TIMEOUT_CEILING,
                )
        else:
            timeout_s = min(self._timeout, _MAX_TIMEOUT_CEILING)

        # Build the remote command with every interpolated value shell-quoted
        # (it is re-parsed by the remote shell). See _build_remote_command.
        remote_cmd = self._build_remote_command(model, effort, max_turns)
        ssh_args = self._build_ssh_args(remote_cmd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()),
                timeout=timeout_s,
            )
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return {"error": f"SSH CC dispatch timed out after {timeout_s}s"}
        except OSError as exc:
            return {"error": f"SSH connection failed: {exc}"}

        stdout = stdout_bytes.decode()
        stderr = stderr_bytes.decode()

        if proc.returncode != 0:
            return {
                "error": f"Remote claude exited {proc.returncode}",
                "stderr": stderr[:2000],
                "stdout": stdout[:2000],
            }

        return self._parse_cc_output(stdout)

    async def _send_shell(self, command: str) -> dict:
        """Run a raw shell command on the remote machine.

        ``command`` is forwarded verbatim to the remote shell — this is the raw
        SHELL escape hatch by design. Callers are responsible for shell-quoting
        any interpolated/untrusted values before passing them here (see
        ``_build_remote_command`` and ``health_check`` for the quoting pattern).
        """
        ssh_args = self._build_ssh_args(command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=30,
            )
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return {"error": "SSH shell command timed out"}
        except OSError as exc:
            return {"error": f"SSH connection failed: {exc}"}

        return {
            "output": stdout_bytes.decode().strip(),
            "stderr": stderr_bytes.decode().strip() or None,
            "exit_code": proc.returncode,
        }

    @staticmethod
    def _parse_cc_output(raw: str) -> dict:
        """Parse claude -p JSON output into a structured dict.

        Scans lines in reverse for {"type": "result", ...} — same approach
        as CCInvoker._parse_output().
        """
        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and parsed.get("type") == "result":
                    usage = parsed.get("usage", {})
                    return {
                        "text": parsed.get("result", ""),
                        "session_id": parsed.get("session_id", ""),
                        "cost_usd": parsed.get("total_cost_usd", 0.0),
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "duration_ms": parsed.get("duration_ms", 0),
                        "is_error": parsed.get("is_error", False),
                        "model_used": next(iter(parsed.get("modelUsage", {})), ""),
                    }
            except json.JSONDecodeError:
                continue
        # Fallback: no structured output found
        return {"text": raw.strip(), "is_error": False, "parse_fallback": True}

    async def health_check(self, endpoint: str, expected_status: int) -> bool:
        """Check remote connectivity by running a simple SSH command."""
        result = await self._send_shell(
            f"{shlex.quote(self._remote_claude_path)} --version"
        )
        return result.get("exit_code") == 0


def create_ipc_adapter(
    config: IPCConfig,
) -> HttpIPCAdapter | StdioIPCAdapter | SshIPCAdapter:
    """Factory: create the right IPC adapter from config."""
    if config.method == "http":
        return HttpIPCAdapter(config)
    if config.method == "stdio":
        return StdioIPCAdapter(config)
    if config.method == "ssh":
        return SshIPCAdapter(config)
    raise ValueError(f"Unknown IPC method: {config.method}")
