"""Static obstacle resolution chains for V3.

Each chain is an ordered list of methods to try when an obstacle of the given
type is encountered.  The learning system will record which methods succeed
and which fail, building procedural memory over time (Phase 6+).
"""

from __future__ import annotations

CHAINS: dict[str, list[str]] = {
    "web_fetch": [
        "firecrawl",
        "playwright",
        "requests_fallback",
        "cache_lookup",
    ],
    "api_rate_limit": [
        "wait_and_retry",
        "alternative_provider",
        "cache_lookup",
        "degrade_gracefully",
    ],
    "model_unavailable": [
        "fallback_model",
        "queue_for_later",
        "local_model",
        "skip_optional",
    ],
    "tool_failure": [
        "retry_with_different_params",
        "alternative_tool",
        "manual_fallback",
        "report_gap",
    ],
    "permission_error": [
        "escalate_to_user",
        "try_alternative_path",
        "skip_with_note",
    ],
}


def get_next_method(obstacle_type: str, failed_methods: list[str]) -> str | None:
    """Return the next untried method in the chain, or None if exhausted."""
    chain = CHAINS.get(obstacle_type)
    if chain is None:
        return None
    for method in chain:
        if method not in failed_methods:
            return method
    return None


def get_chain(obstacle_type: str) -> list[str] | None:
    """Return the full chain for an obstacle type, or None if unknown."""
    return CHAINS.get(obstacle_type)
