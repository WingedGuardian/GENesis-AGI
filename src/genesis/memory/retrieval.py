from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import aiosqlite
from qdrant_client import QdrantClient

from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links, observations
from genesis.memory.activation import compute_activation
from genesis.memory.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from genesis.memory.intent import classify_intent, expand_query, rank_by_intent
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
_KNOWN_SUBSYSTEMS: tuple[str, ...] = ("ego", "triage", "reflection")


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
        msg = (
            "include_subsystem and only_subsystem are mutually exclusive; "
            "pass at most one"
        )
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
        f"SELECT memory_id FROM memory_metadata "
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
    ) -> None:
        self._embeddings = embedding_provider
        self._qdrant = qdrant_client
        self._db = db

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
        include_subsystem: bool | list[str] = False,
        only_subsystem: str | list[str] | None = None,
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
            include_subsystem, only_subsystem,
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
        embedding_available = True
        vector = None
        try:
            vector = await self._embeddings.embed(query)
            await record_last_run(
                self._db, "21b_query_embedding",
                provider="embedding", model_id="cloud-primary",
                response_text=f"Query embed: {query[:60]}",
            )
        except EmbeddingUnavailableError:
            embedding_available = False
            logger.warning("Embedding unavailable, falling back to FTS5-only retrieval")

        # 2. Qdrant vector search across collections (skip if no embedding)
        qdrant_results: list[dict] = []
        qdrant_by_id: dict[str, dict] = {}
        if embedding_available and vector is not None:
            for coll in collections:
                with track_operation(self._embeddings.tracker, "qdrant.search"):
                    hits = qdrant_ops.search(
                        self._qdrant,
                        collection=coll,
                        query_vector=vector,
                        limit=candidate_limit,
                        wing=wing,
                        room=room,
                        exclude_subsystems=exclude_subsystems,
                        include_only_subsystems=include_only_subsystems,
                    )
                for hit in hits:
                    hit["_collection"] = coll
                qdrant_results.extend(hits)

            qdrant_results.sort(key=lambda h: h["score"], reverse=True)

            for hit in qdrant_results:
                mid = hit["id"]
                if mid not in qdrant_by_id:
                    qdrant_by_id[mid] = hit

        # 2b. Event-calendar search (temporal queries)
        # (Intent classified at the top of recall(); reused below for
        # RRF bias in step 7 and for the temporal-marker check here.)
        event_memory_ids: list[str] = []
        if intent.category == "WHEN" or _has_temporal_markers(query):
            try:
                from genesis.memory.temporal import parse_temporal_reference
                time_range = parse_temporal_reference(query)
                if time_range:
                    from genesis.db.crud import memory_events
                    event_memory_ids = await memory_events.get_memory_ids_in_range(
                        self._db, time_range[0], time_range[1],
                        limit=candidate_limit,
                    )
            except Exception:
                logger.warning("Event-calendar search failed", exc_info=True)

        # 2c. Expand query via tag co-occurrence (opt-in, expensive index rebuild)
        fts_query = query
        if expand_query_terms:
            try:
                fts_query = await expand_query(
                    query, self._qdrant, collections, max_expansions=5,
                )
            except Exception:
                logger.warning("Query expansion failed, using original", exc_info=True)

        # 3. FTS5 text search (using expanded query)
        # FTS5 respects the caller's source choice — searching the wrong
        # pool here is how knowledge_base entries flood episodic recall
        # results purely by candidate volume. When source="both" we pass
        # None so FTS5 sees every row; for a single-collection source we
        # filter at the SQL level so the candidate set matches Qdrant's
        # filtered search and RRF fuses comparable lists.
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
        )

        fts_by_id: dict[str, dict] = {}
        for row in fts_results:
            mid = row["memory_id"]
            if mid not in fts_by_id:
                fts_by_id[mid] = row

        # 4. Union of all candidate memory_ids
        all_ids = set(qdrant_by_id) | set(fts_by_id) | set(event_memory_ids)
        if not all_ids:
            return []

        # 4b. Phase 1.5e: drop candidates past their bitemporal invalid_at.
        # FTS5 already filtered (search_ranked has SQL WHERE on invalid_at);
        # Qdrant and the event calendar don't see invalid_at, so a batched
        # lookup here catches their candidates before we waste activation/
        # link computation on expired rows.
        # Wrapped in try/except: a DB failure here should degrade to
        # "no expiry filter applied" rather than crash the entire recall.
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
            if not all_ids:
                return []

        # 5. Compute activation scores
        now_str = datetime.now(UTC).isoformat()
        activation_by_id: dict[str, float] = {}
        for mid in all_ids:
            qdrant_hit = qdrant_by_id.get(mid)
            if qdrant_hit:
                payload = qdrant_hit.get("payload", {})
                confidence = payload.get("confidence", 0.5)
                created_at = payload.get("created_at", now_str)
                retrieved_count = payload.get("retrieved_count", 0)
            else:
                confidence = 0.5
                created_at = now_str
                retrieved_count = 0

            link_count = await memory_links.count_links(self._db, mid)
            mem_class = payload.get("memory_class", "fact") if qdrant_hit else "fact"
            act = compute_activation(
                confidence=confidence,
                created_at=created_at,
                retrieved_count=retrieved_count,
                link_count=link_count,
                source=payload.get("source", "") if qdrant_hit else "",
                tags=payload.get("tags") or [] if qdrant_hit else [],
                now=now_str,
                memory_class=mem_class,
            )
            activation_by_id[mid] = act.final_score

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

        # 8. Filter by min_activation
        candidates = [
            mid for mid in fused if activation_by_id.get(mid, 0.0) >= min_activation
        ]

        # 9. Sort by fused score descending
        candidates.sort(key=lambda m: fused[m], reverse=True)

        # 9b. Filter FTS5-only candidates by wing/room (Qdrant results
        #     are already filtered at query time; this catches FTS5-only
        #     candidates that don't match the requested wing/room).
        if wing or room:
            filtered: list[str] = []
            for mid in candidates:
                qhit = qdrant_by_id.get(mid)
                if qhit:
                    # Qdrant already filtered — guaranteed match
                    filtered.append(mid)
                else:
                    # FTS5-only candidate — no wing/room data, exclude
                    # since we can't verify membership.
                    pass
            candidates = filtered

        # 10. Take top limit
        top = candidates[:limit]

        # 11. Increment retrieved_count for returned results
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
                        payload={"retrieved_count": old_count + 1},
                    )
                except Exception:
                    logger.warning(
                        "Failed to update retrieved_count for %s in %s",
                        mid, coll, exc_info=True,
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
                    len(obs_ids), exc_info=True,
                )

        # 12. Build RetrievalResult objects
        results: list[RetrievalResult] = []
        for mid in top:
            qdrant_hit = qdrant_by_id.get(mid)
            fts_hit = fts_by_id.get(mid)

            # Determine content and metadata
            if qdrant_hit:
                payload = qdrant_hit.get("payload", {})
                content = payload.get("content", "")
                src = payload.get("source", "")
                mem_type = payload.get("memory_type", "")
            elif fts_hit:
                content = fts_hit.get("content", "")
                src = fts_hit.get("source_type", "")
                mem_type = fts_hit.get("collection", "")
                payload = fts_hit
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
            f_rank = (
                fts_ranked.index(mid) + 1
                if mid in set(fts_ranked)
                else None
            )

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
                    memory_class=_p.get("memory_class", "fact"),
                    query_intent=intent.category,
                    intent_confidence=intent.confidence,
                ),
            )

        # J-9 eval: log recall event for memory retrieval quality measurement
        from genesis.eval.j9_hooks import emit_recall_fired
        await emit_recall_fired(
            self._db,
            query=query,
            result_count=len(results),
            top_scores=[r.score for r in results[:5]],
            memory_ids=[r.memory_id for r in results[:10]],
            latency_ms=(time.monotonic() - _t0) * 1000,
            source=source,
        )

        return results
