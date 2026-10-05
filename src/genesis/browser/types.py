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
    """The browser layers of the genesis-health browser tools.

    Numbered as in ``src/genesis/mcp/health/browser.py``. The value is the
    ``layer`` field of every ``browser_navigate`` result. Read-only fetching
    (``web_fetch``) comes before these and needs no browser; desktop control of
    non-web windows is outside them.
    """

    CAMOUFOX = "camoufox"
    """Layer 1 (default): Camoufox, anti-detection Firefox, persistent profile at
    ~/.genesis/camoufox-profile/, headed on display :99."""

    CHROMIUM = "chromium"
    """Layer 2: the Chromium fallback (patchright, plain Playwright if patchright
    is missing), persistent profile at ~/.genesis/browser-profile/."""

    REMOTE_CDP = "remote_cdp"
    """Layer 3: the user's own Chrome over CDP, in a tab Genesis opens."""

    TINYFISH = "tinyfish_cdp"
    """Layer 4: a TinyFish cloud browser over CDP (paid)."""


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
    browser: str = "chromium"
    """Which browser's profile: ``camoufox`` or ``chromium``."""
    error: str = ""
    """Set when the cookie database could not be read (``sessions`` is then
    empty because it is UNKNOWN, not because there are none)."""
