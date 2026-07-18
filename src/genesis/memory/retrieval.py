from __future__ import annotations

import dataclasses
import logging
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime

import aiosqlite
from qdrant_client import QdrantClient

from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links, observations
from genesis.memory.activation import compute_activation
from genesis.memory.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from genesis.memory.intent import (
    QueryIntent,
    classify_intent,
    expand_query,
    rank_by_intent,
)
from genesis.memory.reranker import VoyageReranker
from genesis.memory.types import RetrievalResult
from genesis.observability.call_site_recorder import record_last_run
from genesis.observability.provider_activity import track_operation
from genesis.qdrant import collections as qdrant_ops

logger = logging.getLogger(__name__)


def _has_temporal_markers(query: str) -> bool:
    """Quick check for temporal language in a query."""
    from genesis.memory.temporal import has_temporal_markers

    return has_temporal_markers(query)


_SOURCE_TO_COLLECTIONS: dict[str, list[str]] = {
    "episodic": ["episodic_memory"],
    "knowledge": ["knowledge_base"],
    "both": ["episodic_memory", "knowledge_base"],
}

# Subsystems that tag their own memory writes via ``source_subsystem``.
# Foreground recall excludes these by default so automated decisional
# content (ego corrections, triage signals, reflection observations)
# doesn't pollute user-facing answers. Surplus is intentionally absent —
# its direct-session profile blocks memory writes entirely.
# Autonomy: audit/gate data must never surface to ego context (opacity).
#
# A "subsystem" here is an INTERNAL Genesis cognitive component (ego, triage,
# reflection, autonomy). A ``module`` (``src/genesis/modules/**`` — an
# external, pluggable capability: "hands, not brain", see modules/base.py) is
# NEVER a subsystem: module memory writes MUST NOT set ``source_subsystem``.
# That invariant is enforced by tests/test_memory/test_store_subsystem_coverage.py.
_KNOWN_SUBSYSTEMS: tuple[str, ...] = ("ego", "triage", "reflection", "autonomy")

# ---------------------------------------------------------------------------
# Graph-boosted retrieval constants (spec Part 1)
# ---------------------------------------------------------------------------
_BACKLINK_BOOST_COEF = 0.05  # log-scale boost per inbound link
_ADJACENCY_BOOST = 1.05  # 5% bump for cluster-coherent results
_ADJACENCY_MIN_INLINKS = 2  # need ≥2 top-K peers linking to you
_ADJACENCY_TOP_K = 20  # adjacency check on top-K after backlink boost
_FLOOR_RATIO = 0.85  # skip boosts for results below 85% of top score


def _validate_subsystem_names(names: list[str], param: str) -> None:
    """Raise ``ValueError`` if any name is not a known subsystem.

    Guards against a silent empty result from a typo (e.g.
    ``only_subsystem='automation'``): an unknown name matches no rows and
    would return nothing, hiding the caller's mistake. Loud > silent.
    """
    unknown = [n for n in names if n not in _KNOWN_SUBSYSTEMS]
    if unknown:
        msg = (
            f"{param} contains unknown subsystem(s) {unknown!r}; "
            f"valid subsystems are {list(_KNOWN_SUBSYSTEMS)}"
        )
        raise ValueError(msg)


