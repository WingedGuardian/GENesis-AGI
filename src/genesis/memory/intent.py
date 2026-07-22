"""Query intent classification and expansion for memory retrieval.

Classifies queries into intent categories (WHAT/WHY/HOW/WHEN/WHERE/STATUS)
and biases retrieval toward intent-appropriate memories. Also performs
query expansion via tag co-occurrence for improved FTS5 recall.

Inspired by deterministic knowledge navigation (Chudinov 2026) and
OpenClaw QMD query expansion. See:
  docs/reference/2026-04-02-competitive-landscape-harness-engineering.md
"""

from __future__ import annotations

import asyncio
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
    (
        "WHY",
        re.compile(
            r"^(why\b|what.{0,20}reason|rationale\b|motivation\b|justification\b)",
            re.IGNORECASE,
        ),
        0.85,
    ),
    (
        "HOW",
        re.compile(
            r"^(how\b|steps?\s+to\b|procedure\b|process\s+for\b|instructions?\b)",
            re.IGNORECASE,
        ),
        0.85,
    ),
    (
        "WHEN",
        re.compile(
            r"^(when\b|timeline\b|history\s+of\b|last\s+time\b|first\s+time\b)",
            re.IGNORECASE,
        ),
        0.85,
    ),
    (
        "WHERE",
        re.compile(
            r"^(where\b|location\s+of\b|which\s+file\b|find\s+the\b)",
            re.IGNORECASE,
        ),
        0.85,
    ),
    # STATUS is not anchored — often appears mid-query
    (
        "STATUS",
        re.compile(
            r"(status\s+of\b|progress\s+on\b|current\s+state\b|update\s+on\b|is\s+it\s+done\b)",
            re.IGNORECASE,
        ),
        0.80,
    ),
    (
        "WHAT",
        re.compile(
            r"^(what\b|define\b|describe\b|explain\s+what\b|tell\s+me\s+about\b)",
            re.IGNORECASE,
        ),
        0.80,
    ),
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
            category="GENERAL",
            confidence=0.0,
            matched_pattern="",
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
        category="GENERAL",
        confidence=0.0,
        matched_pattern="",
        recommended_source=_INTENT_TO_SOURCE["GENERAL"],
    )


# ---------------------------------------------------------------------------
# Stance classification — drives the intent-aware proactive result budget
# ---------------------------------------------------------------------------
#
# Distinct from ``classify_intent`` (which routes source + biases ranking):
# stance answers "how many memories does this prompt deserve?" for the
# proactive recall endpoint. A bare command ("restart the server") wants
# ~0-1; a decision question ("what did we decide about X") wants 5-8. The
# fixed top-3 was never the problem — a count that ignores intent was.
# The stance→count map is config-driven (config/memory_recall.yaml
# ``proactive.profiles.<name>.budgets``); this function only names the stance.

STANCES: tuple[str, ...] = ("command", "chatter", "general", "question_decision")

# Imperative verbs that, when they lead a non-interrogative prompt, mark it a
# command (act-on-the-system, minimal recall). Kept broad but action-oriented.
_COMMAND_VERBS = frozenset(
    {
        "restart",
        "run",
        "rerun",
        "deploy",
        "redeploy",
        "stop",
        "start",
        "commit",
        "push",
        "pull",
        "fix",
        "install",
        "uninstall",
        "delete",
        "remove",
        "drop",
        "add",
        "create",
        "update",
        "upgrade",
        "rebuild",
        "build",
        "revert",
        "merge",
        "rebase",
        "kill",
        "enable",
        "disable",
        "set",
        "unset",
        "clear",
        "checkout",
        "retry",
        "execute",
        "launch",
        "apply",
        "reset",
        "rollback",
        "bump",
        "regenerate",
        "reload",
        "restore",
        "sync",
        "cancel",
        "abort",
        "purge",
    }
)

# "what did we decide/agree/choose …" and kin — history-recall questions that
# deserve a wider budget even when ``classify_intent`` returns GENERAL.
_DECISION_QUESTION = re.compile(
    r"\b(decide|decided|decision|agree|agreed|chose|choose|chosen|settle|"
    r"settled|conclude|concluded|pick|picked|plan(?:ned)?\s+to|"
    r"what\s+did\s+we|why\s+did\s+we|last\s+time)\b",
    re.IGNORECASE,
)

# Intent categories that inherently want more grounding context.
_DECISION_INTENTS = frozenset({"WHY", "WHEN", "STATUS"})

_INTERROGATIVE_LEAD = re.compile(
    r"^\s*(what|why|how|when|where|which|who|is|are|do|does|did|can|could|"
    r"should|would|will|shall|may|might|has|have|had)\b",
    re.IGNORECASE,
)


