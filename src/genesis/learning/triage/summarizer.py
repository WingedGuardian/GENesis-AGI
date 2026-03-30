"""Step 1.5 — Build an InteractionSummary from a CCOutput."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from genesis.cc.types import CCOutput
from genesis.learning.types import InteractionSummary

_MAX_USER_TEXT = 500
_MAX_RESPONSE_TEXT = 1000

# Patterns that indicate tool usage in CC output.
_TOOL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Tool:\s*(\w+)"),
    re.compile(r"<tool_call>\s*(\w+)"),
    re.compile(r"Using tool:\s*(\w+)"),
]


def _extract_tool_calls(text: str) -> list[str]:
    """Return deduplicated tool names found in *text*, preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for pat in _TOOL_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                result.append(name)
    return result


def build_summary(
    output: CCOutput,
    session_id: str,
    user_text: str,
    channel: str,
) -> InteractionSummary:
    """Create an :class:`InteractionSummary` from raw interaction data."""
    return InteractionSummary(
        session_id=session_id,
        user_text=user_text[:_MAX_USER_TEXT],
        response_text=output.text[:_MAX_RESPONSE_TEXT],
        tool_calls=_extract_tool_calls(output.text),
        token_count=output.input_tokens + output.output_tokens,
        channel=channel,
        timestamp=datetime.now(UTC),
    )
