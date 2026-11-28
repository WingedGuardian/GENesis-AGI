"""Bounded target selection before the ordinary launcher gives up its input."""

import re
from pathlib import Path

import anyio
import httpx2
from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT

from genesis.mcp.database_target import (
    TARGET_META,
    TARGET_STATUS,
    database_target,
    require_database_target,
)
from genesis.mcp.plan_bookmark_context import PENDING_VERSION
from genesis.mcp.shared_paths import SharedPathError


async def target_status(client, origin, target):
    """Fetch and qualify this memory contract within the existing SDK budget."""
    with anyio.fail_after(MCP_DEFAULT_TIMEOUT):
        response = await client.get(origin + "/status")
        response.raise_for_status()
        status = response.json()
        if (not isinstance(status, dict) or status.get("service") != "memory"
                or status.get("enabled") is not True
                or type(status.get("transport_schema_version")) is not int
                or status["transport_schema_version"] != 1
                or type(status.get("plan_bookmark_pending_version")) is not int
                or status["plan_bookmark_pending_version"] != PENDING_VERSION):
            raise SharedPathError("Shared memory contract unavailable or incompatible")
        revision = status.get("catalog_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise SharedPathError("Shared memory catalog revision unavailable or invalid")
        require_database_target({TARGET_META: status.get(TARGET_STATUS)}, target)
        return status


async def select_memory_target(settings, header_factory, path):
    """Return a matching logical target; never freeze or override ordinary paths."""
    if settings.http.service != "memory":
        raise ValueError("Database targets require memory")
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path != path.resolve(strict=True):
        return None
    target = database_target(path)
    with anyio.fail_after(MCP_DEFAULT_TIMEOUT):
        async with httpx2.AsyncClient(headers=header_factory(), trust_env=False,
                follow_redirects=False, max_redirects=0, timeout=MCP_DEFAULT_TIMEOUT) as client:
            await target_status(client, settings.http.origin, target)
    return target
