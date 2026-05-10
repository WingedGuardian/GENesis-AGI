#!/usr/bin/env python3
"""PreToolUse hook (WebFetch|WebSearch): soft nudge toward MCP web tools.

Fires when CC's built-in WebFetch or WebSearch is invoked. Suggests the
Genesis MCP web_fetch/web_search tools which provide anti-bot bypass,
JS rendering, and work in all session types (including background).

Never blocks (exit 0 always). Nudges once per session via sentinel file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

_SENTINEL_PREFIX = "genesis_web_nudge_"


def _session_sentinel_path() -> str:
    """Path to a sentinel file that tracks whether we've nudged this session."""
    session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
    return os.path.join(tempfile.gettempdir(), f"{_SENTINEL_PREFIX}{session_id}")


def main() -> int:
    # Only nudge once per session
    sentinel = _session_sentinel_path()
    if os.path.exists(sentinel):
        return 0

    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0

        # Create sentinel — we've nudged
        try:
            with open(sentinel, "w") as f:
                f.write("1")
        except OSError:
            pass  # Non-critical

        # Output soft nudge as additionalContext
        nudge = (
            "Genesis MCP web tools (web_fetch, web_search) provide anti-bot bypass "
            "(Scrapling TLS impersonation), JS rendering (Crawl4AI), and work in ALL "
            "session types including background. Consider using them instead.\n"
            "- web_fetch(url) — structured content with smart fallback chain\n"
            "- web_search(query) — SearXNG (unlimited) with Brave/Tavily/Exa/Perplexity options\n"
            "CC WebFetch is best when you specifically need an AI-processed summary."
        )
        print(json.dumps({"additionalContext": nudge}))

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