def classify_stance(prompt: str) -> str:
    """Name the prompt's stance for the proactive result budget.

    One of ``STANCES``. LLM-free (this is latency-critical — runs on the
    prompt-hook path). Precedence: chatter (too little signal) → command
    (imperative-led, non-interrogative) → question_decision (decision phrasing
    or a WHY/WHEN/STATUS intent) → general (default).
    """
    cleaned = (prompt or "").strip()
    tokens = _tokenize_query(cleaned)
    # Chatter: greetings / acks / fragments — nothing worth grounding.
    if len(tokens) < 2:
        return "chatter"

    lead_is_question = bool(_INTERROGATIVE_LEAD.match(cleaned))

    # Command: leads with an imperative verb and is not phrased as a question.
    first = tokens[0].lower()
    if not lead_is_question and first in _COMMAND_VERBS:
        return "command"

    # Decision/history question: explicit decision phrasing, or an intent that
    # inherently references shared history/state.
    if _DECISION_QUESTION.search(cleaned):
        return "question_decision"
    if classify_intent(cleaned).category in _DECISION_INTENTS:
        return "question_decision"

    return "general"


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

# Tag prefixes excluded from the co-occurrence index. ``obs:`` are per-event
# observation tags (unique, noisy). The structural taxonomy tags
# (``class:``/``wing:``/``life_domain:``) are appended to EVERY memory at store
# time (see store.py), so they co-occur with all content tags and would
# dominate expansion — admitting documents that match only a broad structural
# tag and collapsing FTS5 precision (audit MEM-001). None makes a useful
# expansion term.
_INDEX_EXCLUDED_TAG_PREFIXES = ("obs:", "class:", "wing:", "life_domain:")


def _build_expanded_query(keywords: list[str], expansions: list[str]) -> str:
    """Compose an FTS5 boolean query from original keywords + expansion terms.

    Precision-preserving structure (audit MEM-001): expansion terms may only
    BOOST documents that already match an original keyword — never surface a
    document that matches an expansion alone (the old flat ``... OR exp1 OR
    exp2`` form did, which is the precision collapse this fixes).

    Multi-keyword:  ``(k1 AND k2 …) OR ((k1 OR k2 …) AND (e1 OR e2 …))``
    Single-keyword: ``(k1) OR e1 OR e2 …`` — one keyword has nothing to AND
        against, so the gate is degenerate; the flat form is kept (mitigated
        by structural tags being excluded from the index, so the remaining
        expansions are genuine content tags).

    The result is fed to FTS5 with ``boolean=True``; ``_prepare_fts5`` preserves
    the ``AND``/``OR``/parentheses (balanced by construction here).
    """
    original_and = " AND ".join(keywords)
    if not expansions:
        return f"({original_and})" if len(keywords) > 1 else original_and
    if len(keywords) >= 2:
        kw_or = " OR ".join(keywords)
        exp_or = " OR ".join(expansions)
        return f"({original_and}) OR (({kw_or}) AND ({exp_or}))"
    # Single keyword: no AND-gate possible — keep the flat boost form.
    return " OR ".join([f"({original_and})", *expansions])


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
            # Normalize and deduplicate, excluding non-semantic prefixes
            # (obs: event tags + structural taxonomy tags that co-occur with
            # everything — see _INDEX_EXCLUDED_TAG_PREFIXES / MEM-001).
            normalized = list(
                {t.lower() for t in tags if t and not t.startswith(_INDEX_EXCLUDED_TAG_PREFIXES)}
            )
            for i, tag_a in enumerate(normalized):
                if tag_a not in cooc:
                    cooc[tag_a] = {}
                for tag_b in normalized[i + 1 :]:
                    cooc[tag_a][tag_b] = cooc[tag_a].get(tag_b, 0) + 1
                    if tag_b not in cooc:
                        cooc[tag_b] = {}
                    cooc[tag_b][tag_a] = cooc[tag_b].get(tag_a, 0) + 1

        self._cooccurrence = cooc
        self._memory_count = memory_count
        self._built_at = time.monotonic()
        logger.info(
            "Tag co-occurrence index built: %d unique tags, %d memories",
            len(cooc),
            memory_count,
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


# Module-level singleton — refreshed in the BACKGROUND when stale (SWR).
_tag_index = TagCooccurrenceIndex()

# Stale-while-revalidate state for expand_query (proactive-recall latency,
# follow-up ac27b693). The pre-SWR code rebuilt the index INLINE on the first
# recall after every restart (and after >10% corpus growth) — a synchronous
# scroll of the entire corpus (hundreds of sync HTTP pages) that ran on the
# request's own event loop and could stall a single prompt for multiple
# seconds. SWR fixes that: the count-check is time-gated, and a stale index is
# refreshed by ONE background task (single-flight) while the current prompt
# proceeds with whatever the index currently holds (worst case: one prompt's
# FTS query is unexpanded — a marginal recall dip, not a multi-second stall).
_COUNT_CHECK_INTERVAL_S = 300.0  # re-poll Qdrant point counts at most this often
_last_count_check: float = 0.0  # monotonic ts of the last get_collection poll
_cached_total_count: int = 0  # last observed corpus size (drives is_stale)
_rebuild_in_flight: bool = False  # single-flight guard for the bg rebuild


def _reset_tag_index_state() -> None:
    """Test seam: reset the module-level SWR state to a cold start."""
    global _last_count_check, _cached_total_count, _rebuild_in_flight  # noqa: PLW0603
    _last_count_check = 0.0
    _cached_total_count = 0
    _rebuild_in_flight = False
    _tag_index.__init__()  # type: ignore[misc]


def _scan_tag_lists(qdrant_client: object, collections: list[str]) -> list[list[str]]:
    """Scroll every point's tags for a co-occurrence rebuild (SYNC Qdrant).

    Runs off the request path — invoked only inside the background rebuild task
    (wrapped in a thread), never inline on a recall.
    """
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
                coll,
                exc_info=True,
            )
    return tag_lists


