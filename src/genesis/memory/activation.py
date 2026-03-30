from __future__ import annotations

import math
from datetime import UTC, datetime

from genesis.memory.types import ActivationScore

# Category-aware half-lives: different memory types have different relevance
# decay rates. A fact about a service doesn't decay with time; a system health
# observation is stale in hours. These values are initial estimates — tune
# based on observed retrieval quality.
_HALF_LIFE_BY_SOURCE: dict[str, float] = {
    "session_extraction": 60.0,     # Conversation content — slow decay
    "deep_reflection": 45.0,        # Strategic insights
    "reflection": 30.0,             # Routine observations
    "surplus_promotion": 60.0,      # Promoted research findings
    "retrospective": 45.0,          # Session retrospectives
    "auto_memory_harvest": 30.0,    # Automated extractions
    "fts5_reindex": 30.0,           # Migrated (unknown original provenance)
    "embedding_recovery": 30.0,     # Recovered from pending queue
}
_DEFAULT_HALF_LIFE = 30.0
_MAX_HALF_LIFE = 120.0


def _effective_half_life(
    source: str,
    tags: list[str] | None,
    override: float | None,
) -> float:
    """Compute category-aware half-life from source type and tags."""
    if override is not None:
        return override
    base = _HALF_LIFE_BY_SOURCE.get(source, _DEFAULT_HALF_LIFE)
    # Entity-tagged memories (capitalized proper nouns) decay slower —
    # "AgentMail is an email service" doesn't become less true over time.
    if tags and any(t and t[0].isupper() for t in tags):
        base = min(base * 2.0, _MAX_HALF_LIFE)
    return base


def compute_activation(
    confidence: float,
    created_at: str,
    retrieved_count: int,
    link_count: int,
    *,
    source: str = "",
    tags: list[str] | None = None,
    half_life_days: float | None = None,
    now: str | None = None,
) -> ActivationScore:
    """Compute activation score with category-aware decay.

    Args:
        source: Memory source type (e.g., "session_extraction", "reflection").
            Used to select category-specific half-life.
        tags: Memory tags. Capitalized tags (entity names) get 2x half-life.
        half_life_days: Explicit override. If set, bypasses category routing.
    """
    now_dt = (
        datetime.fromisoformat(now) if now else datetime.now(UTC)
    )
    created_dt = datetime.fromisoformat(created_at)
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    age_days = (now_dt - created_dt).total_seconds() / 86400

    effective_hl = _effective_half_life(source, tags, half_life_days)
    recency = math.exp(-0.693 * age_days / effective_hl)
    access_freq = min(1.0, math.log(1 + retrieved_count) / math.log(1 + 20))
    connectivity = min(1.0, math.log(1 + link_count) / math.log(1 + 10))
    final = confidence * recency * (0.5 + 0.3 * access_freq + 0.2 * connectivity)

    return ActivationScore(
        memory_id="",
        base_score=confidence,
        recency_factor=recency,
        access_frequency=access_freq,
        connectivity_factor=connectivity,
        final_score=final,
    )
