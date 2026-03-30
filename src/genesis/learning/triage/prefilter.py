"""Step 1.6 — Zero-cost prefilter gate."""

from __future__ import annotations

from genesis.learning.types import InteractionSummary

_MIN_TOKEN_COUNT = 100


def should_skip(summary: InteractionSummary) -> bool:
    """Return True when the interaction is too trivial to analyse."""
    return summary.token_count < _MIN_TOKEN_COUNT and len(summary.tool_calls) == 0
