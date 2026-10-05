"""Browser types for profile management."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# pgrep patterns for detecting browser-related processes. Single source of truth
# used by the awareness signal collector, health probe, process reaper, and
# remediation registry. Verified against actual /proc/PID/cmdline entries —
# they match only browser binaries, not the MCP server's Python process.
#
# These are POSIX EXTENDED regexes (pgrep -f), not Python regexes: a `(?:...)`
# group is a pgrep regex error (exit 2), which every consumer reads as "no
# browser processes". tests/test_browser/test_pgrep_patterns.py runs each one
# through real pgrep. The driver pattern covers patchright, which ships the same
# Node driver under its own package directory.
BROWSER_PGREP_PATTERNS: tuple[str, ...] = (
    "camoufox-bin",
    r"ms-playwright.*chrome",
    "(playwright|patchright)/driver/node",
)


class BrowserLayer(StrEnum):
    """Four layers of browser interaction, from lightweight to full control."""

    FETCH = "fetch"
    """Layer 1: API-like web fetch. Read-only, no authentication, no interaction.
    Uses WebFetch, Firecrawl, or genesis.web (SearXNG + Brave)."""

    MANAGED = "managed"
    """Layer 2: Genesis browser tools with persistent profile. Agent's own logins.
    Standard mode (Chromium) or stealth mode (Camoufox anti-detection).
    Profile at ~/.genesis/browser-profile/."""

    RELAY = "relay"
    """Layer 3: On-demand MCP (Chrome DevTools or Playwright) or CDP-over-SSH
    to user's running Chrome. Uses user's logged-in sessions."""

    VISUAL = "visual"
    """Layer 4: Claude Computer Use. Screenshot-based visual interaction.
    Universal fallback for CAPTCHAs, canvas, non-browser applications. V4."""


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
