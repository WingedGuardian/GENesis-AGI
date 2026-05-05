"""IPC adapters for communicating with external programs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Protocol

import httpx

from genesis.modules.external.config import IPCConfig

logger = logging.getLogger(__name__)


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
        timeout_s = data.get("timeout_s", self._timeout)

        # Build remote command: cd to working dir, run claude -p
        parts = []
        if self._remote_working_dir:
            parts.append(f"cd {self._remote_working_dir} &&")
        parts.append(
            f"{self._remote_claude_path} -p"
            f" --model {model}"
            f" --output-format json"
            f" --effort {effort}"
            f" --max-turns 25"
            f" --dangerously-skip-permissions"
        )
        remote_cmd = " ".join(parts)
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
        """Run a raw shell command on the remote machine."""
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
        result = await self._send_shell(f"{self._remote_claude_path} --version")
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
