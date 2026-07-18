"""Query→entity resolution lane for hybrid recall — SHADOW-only probe.

Measures — never changes — whether resolving a recall query to entity nodes
and walking the entity graph would surface NOVEL, valid candidates the
vector/FTS lanes miss. Off by default; ``entity_lane.mode: shadow`` in
``memory_recall.yaml`` turns on measurement (an ``entity_lane_shadow``
eval_event per recall). The live path — actually fusing the lane into RRF —
is deferred to PR-2 (see the ``GROUNDWORK(PR-2)`` marker in
:func:`entity_lane_mode`), gated on the shadow metrics.

This is the ambient worker's E4 lane (``session_awareness/ranking.py``)
transplanted into the main recall path as an observation. The scoring
constants are DUPLICATED here rather than imported to keep ``memory/`` from
depending on ``session_awareness/`` (wrong dependency direction) — keep them
in sync with ``ranking.ENTITY_*`` if that lane's scoring changes.

Measurement-integrity notes (why the numbers are honest):

- ``novel_candidates`` counts only lane memories that pass the SAME validity
  filter the main pipeline applies at ``retrieval._filter_expired_candidates``
  (bitemporally-expired / deprecated / deleted are excluded via
  ``hydrate_for_expansion`` — the visibility predicate the sibling graph lane
  already uses). ``memories_mentioning`` has no such filter, so without this
  the count would over-sell the lane.
- ``topk_delta`` is the lane's marginal effect on pure RRF ordering — an
  UPPER BOUND: it skips rerank / graph-boost / min_activation / diversity /
  scope, which the production path would apply to novel candidates. Read it
  as "the ceiling of what the lane could promote," not what it would.
- ``embedding_available`` is emitted so analysis can stratify: under the
  FTS5-only recall branch the base ranking is weaker, which inflates the
  lane's apparent marginal effect.

Merge-following: resolution matches against the ACTIVE norm_name set
(``list_norm_names``), which is post-merge survivors. A query naming a
merged-away surface form is missed (``get_by_norm_name`` would follow the
merge). With the adjudication drainer in ``propose_only`` this set is ~empty,
so the bias is a safe-direction undercount — acceptable for a probe. PR-2's
live resolver should follow merges; do NOT copy the active-set shortcut
forward as-is.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import entities as entities_crud
from genesis.db.crud import j9_eval
from genesis.db.crud import memory as memory_crud
from genesis.db.crud.entities import PROVENANCE_WEIGHTS
from genesis.memory import graph_expansion
from genesis.memory.entity_resolution import normalize_content

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# ── Scoring constants — comparability with session_awareness/ranking.py's E4
# lane (do NOT import ranking.py from memory/ — wrong dependency direction).
ENTITY_DEPTH_DECAY = 0.7  # per-hop decay on top of edge confidences (ranking.py:73)
ENTITY_MENTIONS_PER_ENTITY = 10  # ranking.py:74
ENTITY_LANE_LIMIT = 15  # max entity-lane candidates considered (ranking.py:75)
ENTITY_MAX_DEPTH = 2

_MAX_QUERY_ENTITIES = 8  # seed cap from one query
_MAX_NGRAM = 3

# Only single-token unigrams that are pure noise are skipped; multi-word
# n-grams are never dropped (a real norm_name may contain a stopword).
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "of",
        "to",
        "in",
        "on",
        "and",
        "or",
        "for",
        "with",
        "at",
        "by",
        "it",
        "this",
        "that",
        "what",
        "did",
        "do",
        "does",
        "about",
        "we",
        "i",
        "you",
        "be",
        "as",
        "from",
    }
)


def entity_lane_mode() -> str:
    """Effective entity-lane mode: ``off | shadow`` — read live from config.

    Reads the shared ``memory_recall`` config
    (``graph_expansion.load_recall_config``, mtime-cached). The module-wide
    ``enabled: false`` master kill switch forces ``off`` (parity with
    ``graph_expansion._mode_from``). Otherwise recognizes ``off``/``shadow``
    only; anything else — including ``live`` (reserved for PR-2) and typos —
    degrades to ``off``. This is deliberately MORE conservative than
    ``graph_expansion`` (which degrades unknown→shadow): an unshipped lane must
    never start running the resolver's active-norm_name scan on a hot recall
    path because of a hand-edit.
    """
    cfg = graph_expansion.load_recall_config()
    if not cfg.get("enabled", True):
        return "off"
    section = cfg.get("entity_lane")
    mode = section.get("mode") if isinstance(section, dict) else None
    if mode == "shadow":
        return "shadow"
    # GROUNDWORK(PR-2): when the live entity lane ships, recognize "live" HERE
    # and add it to settings._validate_memory_recall's entity_lane allow-set in
    # the SAME change — until both flip together, live→off (silent) so a
    # premature config flip can never half-activate the lane.
    return "off"


async def resolve_query_entities(
    db: aiosqlite.Connection,
    query: str | None,
    *,
    max_entities: int = _MAX_QUERY_ENTITIES,
) -> dict[str, float]:
    """Resolve a free-text query to seed entity ids (weight 1.0 each).

    ``normalize_content`` (alias-expand) → lowercase → whitespace n-grams
    (1..``_MAX_NGRAM``) → O(1) membership against the ACTIVE norm_name set
    (``list_norm_names``). Whitespace splitting preserves single tokens like
    ``pr#1089``; multi-word norm_names (``concurrent workstream``) resolve via
    the bi/tri-gram. Read-only: no writes, no entity creation, no LLM.
    Returns ``{entity_id: 1.0}`` capped at *max_entities*.
    """
    norm = normalize_content(query or "").lower()
    tokens = norm.split()
    if not tokens:
        return {}

    ngrams: list[str] = []
    seen: set[str] = set()
    for n in range(1, _MAX_NGRAM + 1):
        for i in range(len(tokens) - n + 1):
            gram = " ".join(tokens[i : i + n])
            if gram in seen:
                continue
            seen.add(gram)
            if n == 1 and gram in _STOPWORDS:
                continue
            ngrams.append(gram)
    if not ngrams:
        return {}

    # norm_name → [entity_id]; active-only (= post-merge survivors). One scan;
    # the entity count is small enough to hold in-process (ranking.py precedent).
    name_to_ids: dict[str, list[str]] = {}
    for norm_name, entity_id, _etype in await entities_crud.list_norm_names(db):
        name_to_ids.setdefault(norm_name, []).append(entity_id)

    weights: dict[str, float] = {}
    for gram in ngrams:
        for entity_id in name_to_ids.get(gram, ()):
            weights[entity_id] = 1.0
            if len(weights) >= max_entities:
                return weights
    return weights


async def compute_entity_lane(
    db: aiosqlite.Connection,
    weights: dict[str, float],
    *,
    as_of: str | None = None,
) -> tuple[list[str], int]:
    """Entity-graph candidate memory_ids for seed *weights*, strongest first.

    ``connected_entities`` (≤``ENTITY_MAX_DEPTH`` bitemporal hops) with per-hop
    ``ENTITY_DEPTH_DECAY`` → ``memories_mentioning`` → best-per-memory
    ``path_score = weight × mention_confidence × provenance_weight``. Returns
    ``(memory_ids capped at ENTITY_LANE_LIMIT, entities_reached_count)``. The
    memory_ids are NOT visibility-filtered — the caller applies
    ``hydrate_for_expansion``. Never raises.
    """
    if not weights:
        return [], 0
    try:
        weights = dict(weights)  # seeds at 1.0; add decayed reached below
        reached = await entities_crud.connected_entities(
            db,
            list(weights),
            max_depth=ENTITY_MAX_DEPTH,
            as_of=as_of,
        )
        for entity_id, info in reached.items():
            weights[entity_id] = info["path_confidence"] * (ENTITY_DEPTH_DECAY ** info["depth"])
        mentions = await entities_crud.memories_mentioning(
            db,
            list(weights),
            limit_per_entity=ENTITY_MENTIONS_PER_ENTITY,
        )
        best: dict[str, float] = {}
        for m in mentions:
            score = (
                weights.get(m["entity_id"], 0.0)
                * (m["confidence"] or 0.0)
                * PROVENANCE_WEIGHTS.get(m["provenance"], 0.5)
            )
            if score > best.get(m["memory_id"], -1.0):
                best[m["memory_id"]] = score
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        return [mid for mid, _ in ranked[:ENTITY_LANE_LIMIT]], len(reached)
    except Exception:
        logger.debug("entity lane compute failed", exc_info=True)
        return [], 0


def _rrf_top(ranked_lists: list[list[str]], limit: int) -> set[str]:
    """Top-*limit* memory_ids by pure RRF over *ranked_lists* (no filters)."""
    # Function-level import breaks the retrieval→entity_query import cycle
    # (ranking.py uses the same pattern for _expired_candidate_ids).
    from genesis.memory.retrieval import _rrf_fuse

    fused = _rrf_fuse(ranked_lists)
    return set(sorted(fused, key=lambda mid: fused[mid], reverse=True)[:limit])


async def maybe_entity_lane_shadow(
    db: aiosqlite.Connection,
    *,
    query: str | None,
    ranked_lists: list[list[str]],
    all_ids: set[str],
    limit: int,
    embedding_available: bool,
    recall_event_id: str | None = None,
) -> None:
    """Shadow-measure the entity lane against a completed recall.

    No-op unless ``entity_lane_mode() == "shadow"``. NEVER changes recall
    output (returns ``None``); on any error logs and returns. Emits one
    ``entity_lane_shadow`` eval_event with the fire/novelty/delta metrics that
    drive the PR-2 flip decision.
    """
    if entity_lane_mode() != "shadow":
        return
    t0 = time.monotonic()
    try:
        weights = await resolve_query_entities(db, query)
        lane_ids, entities_reached = await compute_entity_lane(db, weights) if weights else ([], 0)

        # Measurement parity: drop lane candidates the main pipeline would have
        # excluded (expired / deprecated / deleted) BEFORE counting novelty.
        valid_ids = lane_ids
        if lane_ids:
            hydrated = await memory_crud.hydrate_for_expansion(db, lane_ids)
            now_iso = datetime.now(UTC).isoformat()
            valid_ids = []
            for mid in lane_ids:
                row = hydrated.get(mid)
                if not row or not row.get("content"):
                    continue
                invalid_at = row.get("invalid_at")
                if (invalid_at is not None and invalid_at <= now_iso) or row.get("deprecated"):
                    continue
                valid_ids.append(mid)

        novel = [mid for mid in valid_ids if mid not in all_ids]

        # topk_delta: isolated marginal RRF effect (pre-filter UPPER BOUND).
        base_top = _rrf_top(ranked_lists, limit)
        with_top = _rrf_top([*ranked_lists, valid_ids], limit) if valid_ids else base_top
        topk_delta = len(with_top - base_top)

        latency_ms = int((time.monotonic() - t0) * 1000)
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type="entity_lane_shadow",
            subject_id=recall_event_id,
            metrics={
                "entities_resolved": len(weights),
                "entities_reached": entities_reached,
                "lane_candidates": len(valid_ids),
                "lane_candidates_prefilter": len(lane_ids),
                "novel_candidates": len(novel),
                "topk_delta": topk_delta,
                "sample_novel_ids": novel[:5],
                "embedding_available": bool(embedding_available),
                "latency_ms": latency_ms,
            },
        )
        logger.info(
            "entity_lane_shadow resolved=%d reached=%d lane=%d novel=%d "
            "topk_delta=%d latency_ms=%d",
            len(weights),
            entities_reached,
            len(valid_ids),
            len(novel),
            topk_delta,
            latency_ms,
        )
    except Exception:
        logger.warning("entity_lane_shadow failed — recall unaffected", exc_info=True)