async def _rebuild_tag_index(
    qdrant_client: object,
    collections: list[str],
    total_count: int,
) -> None:
    """Background SWR rebuild: scan tags in a thread, rebuild, clear the flag.

    Single-flight — the caller sets ``_rebuild_in_flight`` before scheduling
    this, and this clears it in a ``finally`` so a scan failure can't wedge the
    index stale forever.
    """
    global _rebuild_in_flight  # noqa: PLW0603
    try:
        # The scroll is synchronous Qdrant I/O; keep it off the event loop so a
        # large-corpus rebuild never blocks concurrent recalls.
        tag_lists = await asyncio.to_thread(_scan_tag_lists, qdrant_client, collections)
        _tag_index.build(tag_lists, total_count)
        logger.info(
            "Tag co-occurrence index rebuilt in background (%d tag-lists, corpus=%d)",
            len(tag_lists),
            total_count,
        )
    finally:
        _rebuild_in_flight = False


def _tokenize_query(query: str) -> list[str]:
    """Extract meaningful keywords from a query string."""
    # Remove common stop words and short tokens
    stop_words = {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "we",
        "you",
        "he",
        "she",
        "they",
        "me",
        "us",
        "my",
        "what",
        "why",
        "how",
        "when",
        "where",
        "which",
        "who",
        "about",
        "and",
        "or",
        "not",
        "but",
        "so",
        "if",
        "then",
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

    Refreshes the tag co-occurrence index in the BACKGROUND when stale
    (stale-while-revalidate — see the SWR state block above), then appends
    related terms to the query string. NEVER rebuilds inline: this runs on the
    per-prompt recall path, so a stale index triggers a single-flight background
    rebuild and this call proceeds with the current index (follow-up ac27b693).

    Returns the original query if expansion produces no results or
    if Qdrant is unavailable.
    """
    global _last_count_check, _cached_total_count, _rebuild_in_flight  # noqa: PLW0603

    try:
        # Time-gate the Qdrant point-count poll: the pre-SWR code hit
        # get_collection on EVERY recall (2 sync HTTP calls/prompt). Re-poll at
        # most every _COUNT_CHECK_INTERVAL_S; reuse the cached count otherwise.
        now = time.monotonic()
        if _last_count_check == 0.0 or (now - _last_count_check) >= _COUNT_CHECK_INTERVAL_S:
            total_count = 0
            for coll in collections:
                try:
                    info = qdrant_client.get_collection(coll)  # type: ignore[union-attr]
                    total_count += info.points_count or 0
                except Exception:
                    continue
            _cached_total_count = total_count
            _last_count_check = now
        else:
            total_count = _cached_total_count

        # Stale index → kick ONE background rebuild (single-flight) and proceed
        # with whatever the index currently holds. Worst case for THIS prompt:
        # an unexpanded FTS query (a marginal recall dip) instead of a
        # multi-second inline scroll stall.
        if total_count > 0 and _tag_index.is_stale(total_count) and not _rebuild_in_flight:
            _rebuild_in_flight = True
            from genesis.util.tasks import tracked_task

            tracked_task(
                _rebuild_tag_index(qdrant_client, list(collections), total_count),
                name="tag_index_rebuild",
            )

        # Expand query
        keywords = _tokenize_query(query)
        if not keywords:
            return query

        expansions = _tag_index.expand(keywords, max_expansions=max_expansions)
        if not expansions:
            return query

        # Compose the precision-preserving boolean FTS5 query (MEM-001):
        # expansion terms only boost documents already matching an original
        # keyword, never surface an expansion-only match. See
        # _build_expanded_query.
        expanded = _build_expanded_query(keywords, expansions)
        logger.debug("Query expanded: %r → %r", query, expanded)
        return expanded

    except Exception:
        logger.error("Query expansion failed, using original query", exc_info=True)
        return query
