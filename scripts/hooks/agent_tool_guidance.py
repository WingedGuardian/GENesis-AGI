#!/usr/bin/env python3
"""PreToolUse hook (Agent): nudge caller to include tool guidance in agent prompts.

When dispatching subagents for research or exploration, the caller often
forgets to tell the agent about Genesis's web and code intelligence tools.
This hook provides a soft reminder to include tool guidance.

Smart filtering: only nudges when the prompt contains research/web/explore
keywords. Nudges once per session via sentinel. Never blocks (exit 0).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload, session_id  # noqa: E402

_SENTINEL_PREFIX = "genesis_agent_guidance_"

# Keywords that suggest the agent will need web or code discovery tools
_RESEARCH_KEYWORDS = re.compile(
    r"\b(fetch|search|web|url|http|research|explore|find|discover|investigate"
    r"|codebase|architecture|symbol|function|class|import|call.?chain)\b",
    re.IGNORECASE,
)


def _session_sentinel_path(sid: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"{_SENTINEL_PREFIX}{sid}")


def main() -> int:
    payload = read_payload()

    # Only nudge once per session
    sentinel = _session_sentinel_path(session_id(payload))
    if os.path.exists(sentinel):
        return 0

    try:
        prompt = field(payload, "prompt")

        # Only nudge for research/exploration-type prompts
        if not _RESEARCH_KEYWORDS.search(prompt):
            return 0

        # Create sentinel
        try:
            with open(sentinel, "w") as f:
                f.write("1")
        except OSError:
            pass

        nudge = (
            "When dispatching agents for research or exploration, include tool guidance:\n"
            "- Web: use web_fetch(url) and web_search(query) MCP tools (anti-bot, JS rendering)\n"
            "- Code: use CBM search_graph/trace_path, Serena find_symbol/find_referencing_symbols\n"
            "- These MCP tools are available to the agent. Prefer them over CC WebFetch/WebSearch and raw Grep."
        )
        print(json.dumps({"additionalContext": nudge}))

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
