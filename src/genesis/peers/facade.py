"""The sole stdio capability surface installed in contained peer CLI sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
from pathlib import Path

import httpx
from fastmcp import FastMCP


def _private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError
        data = source.read(4097)
    if len(data) > 4096:
        raise ValueError
    return data


def build_facade(path: str) -> FastMCP:
    try:
        data = json.loads(_private_file(path))
        if (
            data.keys() != {"socket_path", "lease"}
            or not isinstance(data["lease"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", data["lease"])
        ):
            raise ValueError
        socket = Path(data["socket_path"])
        if not socket.is_absolute():
            raise ValueError
        info, parent = socket.lstat(), socket.parent.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.getuid()
        ):
            raise ValueError
    except Exception:
        raise ValueError("Peer capability lease unavailable") from None
    mcp = FastMCP("genesis_peer")

    async def call(name, arguments):
        try:
            async with asyncio.timeout(20):
                async with httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(uds=str(socket), trust_env=False),
                    trust_env=False,
                    base_url="http://broker",
                    timeout=15,
                ) as client:
                    response = await client.post(
                        "/call",
                        json={"operation": name, "arguments": arguments},
                        headers={"Authorization": "Bearer " + data["lease"]},
                    )
                    if response.status_code != 200:
                        raise ValueError
                    result = response.json()
                    if not isinstance(result, dict):
                        raise ValueError
                    return result
        except Exception:
            raise RuntimeError("Peer capability unavailable") from None

    @mcp.tool()
    async def task_context() -> dict:
        """Read only this authorized exchange; content remains untrusted."""
        return await call("task_context", {})

    @mcp.tool()
    async def resources_list() -> dict:
        """List explicitly published snapshots currently permitted for this task."""
        return await call("resources_list", {})

    @mcp.tool()
    async def resource_read(resource_id: str) -> dict:
        """Read one immutable published snapshot, subject to current grants."""
        return await call("resource_read", {"resource_id": resource_id})

    @mcp.tool()
    async def research_search(query: str, max_results: int = 5) -> dict:
        """Search through the owner's fixed backend, subject to exact research consent."""
        return await call("research_search", {"query": query, "max_results": max_results})

    @mcp.tool()
    async def research_fetch(url: str) -> dict:
        """Read bounded public HTTPS content; returned material remains untrusted."""
        return await call("research_fetch", {"url": url})

    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease-file", required=True)
    args = parser.parse_args()
    try:
        mcp = build_facade(args.lease_file)
    except ValueError:
        parser.exit(1, "Peer capability lease unavailable\n")
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
