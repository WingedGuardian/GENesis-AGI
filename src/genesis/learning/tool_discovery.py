"""Tool discovery — static capability map and content-type routing.

Populates the tool_registry table with known tool capabilities and provides
content-type-based routing to select the best tool for a given input type.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import capability_gaps, tool_registry

# GROUNDWORK(provider-migration): Static maps superseded by ProviderRegistry.
# Use genesis.providers.ProviderRegistry.route_by_content_type() for new code.
# Kept for backward compatibility with callers that haven't migrated yet.
CONTENT_TYPE_ROUTING: dict[str, list[str]] = {
    "youtube_video": ["gemini"],
    "web_page": ["firecrawl", "playwright", "requests"],
    "pdf": ["pdf_extractor", "gemini"],
    "image": ["gemini", "claude"],
    "code": ["claude", "deepseek"],
    "structured_data": ["claude", "deepseek", "mistral"],
    "csv": ["csv_connector"],
    "spreadsheet": ["google_sheets_connector", "csv_connector"],
}

KNOWN_TOOLS: list[dict[str, str | None]] = [
    {
        "name": "firecrawl",
        "category": "web",
        "description": "Web scraping and content extraction",
        "tool_type": "mcp",
        "provider": "firecrawl",
        "cost_tier": "cheap",
    },
    {
        "name": "playwright",
        "category": "web",
        "description": "Browser automation for dynamic pages",
        "tool_type": "mcp",
        "provider": "playwright",
        "cost_tier": "free",
    },
    {
        "name": "gemini",
        "category": "analysis",
        "description": "Multimodal analysis (video, image, text)",
        "tool_type": "builtin",
        "provider": "google",
        "cost_tier": "moderate",
    },
    {
        "name": "claude",
        "category": "analysis",
        "description": "Advanced reasoning and code analysis",
        "tool_type": "builtin",
        "provider": "anthropic",
        "cost_tier": "moderate",
    },
    {
        "name": "deepseek",
        "category": "analysis",
        "description": "Code-focused analysis and generation",
        "tool_type": "builtin",
        "provider": "deepseek",
        "cost_tier": "cheap",
    },
    {
        "name": "mistral",
        "category": "analysis",
        "description": "Structured data and multilingual analysis",
        "tool_type": "builtin",
        "provider": "mistral",
        "cost_tier": "cheap",
    },
    {
        "name": "pdf_extractor",
        "category": "extraction",
        "description": "PDF text and table extraction",
        "tool_type": "builtin",
        "provider": None,
        "cost_tier": "free",
    },
    {
        "name": "requests",
        "category": "web",
        "description": "Simple HTTP requests for static pages",
        "tool_type": "builtin",
        "provider": None,
        "cost_tier": "free",
    },
    {
        "name": "csv_connector",
        "category": "data_access",
        "description": "Read/write local CSV files as structured data",
        "tool_type": "provider",
        "provider": "genesis",
        "cost_tier": "free",
    },
    {
        "name": "google_sheets_connector",
        "category": "data_access",
        "description": "Read/write Google Sheets as structured data",
        "tool_type": "provider",
        "provider": "google",
        "cost_tier": "free",
    },
]


async def populate_tool_registry(db: aiosqlite.Connection) -> int:
    """Populate tool_registry table with known tool capabilities.

    Returns count of tools registered.
    """
    now = datetime.now(UTC).isoformat()
    count = 0
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
    return count


def route_by_content_type(content_type: str) -> list[str]:
    """Return ordered list of tools that can handle this content type.

    .. deprecated::
        Use ``ProviderRegistry.route_by_content_type()`` instead.
        This static map is kept for backward compatibility.
    """
    # GROUNDWORK(provider-migration): Migrate callers to ProviderRegistry
    return CONTENT_TYPE_ROUTING.get(content_type, [])


async def record_capability_gap(
    db: aiosqlite.Connection,
    content_type: str,
    attempted_tools: list[str],
) -> str:
    """Record when no tool can handle a content type."""
    now = datetime.now(UTC).isoformat()
    description = (
        f"No tool handles content_type={content_type}. "
        f"Tried: {', '.join(attempted_tools)}"
    )
    gap_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"content_type:{content_type}"))
    await capability_gaps.upsert(
        db,
        id=gap_id,
        description=description,
        gap_type="capability_gap",
        first_seen=now,
        last_seen=now,
    )
    return gap_id
