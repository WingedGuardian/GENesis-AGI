"""Shared domain classification for the internal / user_world spine.

Two jurisdictions:
  - ``internal``   — Genesis's own system work (the Genesis-COO ego's domain:
    runtime, routing, memory, health, dev work).
  - ``user_world`` — the user's life, career, content, and interests (the
    user-CEO ego's domain).

The keyword set positively detects ``internal`` ONLY. There is deliberately no
positive ``user_world`` detector: Genesis runs its own marketing/outreach
campaign, so terms like "outreach", "marketing", "discord", or "github" collide
with the user's career/content vocabulary and would produce false ``user_world``
tags. Two entry points reflect this asymmetry:

  - :func:`is_genesis_internal` — binary (internal-keyword present or not). Used
    where a definite genesis/non-genesis split is required (e.g. annotating
    conversation transcripts for the user ego).
  - :func:`classify_domain` — ``'internal'`` on a keyword hit, else ``None``.
    Never guesses ``user_world``; callers store ``None`` as "not yet classified"
    rather than a wrong guess.
"""

from __future__ import annotations

# Keywords indicating a session/item is about Genesis internals (not user-world).
GENESIS_INTERNAL_KEYWORDS = frozenset({
    "surplus", "dream cycle", "dream_cycle", "genesis runtime",
    "routing config", "circuit breaker", "guardian", "sentinel", "qdrant",
    "awareness loop", "health check", "dead letter", "model_routing",
    "worktree", "genesis-development", "dashboard fix", "ego cycle",
    "model eval", "surplus_task", "provider fallback", "watchdog",
    "systemd", "genesis server", "eval batch", "j9 eval",
    "runtime init", "embedding chain", "embedding fallback",
})


def is_genesis_internal(text: str) -> bool:
    """True if *text* contains a Genesis-internal keyword."""
    lowered = (text or "").lower()
    return any(kw in lowered for kw in GENESIS_INTERNAL_KEYWORDS)


def classify_domain(text: str) -> str | None:
    """Classify *text* into the domain spine.

    Returns ``'internal'`` on an internal-keyword hit, otherwise ``None``
    (uncertain — never guesses ``'user_world'``).
    """
    return "internal" if is_genesis_internal(text) else None
