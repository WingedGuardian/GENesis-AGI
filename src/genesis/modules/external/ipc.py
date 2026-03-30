"""IPC adapters for communicating with external programs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Protocol

import httpx

from genesis.modules.external.config import IPCConfig

logger = logging.getLogger(__name__)


class IPCAdapter(Protocol):
    """Protocol for IPC communication with external programs."""

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
            return resp.json()
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
            return json.loads(line.decode().strip())
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


def create_ipc_adapter(config: IPCConfig) -> HttpIPCAdapter | StdioIPCAdapter:
    """Factory: create the right IPC adapter from config."""
    if config.method == "http":
        return HttpIPCAdapter(config)
    if config.method == "stdio":
        return StdioIPCAdapter(config)
    raise ValueError(f"Unknown IPC method: {config.method}")
