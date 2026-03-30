"""Bootstrap tool registry at startup with known + CC tools."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import tool_registry
from genesis.learning.tool_discovery import KNOWN_TOOLS

logger = logging.getLogger(__name__)

CC_TOOLS: list[dict[str, str | None]] = [
    {"name": "Read", "category": "file_ops", "description": "Read files from filesystem", "tool_type": "builtin", "provider": "cc"},
    {"name": "Write", "category": "file_ops", "description": "Write/create files", "tool_type": "builtin", "provider": "cc"},
    {"name": "Edit", "category": "file_ops", "description": "Edit existing files", "tool_type": "builtin", "provider": "cc"},
    {"name": "Bash", "category": "system", "description": "Execute shell commands", "tool_type": "builtin", "provider": "cc"},
    {"name": "Grep", "category": "search", "description": "Search file contents via regex", "tool_type": "builtin", "provider": "cc"},
    {"name": "Glob", "category": "search", "description": "Find files by pattern", "tool_type": "builtin", "provider": "cc"},
    {"name": "WebFetch", "category": "web", "description": "Fetch web page content", "tool_type": "builtin", "provider": "cc"},
    {"name": "WebSearch", "category": "web", "description": "Search the web", "tool_type": "builtin", "provider": "cc"},
    {"name": "Agent", "category": "orchestration", "description": "Launch subagent for complex tasks", "tool_type": "builtin", "provider": "cc"},
]


async def bootstrap_tool_registry(db: aiosqlite.Connection) -> int:
    """Populate tool_registry with known + CC tools. Idempotent via upsert.

    Returns total count of tools registered.
    """
    now = datetime.now(UTC).isoformat()
    count = 0

    # Register known tools (from tool_discovery)
    for tool in KNOWN_TOOLS:
        await tool_registry.upsert(
            db,
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, tool["name"])),  # type: ignore[arg-type]
            name=tool["name"],  # type: ignore[arg-type]
            category=tool["category"],  # type: ignore[arg-type]
            description=tool["description"],  # type: ignore[arg-type]
            tool_type=tool["tool_type"],  # type: ignore[arg-type]
            provider=tool.get("provider"),  # type: ignore[arg-type]
            cost_tier=tool.get("cost_tier"),  # type: ignore[arg-type]
            created_at=now,
        )
        count += 1

    # Register CC (Claude Code) built-in tools
    for tool in CC_TOOLS:
        await tool_registry.upsert(
            db,
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"cc_{tool['name']}")),
            name=tool["name"],  # type: ignore[arg-type]
            category=tool["category"],  # type: ignore[arg-type]
            description=tool["description"],  # type: ignore[arg-type]
            tool_type=tool["tool_type"],  # type: ignore[arg-type]
            provider=tool.get("provider"),  # type: ignore[arg-type]
            cost_tier="free",
            created_at=now,
        )
        count += 1

    logger.info("Tool registry bootstrapped: %d tools", count)
    return count
