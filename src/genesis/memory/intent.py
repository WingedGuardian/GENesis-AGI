"""Query intent classification and expansion for memory retrieval.

Classifies queries into intent categories (WHAT/WHY/HOW/WHEN/WHERE/STATUS)
and biases retrieval toward intent-appropriate memories. Also performs
query expansion via tag co-occurrence for improved FTS5 recall.

Inspired by deterministic knowledge navigation (Chudinov 2026) and
OpenClaw QMD query expansion. See:
  docs/reference/2026-04-02-competitive-landscape-harness-engineering.md
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueryIntent:
    """Classified query intent."""

    category: str  # WHAT, WHY, HOW, WHEN, WHERE, STATUS, GENERAL
    confidence: float  # 0.0-1.0
    matched_pattern: str  # pattern that triggered (debug/logging)
    # 'episodic' | 'knowledge' | 'both'. Defaults to 'both' so existing
    # tests / call sites that construct QueryIntent without specifying
    # source continue to work; classify_intent() sets it explicitly per
    # category via _INTENT_TO_SOURCE.
    recommended_source: str = "both"


# Map intent categories to the recall source that's most likely to ground
# the query. Internal lookups (WHY/WHEN/WHERE/STATUS) almost always live
# in episodic memory — past decisions, timelines, current state. WHAT and
# HOW can go either way: "what is X" might be a knowledge_base entry,
# but "what did we decide about X" is episodic. We default WHAT/HOW to
# "both" rather than guess wrong, and let RRF + activation sort it out.
# GENERAL queries have no signal — keep "both".
_INTENT_TO_SOURCE: dict[str, str] = {
    "WHY": "episodic",
    "WHEN": "episodic",
    "WHERE": "episodic",
    "STATUS": "episodic",
    "WHAT": "both",
    "HOW": "both",
    "GENERAL": "both",
}


@dataclass(frozen=True)
class IntentProfile:
    """Scoring profile for an intent category."""

    boosted_sources: frozenset[str]
    boosted_tags: frozenset[str]
    content_signals: tuple[str, ...]  # keywords to match in content


# Priority order matters: more specific intents first.
# WHY before WHAT because "what was the reason" should match WHY.
_INTENT_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("WHY", re.compile(
        r"^(why\b|what.{0,20}reason|rationale\b|motivation\b|justification\b)",
        re.IGNORECASE,
    ), 0.85),
    ("HOW", re.compile(
        r"^(how\b|steps?\s+to\b|procedure\b|process\s+for\b|instructions?\b)",
        re.IGNORECASE,
    ), 0.85),
    ("WHEN", re.compile(
        r"^(when\b|timeline\b|history\s+of\b|last\s+time\b|first\s+time\b)",
        re.IGNORECASE,
    ), 0.85),
    ("WHERE", re.compile(
        r"^(where\b|location\s+of\b|which\s+file\b|find\s+the\b)",
        re.IGNORECASE,
    ), 0.85),
    # STATUS is not anchored — often appears mid-query
    ("STATUS", re.compile(
        r"(status\s+of\b|progress\s+on\b|current\s+state\b|update\s+on\b|is\s+it\s+done\b)",
        re.IGNORECASE,
    ), 0.80),
    ("WHAT", re.compile(
        r"^(what\b|define\b|describe\b|explain\s+what\b|tell\s+me\s+about\b)",
        re.IGNORECASE,
    ), 0.80),
]

INTENT_PROFILES: dict[str, IntentProfile] = {
    "WHAT": IntentProfile(
        boosted_sources=frozenset({"session_extraction"}),
        boosted_tags=frozenset({"entity", "concept"}),
        content_signals=(),
    ),
    "WHY": IntentProfile(
        boosted_sources=frozenset({"deep_reflection", "retrospective"}),
        boosted_tags=frozenset({"decision", "evaluation"}),
        content_signals=("because", "decided", "rationale", "reason", "chose"),
    ),
    "HOW": IntentProfile(
        boosted_sources=frozenset({"auto_memory_harvest", "session_extraction"}),
        boosted_tags=frozenset({"action_item", "concept"}),
        content_signals=("step", "run", "execute", "command", "install", "configure"),
    ),
    "WHEN": IntentProfile(
        boosted_sources=frozenset({"session_extraction", "retrospective"}),
        boosted_tags=frozenset(),
        content_signals=(),
    ),
    "WHERE": IntentProfile(
        boosted_sources=frozenset({"session_extraction"}),
        boosted_tags=frozenset({"entity"}),
        content_signals=(),
    ),
    "STATUS": IntentProfile(
        boosted_sources=frozenset({"retrospective", "reflection"}),
        boosted_tags=frozenset({"action_item"}),
        content_signals=("status", "progress", "done", "pending", "complete"),
    ),
}


def classify_intent(query: str) -> QueryIntent:
    """Classify query intent using compiled regex patterns.

    Returns GENERAL with confidence 0.0 if no pattern matches. The
    ``recommended_source`` field is derived from the category via
    ``_INTENT_TO_SOURCE`` — callers that defer to intent (passing
    ``source=None`` to ``HybridRetriever.recall``) use this to route
    the query toward the right pool.
    """
    cleaned = query.strip()
    if not cleaned:
        return QueryIntent(
            category="GENERAL", confidence=0.0, matched_pattern="",
            recommended_source=_INTENT_TO_SOURCE["GENERAL"],
        )

    for category, pattern, confidence in _INTENT_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return QueryIntent(
                category=category,
                confidence=confidence,
                matched_pattern=match.group(0),
                recommended_source=_INTENT_TO_SOURCE[category],
            )

    return QueryIntent(
        category="GENERAL", confidence=0.0, matched_pattern="",
        recommended_source=_INTENT_TO_SOURCE["GENERAL"],
    )


def compute_intent_affinity(
    intent: QueryIntent,
    source: str,
    tags: list[str],
    content: str,
) -> float:
    """Compute intent affinity score for a single memory.

    Returns 0.0 for GENERAL intent (no bias).
    """
    if intent.category == "GENERAL":
        return 0.0

    profile = INTENT_PROFILES.get(intent.category)
    if profile is None:
        return 0.0

    score = 0.0
    if source in profile.boosted_sources:
        score += 2.0
    if tags and profile.boosted_tags and (profile.boosted_tags & set(tags)):
        score += 1.5
    if profile.content_signals and content:
        content_lower = content.lower()
        if any(sig in content_lower for sig in profile.content_signals):
            score += 1.0

    return score


def rank_by_intent(
    intent: QueryIntent,
    candidates: dict[str, dict],
) -> list[str]:
    """Rank candidate memory IDs by intent affinity.

    Args:
        intent: Classified query intent.
        candidates: {memory_id: {"source": str, "tags": list, "content": str}}

    Returns empty list for GENERAL intent (no bias applied).
    """
    if intent.category == "GENERAL":
        return []

    scored: list[tuple[str, float]] = []
    for mid, meta in candidates.items():
        affinity = compute_intent_affinity(
            intent,
            source=meta.get("source", ""),
            tags=meta.get("tags") or [],
            content=meta.get("content", ""),
        )
        scored.append((mid, affinity))

    # Sort by affinity descending, then by memory_id for stability
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [mid for mid, _ in scored]


# ---------------------------------------------------------------------------
# Query expansion via tag co-occurrence
# ---------------------------------------------------------------------------

@dataclass
class TagCooccurrenceIndex:
    """Lazily-built, cached index of tag co-occurrence from Qdrant payloads.

    Given a set of query keywords, finds tags that frequently co-occur with
    those keywords in the same memory's tag list. This expands FTS5 queries
    with related terms that improve recall for oblique references.
    """

    _cooccurrence: dict[str, dict[str, int]] = field(default_factory=dict)
    _memory_count: int = 0
    _built_at: float = 0.0
    _stale_threshold: float = 0.10  # rebuild when memory count changes by >10%

    def is_stale(self, current_count: int) -> bool:
        """Check if the index needs rebuilding."""
        if self._memory_count == 0:
            return True
        delta = abs(current_count - self._memory_count) / max(self._memory_count, 1)
        return delta > self._stale_threshold

    def build(self, tag_lists: list[list[str]], memory_count: int) -> None:
        """Build co-occurrence index from tag lists across all memories.

        Args:
            tag_lists: Each inner list is the tags from one memory.
            memory_count: Total memory count (for staleness tracking).
        """
        cooc: dict[str, dict[str, int]] = {}
        for tags in tag_lists:
            # Skip trivial tag lists
            if len(tags) < 2:
                continue
            # Normalize and deduplicate
            normalized = list({t.lower() for t in tags if t and not t.startswith("obs:")})
            for i, tag_a in enumerate(normalized):
                if tag_a not in cooc:
                    cooc[tag_a] = {}
                for tag_b in normalized[i + 1:]:
                    cooc[tag_a][tag_b] = cooc[tag_a].get(tag_b, 0) + 1
                    if tag_b not in cooc:
                        cooc[tag_b] = {}
                    cooc[tag_b][tag_a] = cooc[tag_b].get(tag_a, 0) + 1

        self._cooccurrence = cooc
        self._memory_count = memory_count
        self._built_at = time.monotonic()
        logger.info(
            "Tag co-occurrence index built: %d unique tags, %d memories",
            len(cooc), memory_count,
        )

    def expand(self, keywords: list[str], max_expansions: int = 5) -> list[str]:
        """Find tags that co-occur with the given keywords.

        Returns up to max_expansions additional terms, ranked by total
        co-occurrence count with the query keywords.
        """
        if not self._cooccurrence or not keywords:
            return []

        keyword_set = {k.lower() for k in keywords}
        expansion_scores: dict[str, int] = {}

        for kw in keyword_set:
            neighbors = self._cooccurrence.get(kw, {})
            for tag, count in neighbors.items():
                if tag not in keyword_set:
                    expansion_scores[tag] = expansion_scores.get(tag, 0) + count

        if not expansion_scores:
            return []

        # Sort by score descending, take top N
        ranked = sorted(expansion_scores.items(), key=lambda x: -x[1])
        return [tag for tag, _ in ranked[:max_expansions]]


# Module-level singleton — rebuilt lazily when stale
_tag_index = TagCooccurrenceIndex()


def _tokenize_query(query: str) -> list[str]:
    """Extract meaningful keywords from a query string."""
    # Remove common stop words and short tokens
    stop_words = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been",
        "do", "does", "did", "have", "has", "had", "will", "would",
        "can", "could", "should", "may", "might", "shall",
        "in", "on", "at", "to", "for", "of", "with", "by", "from",
        "it", "its", "this", "that", "these", "those",
        "i", "we", "you", "he", "she", "they", "me", "us", "my",
        "what", "why", "how", "when", "where", "which", "who",
        "about", "and", "or", "not", "but", "so", "if", "then",
    }
    tokens = re.findall(r"\w+", query.lower())
    return [t for t in tokens if t not in stop_words and len(t) > 1]


async def expand_query(
    query: str,
    qdrant_client: object,  # QdrantClient
    collections: list[str],
    *,
    max_expansions: int = 5,
) -> str:
    """Expand a query with co-occurring tags for improved FTS5 recall.

    Builds/refreshes the tag co-occurrence index lazily from Qdrant,
    then appends related terms to the query string.

    Returns the original query if expansion produces no results or
    if Qdrant is unavailable.
    """
    global _tag_index  # noqa: PLW0603

    try:
        # Check if index needs rebuilding
        total_count = 0
        for coll in collections:
            try:
                info = qdrant_client.get_collection(coll)  # type: ignore[union-attr]
                total_count += info.points_count or 0
            except Exception:
                continue

        if _tag_index.is_stale(total_count) and total_count > 0:
            # Rebuild index by scanning tag metadata
            tag_lists: list[list[str]] = []
            for coll in collections:
                try:
                    offset = None
                    while True:
                        result = qdrant_client.scroll(  # type: ignore[union-attr]
                            collection_name=coll,
                            limit=500,
                            offset=offset,
                            with_payload=["tags"],
                            with_vectors=False,
                        )
                        points, next_offset = result
                        for point in points:
                            tags = (point.payload or {}).get("tags") or []
                            if tags:
                                tag_lists.append(tags)
                        if next_offset is None:
                            break
                        offset = next_offset
                except Exception:
                    logger.warning(
                        "Failed to scan tags from %s for co-occurrence index",
                        coll, exc_info=True,
                    )

            _tag_index.build(tag_lists, total_count)

        # Expand query
        keywords = _tokenize_query(query)
        if not keywords:
            return query

        expansions = _tag_index.expand(keywords, max_expansions=max_expansions)
        if not expansions:
            return query

        # Build boolean FTS5 query: original terms AND'd, expansions OR'd
        # e.g. "configure routing" + expansions [setup, deploy] →
        #   "(configure AND routing) OR setup OR deploy"
        # This broadens recall without losing the original intent.
        original_and = " AND ".join(keywords)
        parts = [f"({original_and})"] + expansions
        expanded = " OR ".join(parts)
        logger.debug("Query expanded: %r → %r", query, expanded)
        return expanded

    except Exception:
        logger.error("Query expansion failed, using original query", exc_info=True)
        return query
