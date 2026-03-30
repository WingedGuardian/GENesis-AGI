"""Browser types for profile management."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BrowserLayer(StrEnum):
    """Three layers of browser interaction, from lightweight to full control."""

    FETCH = "fetch"
    """Layer 1: API-like web fetch. Read-only, no authentication, no interaction.
    Uses genesis.web (SearXNG + Brave) or WebFetch tool."""

    MANAGED = "managed"
    """Layer 2: Managed browser with persistent profile. Agent's own logins.
    Uses Playwright MCP with --user-data-dir for cross-session persistence."""

    RELAY = "relay"
    """Layer 3: Browser relay/extension bridge. Connects to user's running
    Chrome via Playwright MCP Bridge extension. Uses user's logged-in sessions."""


@dataclass(frozen=True)
class BrowserSession:
    """Represents a logged-in session in the persistent browser profile."""

    domain: str
    cookie_count: int = 0
    has_local_storage: bool = False
    last_accessed: str = ""


@dataclass
class ProfileInfo:
    """Summary of the persistent browser profile state."""

    profile_path: str
    exists: bool = False
    size_mb: float = 0.0
    sessions: list[BrowserSession] = field(default_factory=list)