def _resolve_subsystem_filter(
    include_subsystem: bool | list[str],
    only_subsystem: str | list[str] | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Translate the public two-param API into filter primitives.

    Returns ``(exclude_subsystems, include_only_subsystems)``:

    - ``exclude_subsystems`` — drop rows whose ``source_subsystem``
      matches; NULL (user-sourced) always passes.
    - ``include_only_subsystems`` — keep ONLY rows whose
      ``source_subsystem`` matches; NULL is excluded.

    The two primitives are mutually exclusive. ``include_subsystem``
    and ``only_subsystem`` are themselves mutually exclusive — raises
    ``ValueError`` if both are non-default.

    Empty containers (e.g. ``only_subsystem=[]``) raise ``ValueError``
    rather than silently disabling the filter. The intended "no
    subsystem opt-ins" is the default ``include_subsystem=False``.

    Resolution table:
        include_subsystem=False (default) → exclude all known subsystems
        include_subsystem=True            → no filter
        include_subsystem=["ego"]         → user content + ego
        only_subsystem="ego"              → ego writes only
    """
    if only_subsystem is not None and include_subsystem is not False:
        msg = "include_subsystem and only_subsystem are mutually exclusive; pass at most one"
        raise ValueError(msg)

    if only_subsystem is not None:
        if isinstance(only_subsystem, str):
            if not only_subsystem:
                msg = "only_subsystem must be a non-empty string or list"
                raise ValueError(msg)
            names = [only_subsystem]
        else:
            names = list(only_subsystem)
            if not names:
                msg = "only_subsystem must be a non-empty string or list"
                raise ValueError(msg)
        _validate_subsystem_names(names, "only_subsystem")
        return (None, names)

    if include_subsystem is True:
        return (None, None)

    if include_subsystem is False:
        return (list(_KNOWN_SUBSYSTEMS), None)

    # list form — include_subsystem=["ego"] means user content + ego
    if not include_subsystem:
        msg = (
            "include_subsystem list must be non-empty; "
            "pass False (default) to exclude all subsystems"
        )
        raise ValueError(msg)
    _validate_subsystem_names(include_subsystem, "include_subsystem")
    keep = set(include_subsystem)
    exclude = [s for s in _KNOWN_SUBSYSTEMS if s not in keep]
    return (exclude, None)


def _rrf_fuse(
    ranked_lists: list[list[str]],
    *,
    k: int = 60,
) -> dict[str, float]:
    """Reciprocal Rank Fusion. Returns {memory_id: fused_score}."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, mid in enumerate(ranked, 1):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
    return scores


def _rank(values: list[float]) -> list[float]:
    """Average (tie-corrected) ranks of ``values``, 1-based. Ties share the
    mean of the positions they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # 1-based mean position across the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman_rank_corr(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation in [-1, 1] (Pearson on tie-corrected ranks).

    Hand-rolled — scipy is not a dependency (MEM-005). Returns ``None`` when
    undefined: fewer than 2 points, mismatched lengths, or zero ranking
    variance in either input (e.g. all values equal).
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx**0.5 * vy**0.5)


def _get_candidate_content(
    mid: str,
    qdrant_by_id: dict[str, dict],
    fts_by_id: dict[str, dict],
) -> str:
    """Get content for a memory candidate from either Qdrant or FTS source."""
    qhit = qdrant_by_id.get(mid)
    if qhit:
        return qhit.get("payload", {}).get("content", "")
    fhit = fts_by_id.get(mid)
    if fhit:
        return fhit.get("content", "")
    return ""


def _apply_diversity_penalty(
    candidates: list[str],
    fused: dict[str, float],
    qdrant_by_id: dict[str, dict],
    fts_by_id: dict[str, dict],
    *,
    jaccard_threshold: float = 0.80,
    penalty: float = 0.5,
    max_per_cluster: int = 3,
) -> list[str]:
    """Penalize echo clusters in retrieval results.

    When multiple candidates have near-identical content (Jaccard >= threshold),
    only the highest-scored one keeps its full score. Others get penalized by
    ``penalty`` multiplier. At most ``max_per_cluster`` candidates survive from
    any echo cluster.

    Prevents sycophantic memory clusters from dominating retrieval.

    **Mutates ``fused`` in-place** — penalized scores are written directly
    into the dict. Callers that need pre-penalty scores must copy first.
    """
    from genesis.memory.source_verification import compute_jaccard

    if len(candidates) < 2:
        return candidates

    # Sort by score first so we process highest-scored candidates first
    sorted_cands = sorted(candidates, key=lambda m: fused.get(m, 0.0), reverse=True)

    # Track which cluster each candidate belongs to (Union-Find light)
    cluster_of: dict[str, int] = {}
    cluster_count: dict[int, int] = {}
    next_cluster = 0

    # Cache content to avoid repeated lookups
    content_cache: dict[str, str] = {}
    for mid in sorted_cands:
        content_cache[mid] = _get_candidate_content(mid, qdrant_by_id, fts_by_id)

    # Greedy clustering: compare each candidate against earlier (higher-scored) ones
    for i, mid in enumerate(sorted_cands):
        content_i = content_cache[mid]
        if not content_i:
            continue

        matched_cluster = None
        for j in range(i):
            earlier = sorted_cands[j]
            content_j = content_cache.get(earlier, "")
            if not content_j:
                continue
            if compute_jaccard(content_i, content_j) >= jaccard_threshold:
                matched_cluster = cluster_of.get(earlier)
                break

        if matched_cluster is not None:
            cluster_of[mid] = matched_cluster
            cluster_count[matched_cluster] = cluster_count.get(matched_cluster, 0) + 1
            # Penalize if cluster already has max members
            if cluster_count[matched_cluster] > max_per_cluster:
                # Remove entirely — too many echoes
                fused[mid] = 0.0
            else:
                # Penalize but keep
                fused[mid] *= penalty
        else:
            # New cluster
            cluster_of[mid] = next_cluster
            cluster_count[next_cluster] = 1
            next_cluster += 1

    # Return candidates that still have positive scores
    return [mid for mid in candidates if fused.get(mid, 0.0) > 0.0]


def _build_intent_ranked(
    intent: QueryIntent,
    all_ids: set[str],
    qdrant_by_id: dict[str, dict],
    fts_by_id: dict[str, dict],
) -> list[str]:
    """Build the intent-biased ranked list for RRF (empty for GENERAL — no bias)."""
    intent_ranked: list[str] = []
    if intent.category != "GENERAL":
        candidate_meta: dict[str, dict] = {}
        for mid in all_ids:
            qhit = qdrant_by_id.get(mid)
            fhit = fts_by_id.get(mid)
            if qhit:
                p = qhit.get("payload", {})
                candidate_meta[mid] = {
                    "source": p.get("source", ""),
                    "tags": p.get("tags") or [],
                    "content": p.get("content", ""),
                }
            elif fhit:
                candidate_meta[mid] = {
                    "source": fhit.get("source_type", ""),
                    "tags": [],
                    "content": fhit.get("content", ""),
                }
        intent_ranked = rank_by_intent(intent, candidate_meta)
    return intent_ranked


def _filter_scope_fts_only(
    candidates: list[str],
    qdrant_by_id: dict[str, dict],
    fts_by_id: dict[str, dict],
    *,
    wing: str | None,
    room: str | None,
    life_domain: str | None,
) -> list[str]:
    """Filter FTS5-only candidates by wing/room/life_domain (Qdrant results
    are already filtered at query time; this catches FTS5-only candidates
    that don't match the requested filters). No-op when no filter is set.

    FTS5-only candidates carry no vector, so they never appear in Qdrant's
    already-filtered results. We verify their membership against the
    AUTHORITATIVE ``wing``/``room`` now projected onto the FTS row by
    ``search_ranked`` (from the joined ``memory_metadata``) — not the
    denormalized FTS ``tags`` token, which can drift from metadata (that
    stale-token dependence is exactly why fts5_only rows were previously
    unreachable by wing-filtered recall). ``life_domain`` has no metadata
    column, so it is derived from the authoritative wing via the same
    mapping the write path uses.
    """
    if not (wing or room or life_domain):
        return candidates

    filtered: list[str] = []
    for mid in candidates:
        if qdrant_by_id.get(mid):
            # Qdrant already applied wing/room/life_domain at query time —
            # a returned hit is a guaranteed match.
            filtered.append(mid)
            continue
        fhit = fts_by_id.get(mid)
        if fhit is None:
            # No FTS row to verify against — can't confirm membership, exclude.
            continue
        row_wing = fhit.get("wing")
        if wing and row_wing != wing:
            continue
        if room and fhit.get("room") != room:
            continue
        if life_domain:
            # No life_domain column on memory_metadata; derive it the same way
            # the write path does — classify_life_domain honors an explicit
            # ``life_domain:`` tag (carried in the FTS ``tags`` string) and
            # otherwise infers from wing. This keeps the FTS path consistent
            # with the stored payload the Qdrant path filters on.
            from genesis.memory.taxonomy import classify_life_domain

            tags_str = fhit.get("tags") or ""
            tag_list = tags_str.split() if tags_str else None
            if classify_life_domain(row_wing or "general", tags=tag_list) != life_domain:
                continue
        filtered.append(mid)
    return filtered


def _assemble_results(
    *,
    top: list[str],
    qdrant_by_id: dict[str, dict],
    fts_by_id: dict[str, dict],
    fused: dict[str, float],
    raw_fused: dict[str, float],
    activation_by_id: dict[str, float],
    vector_ranked_dedup: list[str],
    seen: set[str],
    fts_ranked: list[str],
    embedding_available: bool,
    intent: QueryIntent,
) -> list[RetrievalResult]:
    """Build RetrievalResult objects for the top-ranked candidates.

    Vector/FTS ranks are recomputed positionally against the stage-6 ranked
    lists (``vector_ranked_dedup``/``seen``/``fts_ranked``) — that reach-back
    is deliberate and explicit in the parameters.

    ``fused`` carries the final (diversity-penalized) ordering scores;
    ``raw_fused`` is the pre-penalty snapshot so J-9 logging can
    read genuine retrieval quality via ``RetrievalResult.retrieval_score``.
    """
    results: list[RetrievalResult] = []
    for mid in top:
        qdrant_hit = qdrant_by_id.get(mid)
        fts_hit = fts_by_id.get(mid)

        # Determine content and metadata. ``_collection`` is the
        # authoritative first-party/external discriminator (audit D12):
        # the Qdrant collection on a vector hit (set at recall time, ~L367),
        # or the FTS row's collection tag for an FTS5-only hit.
        if qdrant_hit:
            payload = qdrant_hit.get("payload", {})
            content = payload.get("content", "")
            src = payload.get("source", "")
            mem_type = payload.get("memory_type", "")
            _collection = qdrant_hit.get("_collection", "episodic_memory")
        elif fts_hit:
            content = fts_hit.get("content", "")
            src = fts_hit.get("source_type", "")
            mem_type = fts_hit.get("collection", "")
            payload = fts_hit
            _collection = fts_hit.get("collection", "episodic_memory")
        else:
            continue

        # Determine ranks
        if embedding_available:
            v_rank = (
                vector_ranked_dedup.index(mid) + 1
                if mid in seen and mid in set(vector_ranked_dedup)
                else None
            )
        else:
            v_rank = None
        f_rank = fts_ranked.index(mid) + 1 if mid in set(fts_ranked) else None

        # Extract provenance from Qdrant payload if available
        _p = payload if qdrant_hit else {}
        _line_range = _p.get("source_line_range")
        results.append(
            RetrievalResult(
                memory_id=mid,
                content=content,
                source=src,
                memory_type=mem_type,
                score=fused[mid],
                vector_rank=v_rank,
                fts_rank=f_rank,
                activation_score=activation_by_id.get(mid, 0.0),
                payload=_p,
                source_session_id=_p.get("source_session_id"),
                transcript_path=_p.get("transcript_path"),
                source_line_range=tuple(_line_range) if _line_range else None,
                source_pipeline=_p.get("source_pipeline"),
                # WS-3 stored provenance: read from ``payload`` (not ``_p``)
                # so FTS5-only hits recover it too — their row dict carries
                # ``origin_class`` from the search_ranked LEFT JOIN. COALESCE
                # to the FTS row for a Qdrant hit whose payload predates the
                # 0054 backfill — SQLite is authoritative when the payload
                # key is absent/None. ``source_pipeline`` stays Qdrant-only:
                # it has no SQLite column and is NOT recoverable on FTS.
                origin_class=(
                    payload.get("origin_class")
                    if payload.get("origin_class") is not None
                    else (fts_hit.get("origin_class") if fts_hit else None)
                ),
                memory_class=_p.get("memory_class", "fact"),
                query_intent=intent.category,
                intent_confidence=intent.confidence,
                collection=_collection,
                # Pre-penalty score; falls back to the final score
                # for ids that predate the snapshot (defensive — every id in
                # ``top`` is in the snapshot today).
                retrieval_score=raw_fused.get(mid, fused[mid]),
            ),
        )
    return results


def _entrenchment_metrics(
    results: list[RetrievalResult],
    _scores: list[float],
    retrieved_count_by_id: dict[str, int],
    created_at_by_id: dict[str, str],
    now_str: str,
) -> tuple[float | None, float | None, float | None]:
    """MEM-005: entrenchment signal — does retrieval frequency predict final
    ranking? A positive corr(retrieved_count, score) means the activation
    loop is rewarding mere frequency in the ranking. Monitor-only (D7:
    instrument, do NOT re-rank); a sustained strong-positive trend over
    time flags entrenchment of stale-but-popular memories. Defensive — it
    reads external payload data and must never break recall.

    Returns ``(entrenchment_corr, mean_retrieved_count, mean_age_days)``.
    """
    _entrenchment = _mean_retrieved = _mean_age_days = None
    try:
        _ret_counts = [float(retrieved_count_by_id.get(r.memory_id, 0)) for r in results]
        if _ret_counts:
            _entrenchment = _spearman_rank_corr(_ret_counts, _scores)
            _mean_retrieved = round(sum(_ret_counts) / len(_ret_counts), 2)
            _now_dt = datetime.fromisoformat(now_str.replace("Z", "+00:00"))
            _ages: list[float] = []
            for r in results:
                _ca = created_at_by_id.get(r.memory_id)
                if not _ca:
                    continue
                try:
                    _ages.append(
                        (
                            _now_dt - datetime.fromisoformat(_ca.replace("Z", "+00:00"))
                        ).total_seconds()
                        / 86400.0
                    )
                except (ValueError, AttributeError):
                    continue
            if _ages:
                _mean_age_days = round(sum(_ages) / len(_ages), 1)
    except Exception:
        logger.debug("MEM-005 entrenchment metric failed", exc_info=True)
    return _entrenchment, _mean_retrieved, _mean_age_days


async def _expired_candidate_ids(
    db: aiosqlite.Connection,
    candidate_ids: set[str],
    *,
    as_of: str | None = None,
) -> set[str]:
    """Return the subset of ``candidate_ids`` whose ``invalid_at <= as_of``.

    Bitemporal post-filter for the Qdrant + event-calendar paths. The
    FTS5 path filters in-SQL via ``search_ranked``; this batched lookup
    catches candidates that entered the union from Qdrant vector search
    or the event calendar (neither sees ``memory_metadata.invalid_at``).

    NULL ``invalid_at`` is "valid forever" — never expired.
    """
    if not candidate_ids:
        return set()
    if as_of is None:
        as_of = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" * len(candidate_ids))
    sql = (
        f"SELECT memory_id FROM memory_metadata "  # noqa: S608 - literal SQL fragments; values bound as parameters
        f"WHERE memory_id IN ({placeholders}) "
        f"AND invalid_at IS NOT NULL AND invalid_at <= ?"
    )
    cursor = await db.execute(sql, (*candidate_ids, as_of))
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


class HybridRetriever:
    """Hybrid retrieval: Qdrant vectors + FTS5 text + activation scoring, fused via RRF."""

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        qdrant_client: QdrantClient,
        db: aiosqlite.Connection,
        reranker: VoyageReranker | None = None,
    ) -> None:
        self._embeddings = embedding_provider
        self._qdrant = qdrant_client
        self._db = db
        self._reranker = reranker

    async def _maybe_entity_lane_shadow(
        self,
        *,
        query: str,
        ranked_lists: list[list[str]],
        all_ids: set[str],
        limit: int,
        embedding_available: bool,
        recall_event_id: str | None,
    ) -> None:
        """Fire the entity-lane SHADOW probe (off by default). Fully contained
        (its own guard belts the helper's internal try/except) — appends one
        eval_event, never touches recall output. Called on BOTH the
        empty-candidate early return (the lane's highest-value case: organic
        recall found nothing) and the normal tail.
        """
        try:
            from genesis.memory import entity_query

            await entity_query.maybe_entity_lane_shadow(
                self._db,
                query=query,
                ranked_lists=ranked_lists,
                all_ids=all_ids,
                limit=limit,
                embedding_available=embedding_available,
                recall_event_id=recall_event_id,
            )
        except Exception:
            logger.debug("entity_lane_shadow call failed — recall unaffected", exc_info=True)

    async def recall(
        self,
        query: str,
        *,
        source: str | None = None,
        limit: int = 10,
        min_activation: float = 0.0,
        expand_query_terms: bool = True,
        wing: str | None = None,
        room: str | None = None,
        life_domain: str | None = None,
        project_type: str | None = None,
        include_subsystem: bool | list[str] = False,
        only_subsystem: str | list[str] | None = None,
        rerank: bool = True,
        include_deprecated: bool = False,
        event_id_sink: list[str] | None = None,
        skip_writeback: Callable[[RetrievalResult], bool] | None = None,
    ) -> list[RetrievalResult]:
        """Hybrid retrieval: Qdrant + FTS5 + activation, fused via RRF.

        Source selection:
            - ``source=None`` (default): classify the query's intent and
              use ``intent.recommended_source``. WHY/WHEN/WHERE/STATUS
              route to episodic; WHAT/HOW/GENERAL route to ``both`` and
              let RRF + activation sort the candidates.
            - ``source='episodic' | 'knowledge' | 'both'``: explicit
              override; intent inference is skipped for the source
              decision but still informs RRF biasing.

        ``skip_writeback``: optional predicate over the assembled
        ``RetrievalResult``s — items it returns True for are excluded from
        the retrieved_count/activation write-backs (enforce-drop surfaces
        pass their drop predicate so blocked items gain no retrieval
        credit). Fail-open: a raising predicate restores full write-backs.

            Callers that want to force ``both`` regardless of intent
            should pass ``source='both'`` explicitly.

        Subsystem filtering (mutually exclusive):
            - ``include_subsystem=False`` (default): exclude all
              automated-subsystem writes (ego/triage/reflection) —
              user-facing queries see user content only.
            - ``include_subsystem=True``: no filter — return everything.
            - ``include_subsystem=["ego"]``: user content + named
              subsystems (additive mode).
            - ``only_subsystem="ego"``: return ONLY rows from the named
              subsystem(s) — user content excluded. Used by ego's own
              self-recall path.
        """
        _t0 = time.monotonic()

        # Resolve subsystem-filter API → primitives shared with drift_recall
        # and the underlying Qdrant + FTS5 calls.
        exclude_subsystems, include_only_subsystems = _resolve_subsystem_filter(
            include_subsystem,
            only_subsystem,
        )

        # Classify intent up-front — used for both source-selection
        # (when source is None) and RRF bias (always, downstream).
        intent = classify_intent(query)

        if source is None:
            source = intent.recommended_source
        if source not in _SOURCE_TO_COLLECTIONS:
            msg = f"source must be one of {list(_SOURCE_TO_COLLECTIONS)}, got {source!r}"
            raise ValueError(msg)

        collections = _SOURCE_TO_COLLECTIONS[source]
        candidate_limit = limit * 3

        # 1. Embed query (with fallback to FTS5-only)
        vector, embedding_available = await self._embed_query(query)

        # 2. Qdrant vector search across collections (skip if no embedding)
        qdrant_results: list[dict] = []
        qdrant_by_id: dict[str, dict] = {}
        if embedding_available and vector is not None:
            qdrant_results, qdrant_by_id = await self._gather_vector_candidates(
                vector=vector,
                collections=collections,
                candidate_limit=candidate_limit,
                wing=wing,
                room=room,
                life_domain=life_domain,
                project_type=project_type,
                exclude_subsystems=exclude_subsystems,
                include_only_subsystems=include_only_subsystems,
                include_deprecated=include_deprecated,
            )

        # 2b. Event-calendar search (temporal queries)
        event_memory_ids = await self._gather_event_candidates(
            query,
            intent,
            candidate_limit,
        )

        # 2c. Expand query via tag co-occurrence (opt-in, expensive index rebuild)
        fts_query = await self._expand_fts_query(
            query,
            collections,
            expand_query_terms=expand_query_terms,
        )

        # 3. FTS5 text search (using expanded query)
        fts_results, fts_by_id = await self._gather_fts_candidates(
            query=query,
            fts_query=fts_query,
            collections=collections,
            candidate_limit=candidate_limit,
            exclude_subsystems=exclude_subsystems,
            include_only_subsystems=include_only_subsystems,
            include_deprecated=include_deprecated,
        )

        # 4. Union of all candidate memory_ids
        all_ids = set(qdrant_by_id) | set(fts_by_id) | set(event_memory_ids)

        # 4b. Phase 1.5e: drop candidates past their bitemporal invalid_at.
        # (No-op on an empty set — guard the call so both empty exits share one
        # path below.)
        if all_ids:
            all_ids, event_memory_ids = await self._filter_expired_candidates(
                all_ids,
                qdrant_by_id,
                fts_by_id,
                event_memory_ids,
            )
        if not all_ids:
            # No organic candidates from vector/FTS/event lanes. Still measure
            # whether the entity lane would surface something — its highest-value
            # case (Codex #1121 P2: don't skip the zero-hit path). No ranked
            # lists exist here, so lane novelty = its valid candidates.
            await self._maybe_entity_lane_shadow(
                query=query,
                ranked_lists=[],
                all_ids=set(),
                limit=limit,
                embedding_available=embedding_available,
                recall_event_id=None,
            )
            return []

        now_str = datetime.now(UTC).isoformat()

        # 5/5b. Batch-fetch link counts + compute activation scores
        (
            activation_by_id,
            inbound_by_id,
            retrieved_count_by_id,
            created_at_by_id,
        ) = await self._compute_activations(all_ids, qdrant_by_id, now_str)

        # 6. Build ranked lists for RRF (or FTS5-only if no embedding)
        vector_ranked_dedup: list[str] = []
        seen: set[str] = set()
        if embedding_available:
            vector_ranked = [h["id"] for h in qdrant_results if h["id"] in all_ids]
            for mid in vector_ranked:
                if mid not in seen:
                    seen.add(mid)
                    vector_ranked_dedup.append(mid)

        # FTS5 rank is negative, lower = better; results already ordered by rank
        fts_ranked = [r["memory_id"] for r in fts_results if r["memory_id"] in all_ids]

        activation_ranked = sorted(all_ids, key=lambda m: activation_by_id[m], reverse=True)

        # 6b. Build intent-biased ranked list (empty for GENERAL — no bias)
        intent_ranked = _build_intent_ranked(
            intent,
            all_ids,
            qdrant_by_id,
            fts_by_id,
        )

        # 7. Fusion: RRF if we have vector results, otherwise FTS5 + activation only
        if embedding_available:
            ranked_lists = [vector_ranked_dedup, fts_ranked, activation_ranked]
        else:
            ranked_lists = [fts_ranked, activation_ranked]
        if intent_ranked:
            ranked_lists.append(intent_ranked)
        if event_memory_ids:
            ranked_lists.append(event_memory_ids)
        fused = _rrf_fuse(ranked_lists)

        # 7.5 Cross-encoder reranking (optional, off by default) — the ONLY
        # post-fusion point where ``fused`` is reassigned rather than mutated.
        fused = await self._maybe_rerank(
            query=query,
            fused=fused,
            qdrant_by_id=qdrant_by_id,
            fts_by_id=fts_by_id,
            limit=limit,
            rerank=rerank,
        )

        # 7b. Graph boost: backlink + adjacency (floor-gated); mutates
        # ``fused`` in place.
        graph_boost_applied = await self._apply_graph_boost(fused, inbound_by_id)

        # 8. Filter by min_activation
        candidates = [mid for mid in fused if activation_by_id.get(mid, 0.0) >= min_activation]

        # 8b. Diversity penalty — collapse echo clusters
        # If multiple candidates have near-identical content (Jaccard ≥ 0.80),
        # penalize lower-ranked echoes to prevent sycophantic memory clusters
        # from dominating retrieval results.
        # Snapshot the pre-penalty scores first — the penalty mutates
        # ``fused`` in place, and J-9 must log retrieval QUALITY, not the
        # halved dedup artifact. ``fused`` (penalized) still drives ordering.
        raw_fused = dict(fused)
        candidates = _apply_diversity_penalty(
            candidates,
            fused,
            qdrant_by_id,
            fts_by_id,
        )

        # 9. Sort by fused score descending
        candidates.sort(key=lambda m: fused[m], reverse=True)

        # 9b. Filter FTS5-only candidates by wing/room/life_domain
        candidates = _filter_scope_fts_only(
            candidates,
            qdrant_by_id,
            fts_by_id,
            wing=wing,
            room=room,
            life_domain=life_domain,
        )

        # 10. Take top limit
        top = candidates[:limit]

        # (11/11b/11c retrieval write-backs moved BELOW step 12b — they need
        # the assembled results' stored origin_class for `skip_writeback`.)

        # 12. Build RetrievalResult objects
        results = _assemble_results(
            top=top,
            qdrant_by_id=qdrant_by_id,
            fts_by_id=fts_by_id,
            fused=fused,
            raw_fused=raw_fused,
            activation_by_id=activation_by_id,
            vector_ranked_dedup=vector_ranked_dedup,
            seen=seen,
            fts_ranked=fts_ranked,
            embedding_available=embedding_available,
            intent=intent,
        )

        # 12b. Stored-origin backfill (WS-3): a vector-only hit whose Qdrant
        # payload predates the origin_class payload backfill assembles with
        # origin_class=None (the FTS coalesce can't help when there's no FTS
        # row for it) — but memory_metadata has the value. Fill it here, at
        # the retriever, so EVERY RetrievalResult consumer (MCP recall/
        # proactive, voice, research, context injector) sees the stored origin
        # and the injection gate can't be bypassed by a stale payload. In the
        # steady state (payload backfill complete, store stamps at write) the
        # id list is empty and no query runs. Best-effort: on error, results
        # keep payload values.
        _no_origin = [r.memory_id for r in results if r.origin_class is None]
        if _no_origin:
            try:
                _origin_by_id = await memory_crud.origin_class_by_ids(self._db, _no_origin)
                results = [
                    dataclasses.replace(r, origin_class=_origin_by_id.get(r.memory_id))
                    if r.origin_class is None and _origin_by_id.get(r.memory_id)
                    else r
                    for r in results
                ]
            except Exception:
                logger.debug("origin_class backfill on recall failed", exc_info=True)

        # 11/11b/11c. Retrieval write-backs (Qdrant counts, observation +
        # knowledge_units sync) — each individually swallowed. Runs AFTER
        # assembly + the 12b origin backfill so `skip_writeback` sees the
        # STORED origin: an item the caller will enforce-drop must not gain
        # retrieved_count/activation credit from the very recall that blocks
        # it (Codex #1048 — blocked external content would otherwise farm
        # ranking energy from dispatched sessions). With no predicate the
        # write-back set is identical to the pre-move behavior (all of `top`).
        _wb_ids = top
        if skip_writeback is not None:
            try:
                _wb_ids = [r.memory_id for r in results if not skip_writeback(r)]
            except Exception:
                logger.debug("skip_writeback predicate failed open", exc_info=True)
                _wb_ids = top
        await self._record_retrievals(_wb_ids, qdrant_by_id, fts_by_id, now_str)

        # J-9 eval: log recall event for memory retrieval quality measurement
        from genesis.eval.j9_hooks import (
            emit_recall_diagnostics,
            emit_recall_fired,
        )

        # J-9 logs the PRE-diversity-penalty scores. ``score`` is an
        # ordering value (echo penalty applied); logging it understated the
        # quality of penalized results in top_scores/mean_score/score_spread
        # and skewed the entrenchment correlation.
        _scores = [r.retrieval_score for r in results]
        # MEM-005: entrenchment signal (monitor-only) — see _entrenchment_metrics.
        _entrenchment, _mean_retrieved, _mean_age_days = _entrenchment_metrics(
            results,
            _scores,
            retrieved_count_by_id,
            created_at_by_id,
            now_str,
        )

        recall_event_id = await emit_recall_fired(
            self._db,
            query=query,
            result_count=len(results),
            top_scores=[r.retrieval_score for r in results[:5]],
            memory_ids=[r.memory_id for r in results[:10]],
            latency_ms=(time.monotonic() - _t0) * 1000,
            source=source,
            intent_category=intent.category,
            graph_boost_applied=graph_boost_applied,
            mean_score=sum(_scores) / len(_scores) if _scores else None,
            wing=wing,
            entrenchment_corr=_entrenchment,
            mean_retrieved_count=_mean_retrieved,
            mean_age_days=_mean_age_days,
        )

        # MEM-003: hand the emitted event id back to an MCP caller so it can
        # enrich THIS event (mode / pipeline_used / post-filter counts) instead
        # of emitting a second recall_fired that double-counts in the J-9
        # aggregator and batch judge.
        if event_id_sink is not None and recall_event_id is not None:
            event_id_sink.append(recall_event_id)

        # Recall diagnostics: capture intermediate pipeline metrics
        _overlap = len(set(qdrant_by_id) & set(fts_by_id))
        await emit_recall_diagnostics(
            self._db,
            recall_event_id=recall_event_id,
            qdrant_pool_size=len(qdrant_by_id),
            fts_pool_size=len(fts_by_id),
            event_pool_size=len(event_memory_ids),
            total_candidates=len(all_ids),
            overlap_count=_overlap,
            score_spread=round(max(_scores) - min(_scores), 4) if _scores else None,
            embedding_available=embedding_available,
            intent_category=intent.category,
            intent_confidence=intent.confidence,
            query_expanded=fts_query != query,
        )

        # Entity-lane SHADOW probe (off by default; entity_lane.mode: shadow).
        # Measures whether resolving the query to entity nodes + walking the
        # entity graph would surface novel valid candidates the vector/FTS lanes
        # miss — pure observation that appends one eval_event and NEVER touches
        # ``results``. Placed after the write-backs so its insert_event commit
        # can't flush partial recall state. See also the zero-hit call above.
        await self._maybe_entity_lane_shadow(
            query=query,
            ranked_lists=ranked_lists,
            all_ids=all_ids,
            limit=limit,
            embedding_available=embedding_available,
            recall_event_id=recall_event_id,
        )

        return results

    # --- recall() stage helpers (read-only gathers/computes) ---
    # Bodies are verbatim moves out of recall(); the orchestration order,
    # guards, and early returns live in recall() itself.

    async def _embed_query(
        self,
        query: str,
    ) -> tuple[list[float] | None, bool]:
        """Stage 1: embed the query, degrading to FTS5-only on failure.

        Returns ``(vector, embedding_available)``.
        """
        embedding_available = True
        vector = None
        try:
            vector = await self._embeddings.embed(query)
            await record_last_run(
                self._db,
                "21b_query_embedding",
                provider="embedding",
                model_id="cloud-primary",
                response_text=f"Query embed: {query[:60]}",
            )
        except EmbeddingUnavailableError:
            embedding_available = False
            logger.warning("Embedding unavailable, falling back to FTS5-only retrieval")
        return vector, embedding_available

    async def _gather_vector_candidates(
        self,
        *,
        vector: list[float],
        collections: list[str],
        candidate_limit: int,
        wing: str | None,
        room: str | None,
        life_domain: str | None,
        project_type: str | None,
        exclude_subsystems: list[str] | None,
        include_only_subsystems: list[str] | None,
        include_deprecated: bool,
    ) -> tuple[list[dict], dict[str, dict]]:
        """Stage 2: Qdrant vector search across collections.

        Returns ``(qdrant_results, qdrant_by_id)`` — the score-sorted hit
        list and a first-hit-wins dedup map.
        """
        qdrant_results: list[dict] = []
        qdrant_by_id: dict[str, dict] = {}
        for coll in collections:
            with track_operation(self._embeddings.tracker, "qdrant.search"):
                hits = qdrant_ops.search(
                    self._qdrant,
                    collection=coll,
                    query_vector=vector,
                    limit=candidate_limit,
                    wing=wing,
                    room=room,
                    life_domain=life_domain,
                    project_type=project_type,
                    exclude_subsystems=exclude_subsystems,
                    include_only_subsystems=include_only_subsystems,
                    include_deprecated=include_deprecated,
                )
            for hit in hits:
                hit["_collection"] = coll
            qdrant_results.extend(hits)

        qdrant_results.sort(key=lambda h: h["score"], reverse=True)

        for hit in qdrant_results:
            mid = hit["id"]
            if mid not in qdrant_by_id:
                qdrant_by_id[mid] = hit
        return qdrant_results, qdrant_by_id

    async def _gather_event_candidates(
        self,
        query: str,
        intent: QueryIntent,
        candidate_limit: int,
    ) -> list[str]:
        """Stage 2b: event-calendar search for temporal queries.

        (Intent classified at the top of recall(); reused downstream for
        RRF bias in step 7 and for the temporal-marker check here.)
        """
        event_memory_ids: list[str] = []
        if intent.category == "WHEN" or _has_temporal_markers(query):
            try:
                from genesis.memory.temporal import parse_temporal_reference

                time_range = parse_temporal_reference(query)
                if time_range:
                    from genesis.db.crud import memory_events

                    event_memory_ids = await memory_events.get_memory_ids_in_range(
                        self._db,
                        time_range[0],
                        time_range[1],
                        limit=candidate_limit,
                    )
            except Exception:
                logger.warning("Event-calendar search failed", exc_info=True)
        return event_memory_ids

    async def _expand_fts_query(
        self,
        query: str,
        collections: list[str],
        *,
        expand_query_terms: bool,
    ) -> str:
        """Stage 2c: expand the FTS query via tag co-occurrence (degrades to
        the original query on failure)."""
        fts_query = query
        if expand_query_terms:
            try:
                fts_query = await expand_query(
                    query,
                    self._qdrant,
                    collections,
                    max_expansions=5,
                )
            except Exception:
                logger.warning("Query expansion failed, using original", exc_info=True)
        return fts_query

    async def _gather_fts_candidates(
        self,
        *,
        query: str,
        fts_query: str,
        collections: list[str],
        candidate_limit: int,
        exclude_subsystems: list[str] | None,
        include_only_subsystems: list[str] | None,
        include_deprecated: bool,
    ) -> tuple[list[dict], dict[str, dict]]:
        """Stage 3: FTS5 text search using the expanded query.

        FTS5 respects the caller's source choice — searching the wrong
        pool here is how knowledge_base entries flood episodic recall
        results purely by candidate volume. When source="both" we pass
        None so FTS5 sees every row; for a single-collection source we
        filter at the SQL level so the candidate set matches Qdrant's
        filtered search and RRF fuses comparable lists.

        Returns ``(fts_results, fts_by_id)``.
        """
        fts_is_boolean = fts_query != query  # expansion produced boolean syntax
        fts_collection = collections[0] if len(collections) == 1 else None
        fts_results = await memory_crud.search_ranked(
            self._db,
            query=fts_query,
            collection=fts_collection,
            limit=candidate_limit,
            boolean=fts_is_boolean,
            exclude_subsystems=exclude_subsystems,
            include_only_subsystems=include_only_subsystems,
            include_deprecated=include_deprecated,
        )

        fts_by_id: dict[str, dict] = {}
        for row in fts_results:
            mid = row["memory_id"]
            if mid not in fts_by_id:
                fts_by_id[mid] = row
        return fts_results, fts_by_id

    async def _filter_expired_candidates(
        self,
        all_ids: set[str],
        qdrant_by_id: dict[str, dict],
        fts_by_id: dict[str, dict],
        event_memory_ids: list[str],
    ) -> tuple[set[str], list[str]]:
        """Stage 4b (Phase 1.5e): drop candidates past their bitemporal
        invalid_at.

        FTS5 already filtered (search_ranked has SQL WHERE on invalid_at);
        Qdrant and the event calendar don't see invalid_at, so a batched
        lookup here catches their candidates before we waste activation/
        link computation on expired rows.
        Wrapped in try/except: a DB failure here should degrade to
        "no expiry filter applied" rather than crash the entire recall.

        **Mutates ``qdrant_by_id``/``fts_by_id`` in place** (pops expired
        ids); returns the shrunk ``(all_ids, event_memory_ids)``.
        """
        try:
            expired = await _expired_candidate_ids(self._db, all_ids)
        except Exception:
            logger.warning(
                "invalid_at filter failed, returning unfiltered candidates",
                exc_info=True,
            )
            expired = set()
        if expired:
            all_ids -= expired
            for mid in expired:
                qdrant_by_id.pop(mid, None)
                fts_by_id.pop(mid, None)
            event_memory_ids = [m for m in event_memory_ids if m not in expired]
        return all_ids, event_memory_ids

    async def _compute_activations(
        self,
        all_ids: set[str],
        qdrant_by_id: dict[str, dict],
        now_str: str,
    ) -> tuple[dict[str, float], dict[str, int], dict[str, int], dict[str, str]]:
        """Stages 5/5b: batch-fetch link counts (replaces N+1 per-ID loop)
        and compute activation scores using the batched link data.

        Returns ``(activation_by_id, inbound_by_id, retrieved_count_by_id,
        created_at_by_id)`` — inbound counts feed graph boost in step 7b;
        the MEM-005 maps capture retrieval frequency + age per id to measure
        (not act on) whether the activation loop entrenches frequently-
        retrieved memories.
        """
        link_counts = await memory_links.batch_link_counts(self._db, list(all_ids))

        # FTS-only rows (no Qdrant hit) otherwise fall back to now_str for
        # created_at, giving them an unearned recency = exp(0) = 1.0 and a
        # phantom age of 0 in the MEM-005 entrenchment metric. Fetch their
        # real created_at from memory_metadata (NOT NULL there); legacy FTS
        # ghosts with no metadata row keep the now_str fallback below.
        fts_only_ids = [mid for mid in all_ids if mid not in qdrant_by_id]
        meta_created_at = (
            await memory_crud.batch_created_at(self._db, fts_only_ids) if fts_only_ids else {}
        )

        activation_by_id: dict[str, float] = {}
        inbound_by_id: dict[str, int] = {}  # saved for graph boost in step 7b
        retrieved_count_by_id: dict[str, int] = {}
        created_at_by_id: dict[str, str] = {}
        for mid in all_ids:
            total_links, inbound_links = link_counts.get(mid, (0, 0))
            inbound_by_id[mid] = inbound_links

            qdrant_hit = qdrant_by_id.get(mid)
            if qdrant_hit:
                payload = qdrant_hit.get("payload", {})
                confidence = payload.get("confidence", 0.5)
                created_at = payload.get("created_at", now_str)
                retrieved_count = payload.get("retrieved_count", 0)
            else:
                confidence = 0.5
                created_at = meta_created_at.get(mid, now_str)
                retrieved_count = 0

            mem_class = payload.get("memory_class", "fact") if qdrant_hit else "fact"
            act = compute_activation(
                confidence=confidence,
                created_at=created_at,
                retrieved_count=retrieved_count,
                link_count=total_links,
                source=payload.get("source", "") if qdrant_hit else "",
                tags=payload.get("tags") or [] if qdrant_hit else [],
                now=now_str,
                memory_class=mem_class,
                last_retrieved_at=payload.get("last_retrieved_at") if qdrant_hit else None,
            )
            activation_by_id[mid] = act.final_score
            retrieved_count_by_id[mid] = retrieved_count
            created_at_by_id[mid] = created_at
        return activation_by_id, inbound_by_id, retrieved_count_by_id, created_at_by_id

    # --- recall() stage helpers (mutators / side effects) ---

    async def _maybe_rerank(
        self,
        *,
        query: str,
        fused: dict[str, float],
        qdrant_by_id: dict[str, dict],
        fts_by_id: dict[str, dict],
        limit: int,
        rerank: bool,
    ) -> dict[str, float]:
        """Stage 7.5: cross-encoder reranking (optional, off by default).

        Voyage scores live in a different range (0.0–1.0) than RRF scores
        (~0.01–0.05). To keep the score space uniform for graph boost and
        final sort, we replace the entire fused dict with positional scores
        derived from the reranker's ordering. Candidates the reranker
        didn't score are dropped — if they lacked content or fell below
        top_k, they weren't strong enough to keep.

        Returns the SAME ``fused`` object when reranking is skipped, or a
        replacement positional-score dict when it ran.
        """
        if rerank and self._reranker and self._reranker.enabled and fused:
            rerank_candidates = sorted(
                fused,
                key=fused.get,
                reverse=True,  # type: ignore[arg-type]
            )[: limit * 3]
            rerank_docs: list[dict[str, str]] = []
            for mid in rerank_candidates:
                content = ""
                qhit = qdrant_by_id.get(mid)
                if qhit:
                    content = qhit.get("payload", {}).get("content", "")
                elif mid in fts_by_id:
                    content = fts_by_id[mid].get("content", "")
                if content:
                    rerank_docs.append({"id": mid, "text": content})
            if rerank_docs:
                reranked = await self._reranker.rerank(
                    query,
                    rerank_docs,
                    top_k=limit * 2,
                )
                if reranked:
                    # Rebuild fused with only reranked candidates, using
                    # positional scores so graph boost floor-gating works.
                    fused = {item["id"]: 1.0 / (1 + rank) for rank, item in enumerate(reranked)}
        return fused

    async def _apply_graph_boost(
        self,
        fused: dict[str, float],
        inbound_by_id: dict[str, int],
    ) -> bool:
        """Stage 7b: graph boost — backlink + adjacency (floor-gated).

        **Mutates ``fused`` in place** (multiplicative boosts); never
        rebinds it. Returns whether any boost was applied.
        """
        graph_boost_applied = False
        if fused:
            top_fused = max(fused.values())
            floor_score = top_fused * _FLOOR_RATIO

            # 7b-i. Backlink boost: reward memories referenced by many others
            for mid in fused:
                if fused[mid] < floor_score:
                    continue  # floor-gated: skip weak candidates
                inbound = inbound_by_id.get(mid, 0)
                if inbound > 0:
                    fused[mid] *= 1 + _BACKLINK_BOOST_COEF * math.log(1 + inbound)
                    graph_boost_applied = True

            # 7b-ii. Adjacency boost: reward cluster coherence in top-K
            # Recompute floor after backlink boost — the top score may
            # have changed, and the adjacency gate should use the new top.
            floor_score = max(fused.values()) * _FLOOR_RATIO
            boosted_ranked = sorted(fused, key=fused.get, reverse=True)  # type: ignore[arg-type]
            top_k = boosted_ranked[:_ADJACENCY_TOP_K]
            if len(top_k) >= 3:
                try:
                    edges = await memory_links.inter_candidate_links(
                        self._db,
                        top_k,
                    )
                    intra_inbound: dict[str, int] = {}
                    for src, tgt in edges:
                        if src != tgt:
                            intra_inbound[tgt] = intra_inbound.get(tgt, 0) + 1
                    for mid, count in intra_inbound.items():
                        if count >= _ADJACENCY_MIN_INLINKS and fused[mid] >= floor_score:
                            fused[mid] *= _ADJACENCY_BOOST
                            graph_boost_applied = True
                except Exception:
                    logger.warning(
                        "Adjacency boost query failed, skipping",
                        exc_info=True,
                    )
        return graph_boost_applied

    async def _record_retrievals(
        self,
        top: list[str],
        qdrant_by_id: dict[str, dict],
        fts_by_id: dict[str, dict],
        now_str: str,
    ) -> None:
        """Stages 11/11b/11c: retrieval write-backs for the returned
        candidates. Every write is individually swallowed — a failed
        write-back must never block returning results.
        """
        # Eval-harness seam: a frozen-snapshot bench must not mutate usage
        # tracking — neither prod Qdrant payloads (shared instance; only
        # SQLite is redirected) nor its own snapshot (earlier tasks would
        # re-rank memories for later ones). See env.memory_writebacks_off.
        from genesis.env import memory_writebacks_off

        if memory_writebacks_off():
            return
        # 11. Increment retrieved_count + stamp last_retrieved_at
        for mid in top:
            qdrant_hit = qdrant_by_id.get(mid)
            if qdrant_hit:
                coll = qdrant_hit.get("_collection", "episodic_memory")
                old_count = qdrant_hit.get("payload", {}).get("retrieved_count", 0)
                try:
                    qdrant_ops.update_payload(
                        self._qdrant,
                        collection=coll,
                        point_id=mid,
                        payload={
                            "retrieved_count": old_count + 1,
                            "last_retrieved_at": now_str,
                        },
                    )
                except Exception:
                    logger.warning(
                        "Failed to update retrieved_count for %s in %s",
                        mid,
                        coll,
                        exc_info=True,
                    )

        # 11b. Sync observation retrieved_count in SQLite
        #       Extract obs:<uuid> tags from Qdrant payloads to find linked observations
        obs_ids: list[str] = []
        for mid in top:
            qdrant_hit = qdrant_by_id.get(mid)
            if qdrant_hit:
                tags = qdrant_hit.get("payload", {}).get("tags") or []
                for tag in tags:
                    if tag.startswith("obs:"):
                        obs_ids.append(tag[4:])
        if obs_ids:
            try:
                await observations.increment_retrieved_batch(self._db, obs_ids)
            except Exception:
                logger.warning(
                    "Failed to sync observation retrieved_count for %d obs",
                    len(obs_ids),
                    exc_info=True,
                )

        # 11c. Sync knowledge_units retrieved_count in SQLite
        #       Match via qdrant_id (Qdrant point ID == knowledge_units.qdrant_id)
        ku_qdrant_ids: list[str] = []
        for mid in top:
            qdrant_hit = qdrant_by_id.get(mid)
            if qdrant_hit:
                if qdrant_hit.get("_collection") == "knowledge_base":
                    ku_qdrant_ids.append(mid)
            else:
                # FTS5-only hit — check collection tag
                fts_hit = fts_by_id.get(mid)
                if fts_hit and fts_hit.get("collection") == "knowledge_base":
                    ku_qdrant_ids.append(mid)
        if ku_qdrant_ids:
            try:
                from genesis.db.crud import knowledge as knowledge_crud

                await knowledge_crud.increment_retrieved_batch(
                    self._db,
                    ku_qdrant_ids,
                )
            except Exception:
                logger.warning(
                    "Failed to sync knowledge retrieved_count for %d units",
                    len(ku_qdrant_ids),
                    exc_info=True,
                )
