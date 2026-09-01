"""Step 1.5 — Build an InteractionSummary from a CCOutput."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from genesis.cc.types import CCOutput
from genesis.learning.response_context import elision_marker
from genesis.learning.types import InteractionSummary

_MAX_USER_TEXT = 500

# A SAFETY VALVE against a pathological payload, not a working limit. It has to
# clear real traffic with room to spare, because a response that reaches a
# grader in fragments reads exactly like a response the model abandoned — and
# the grader's verdict is written to permanent memory. MEASURED over 30 days of
# outbound TELEGRAM traffic (n=854): mean 1036 chars, max 6006, none above 20k —
# so on that channel this valve never opens. `build_summary` is also reached from
# terminal/dashboard (`cc/conversation.py`), inbox and mail, whose responses are
# NOT bounded by a messaging limit; those are the cases the elision path exists
# for. 20k is roughly 5k tokens, which every chain model accepts (the smallest
# context in the graders' chains is 128k).
_MAX_RESPONSE_TEXT = 20_000

# Kept for the elided case: the ending is the half a grader needs to judge
# whether generation stopped early, so the tail is preserved and the MIDDLE is
# dropped. Head is generous enough to carry the response's shape.
_ELIDE_HEAD = 12_000
_ELIDE_TAIL = 4_000

# Without this the elided COUNT goes negative just past the valve, and the
# marker would report a nonsense figure. Asserted rather than commented because
# the failure is silent and only shows up in a rendered prompt.
if _ELIDE_HEAD + _ELIDE_TAIL >= _MAX_RESPONSE_TEXT:  # pragma: no cover - config guard
    raise ValueError(
        "elide head+tail must stay under the valve, or the elided count in the "
        "marker goes negative just past it"
    )

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


def _fit_response(text: str) -> str:
    """Return *text* whole, or head + an explicit marker + tail if oversized.

    Never a bare prefix. A response that simply stops mid-word is
    indistinguishable from one the model abandoned, and that is the exact
    misreading this function exists to prevent — so if anything is removed, the
    text must SAY so and must keep its ending.
    """
    if len(text) <= _MAX_RESPONSE_TEXT:
        return text
    elided = len(text) - _ELIDE_HEAD - _ELIDE_TAIL
    return (
        f"{text[:_ELIDE_HEAD]}\n\n{elision_marker(elided)}\n\n{text[-_ELIDE_TAIL:]}"
    )


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
        response_text=_fit_response(output.text),
        response_truncated=output.bg_truncated,
        tool_calls=_extract_tool_calls(output.text),
        token_count=output.input_tokens + output.output_tokens,
        channel=channel,
        timestamp=datetime.now(UTC),
    )
