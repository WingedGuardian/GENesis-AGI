from __future__ import annotations

import math
from datetime import UTC, datetime

from genesis.memory.classification import CLASS_WEIGHTS
from genesis.memory.types import ActivationScore


def _load_blend() -> tuple[float, float, float]:
    """(base, access, connectivity) blend — learned-knob file with fallback.

    WS-2 B5 read seam: `genesis.ledger.learned_knobs.activation_blend()`
    merges any learned overrides (bounds-checked ±20% there) over the shipped
    constants. Loaded ONCE at import (this sits on the per-memory scoring hot
    path — no per-call file I/O); `reload_blend()` re-reads after a knob
    change. Any failure falls back to the shipped 0.6/0.25/0.15.
    """
    try:
        from genesis.ledger.learned_knobs import activation_blend

        blend = activation_blend()
        return (
            float(blend["base"]),
            float(blend["access"]),
            float(blend["connectivity"]),
        )
    except Exception:
        return (0.6, 0.25, 0.15)


_BLEND_BASE, _BLEND_ACCESS, _BLEND_CONNECTIVITY = _load_blend()


def reload_blend() -> None:
    """Re-read the activation blend from the learned-knob file (B5 seam)."""
    global _BLEND_BASE, _BLEND_ACCESS, _BLEND_CONNECTIVITY
    _BLEND_BASE, _BLEND_ACCESS, _BLEND_CONNECTIVITY = _load_blend()


# Category-aware half-lives: different memory types have different relevance
# decay rates. A fact about a service doesn't decay with time; a system health
# observation is stale in hours. These values are initial estimates — tune
# based on observed retrieval quality.
_HALF_LIFE_BY_SOURCE: dict[str, float] = {
    "session_extraction": 60.0,  # Conversation content — slow decay
    "deep_reflection": 45.0,  # Strategic insights
    "reflection": 30.0,  # Routine observations
    "surplus_promotion": 60.0,  # Promoted research findings
    "retrospective": 45.0,  # Session retrospectives
    "auto_memory_harvest": 30.0,  # Automated extractions
    "fts5_reindex": 30.0,  # Migrated (unknown original provenance)
    "embedding_recovery": 30.0,  # Recovered from pending queue
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
    memory_class: str = "fact",
    last_retrieved_at: str | None = None,
) -> ActivationScore:
    """Compute activation score with category-aware decay.

    Args:
        source: Memory source type (e.g., "session_extraction", "reflection").
            Used to select category-specific half-life.
        tags: Memory tags. Capitalized tags (entity names) get 2x half-life.
        half_life_days: Explicit override. If set, bypasses category routing.
        memory_class: Classification (rule/fact/reference). Rules get 1.3x
            boost, references get 0.7x weight.
        last_retrieved_at: ISO timestamp of last retrieval. Used for
            activation-aware carryforward: recently-retrieved memories
            decay more slowly even if they were created long ago.
    """
    now_dt = datetime.fromisoformat(now) if now else datetime.now(UTC)
    created_dt = datetime.fromisoformat(created_at)
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    age_days = (now_dt - created_dt).total_seconds() / 86400

    effective_hl = _effective_half_life(source, tags, half_life_days)

    # Activation-aware carryforward: if the memory was recently retrieved
    # (within 30 days), boost its effective half-life by 1.5x. This prevents
    # memories from being penalized for being off-topic in recent sessions
    # when they were genuinely useful when their topic was active.
    _RECENTLY_GROUNDED_DAYS = 30
    _GROUNDING_BOOST = 1.5
    if last_retrieved_at:
        try:
            last_dt = datetime.fromisoformat(last_retrieved_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            days_since_retrieval = (now_dt - last_dt).total_seconds() / 86400
            if days_since_retrieval <= _RECENTLY_GROUNDED_DAYS:
                effective_hl *= _GROUNDING_BOOST
        except (ValueError, TypeError):
            pass  # Malformed timestamp — skip boost

    recency = math.exp(-0.693 * age_days / effective_hl)
    access_freq = min(1.0, math.log(1 + retrieved_count) / math.log(1 + 20))
    connectivity = min(1.0, math.log(1 + link_count) / math.log(1 + 10))
    class_weight = CLASS_WEIGHTS.get(memory_class, 1.0)
    # Base floor (default 0.6) ensures never-retrieved memories aren't unfairly
    # penalized (cold-start problem). Coefficients sum to 1.0 at maximum with
    # the shipped defaults; the blend is a B5 learned knob (see _load_blend).
    final = (
        confidence
        * recency
        * (_BLEND_BASE + _BLEND_ACCESS * access_freq + _BLEND_CONNECTIVITY * connectivity)
        * class_weight
    )

    return ActivationScore(
        memory_id="",
        base_score=confidence,
        recency_factor=recency,
        access_frequency=access_freq,
        connectivity_factor=connectivity,
        final_score=final,
    )
