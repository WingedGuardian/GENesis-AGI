"""Core memory tools: recall, store, extract, proactive, core_facts, stats, expand."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime

from genesis.memory.activation import compute_activation
from genesis.memory.graph import traverse as graph_traverse
from genesis.memory.provenance import (
    is_external,
    label_result_dicts,
    provenance_descriptor,
    wrap_external_recall,
)
from genesis.security import immunity_shadow

from ..memory import mcp
from ._scoring import DEFAULT_KB_FLOOR_RATIO, relative_kb_floor


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod


logger = logging.getLogger(__name__)


def _increment_retrieved(qdrant, results) -> None:
    """Increment retrieved_count for results not tracked by HybridRetriever."""
    from genesis.env import memory_writebacks_off
    from genesis.qdrant import collections as qdrant_ops

    # Eval-harness seam: frozen-snapshot bench recalls must not mutate the
    # shared prod Qdrant's usage payloads. See env.memory_writebacks_off.
    if memory_writebacks_off():
        return

    for r in results:
        for coll in ("episodic_memory", "knowledge_base"):
            try:
                pts = qdrant.retrieve(coll, ids=[r.memory_id], with_payload=True)
                if pts:
                    old = (pts[0].payload or {}).get("retrieved_count", 0)
                    qdrant_ops.update_payload(
                        qdrant,
                        collection=coll,
                        point_id=r.memory_id,
                        payload={"retrieved_count": old + 1},
                    )
                    break
            except Exception:
                pass


@mcp.tool()
async def memory_recall(
    query: str,
    source: str | None = None,
    limit: int = 10,
    min_activation: float = 0.0,
    compact: bool = False,
    wing: str | None = None,
    room: str | None = None,
    life_domain: str | None = None,
    include_graph: bool = True,
    expand_query_terms: bool = True,
    mode: str = "auto",
    time_range: str | None = None,
    include_subsystem: bool | list[str] = False,
    only_subsystem: str | list[str] | None = None,
    rerank: bool = True,
    include_deprecated: bool = False,
    corrective: bool = True,
) -> list[dict]:
    """Hybrid search: Qdrant vectors + FTS5, RRF fusion, with optional graph enrichment.

    Routing: for ingested docs/APIs use knowledge_recall, for credentials/URLs
    use reference_lookup, for learned procedures use procedure_recall.
    memory_recall with source='both' searches everything but without
    domain-specific filtering or credential audit logging.

    Args:
        source: 'episodic' | 'knowledge' | 'both' | None. Defaults to
            ``'both'`` — searches episodic and knowledge_base collections.
            Knowledge results below a score floor are filtered to reduce noise.
            Use ``knowledge_recall`` MCP tool for knowledge-base lookups,
            or pass ``'both'`` / ``'knowledge'`` explicitly if needed.
        compact: If True, return lightweight previews only (memory_id, preview,
            score, wing, room, memory_class, source). Use memory_expand to
            fetch full content for specific IDs. Saves tokens and ~500ms.
        wing: Filter results to this structural domain (e.g., "infrastructure").
        room: Filter results to this topic within a wing.
        life_domain: Filter by life domain: "personal", "employment", or "genesis".
        include_graph: If False, skip graph traversal (saves ~500ms per call).
        expand_query_terms: If True, expand the FTS5 query via tag co-occurrence
            analysis (~500ms first call, ~10ms cached). Broadens recall for
            ambiguous queries. Default on — catches poor query formulation.
            Note: does not apply to the drift_recall fallback path (if wired).
        mode: Retrieval mode. "auto" = standard + drift fallback (default).
            "standard" = hybrid only, no drift fallback. "drift" = skip
            standard recall, use 3-phase drift retrieval directly. Drift
            mode ignores wing/room filters (discovers clusters dynamically).
        time_range: Explicit date range filter as "YYYY-MM-DD/YYYY-MM-DD".
            Queries the SVO event calendar and boosts temporally matching
            memories in RRF fusion. Automatic temporal detection also runs
            on queries with temporal language (e.g., "what happened last week").
        include_subsystem: Subsystem-filter additive mode. ``False`` (default)
            excludes automated-subsystem writes (ego corrections, triage
            signals, reflection observations). ``True`` returns everything.
            A list (e.g. ``["ego"]``) augments user content with the named
            subsystems. Mutually exclusive with ``only_subsystem``.
        only_subsystem: Subsystem-filter replace mode. Return ONLY rows
            tagged with the named subsystem(s); user content excluded.
            Used by ego's own self-recall path.
        rerank: If True, apply Voyage AI cross-encoder reranking after RRF
            fusion. Improves precision by rescoring candidates on semantic
            relevance. Adds ~300ms latency. Default True — disable with
            rerank=False for latency-sensitive calls.
        include_deprecated: If True, include superseded/deprecated memories
            in results. Default False — only current (non-deprecated) memories
            are returned. Use True for audit/history queries (e.g., tracing
            how a belief evolved over time).
    """
    import time as _time

    # Validate life_domain early to catch typos before expensive search
    if life_domain is not None:
        from genesis.memory.taxonomy import LIFE_DOMAINS

        if life_domain not in LIFE_DOMAINS:
            return [
                {"error": f"life_domain must be one of {sorted(LIFE_DOMAINS)}, got {life_domain!r}"}
            ]

    _t0 = _time.monotonic()
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._retriever is not None and memory_mod._db is not None

    # Default to searching both episodic and knowledge collections.
    # This ensures curated KB content surfaces alongside conversational
    # memory without requiring explicit source='both' on every call.
    # Internal callers (ego, reflection, extraction) use _retriever.recall()
    # directly and are unaffected by this MCP-layer default.
    if source is None:
        source = "both"

    pipeline_used = mode  # track which pipeline actually ran

    # Explicit time_range: query event calendar and merge IDs into results
    event_boost_ids: set[str] = set()
    if time_range:
        try:
            parts = time_range.split("/", 1)
            if len(parts) == 2:
                from genesis.db.crud import memory_events

                event_boost_ids = set(
                    await memory_events.get_memory_ids_in_range(
                        memory_mod._db,
                        parts[0],
                        parts[1],
                        limit=limit * 3,
                    )
                )
        except Exception:
            logger.warning("time_range event query failed", exc_info=True)

    # MEM-003: collect the recall_fired event id the retriever emits (standard /
    # auto_drift paths) so the single event is enriched below instead of a second
    # one being emitted. Drift mode calls drift_recall (no emit) → stays empty.
    recall_event_sink: list[str] = []

    if mode == "drift":
        # Direct DRIFT invocation — skip standard recall entirely
        from genesis.memory.drift import drift_recall

        results = await drift_recall(
            query,
            db=memory_mod._db,
            qdrant_client=memory_mod._qdrant,
            embedding_provider=memory_mod._retriever._embeddings,
            source=source,
            limit=limit,
            min_activation=min_activation,
            include_subsystem=include_subsystem,
            only_subsystem=only_subsystem,
        )
        _increment_retrieved(memory_mod._qdrant, results)
    else:
        results = await memory_mod._retriever.recall(
            query,
            source=source,
            limit=limit,
            min_activation=min_activation,
            wing=wing,
            room=room,
            life_domain=life_domain,
            expand_query_terms=expand_query_terms,
            include_subsystem=include_subsystem,
            only_subsystem=only_subsystem,
            rerank=rerank,
            include_deprecated=include_deprecated,
            event_id_sink=recall_event_sink,
        )
        pipeline_used = "standard"

        # Drift fallback (mode="auto" only): when standard recall returns
        # sparse results, try drift retrieval automatically.
        if mode == "auto" and len(results) < min(3, limit) and limit >= 3 and not wing and not room:
            try:
                from genesis.memory.drift import drift_recall

                drift_results = await drift_recall(
                    query,
                    db=memory_mod._db,
                    qdrant_client=memory_mod._qdrant,
                    embedding_provider=memory_mod._retriever._embeddings,
                    source=source,
                    limit=limit,
                    min_activation=min_activation,
                    include_subsystem=include_subsystem,
                    only_subsystem=only_subsystem,
                )
                if len(drift_results) > len(results):
                    logger.info(
                        "drift_recall fallback: standard=%d → drift=%d results (query=%r)",
                        len(results),
                        len(drift_results),
                        query[:80],
                    )
                    results = drift_results
                    _increment_retrieved(memory_mod._qdrant, drift_results)
                    pipeline_used = "auto_drift"
            except Exception:
                logger.warning("drift_recall fallback failed", exc_info=True)

    # Boost event-calendar matches from explicit time_range
    if event_boost_ids:
        from genesis.db.crud import memory as memory_crud
        from genesis.memory.types import RetrievalResult

        result_ids = {r.memory_id for r in results}
        missing = event_boost_ids - result_ids
        for mid in list(missing)[:limit]:
            try:
                row = await memory_crud.get_by_id(memory_mod._db, mid)
                if row:
                    results.append(
                        RetrievalResult(
                            memory_id=mid,
                            content=row.get("content", ""),
                            source=row.get("source_type", ""),
                            memory_type=row.get("collection", ""),
                            score=0.01,
                            vector_rank=None,
                            fts_rank=None,
                            activation_score=0.0,
                            payload=row,
                            source_pipeline="event_calendar",
                            collection=row.get("collection", "episodic_memory"),
                        )
                    )
            except Exception:
                logger.warning(
                    "Failed to fetch event-calendar memory %s",
                    mid,
                    exc_info=True,
                )

    # KB noise control: when searching both collections, apply a score floor
    # to knowledge_base results to suppress low-relevance bulk intelligence.
    # Episodic results are unfiltered. The floor is RELATIVE to the strongest
    # KB hit (audit MEM-004) — scale-invariant across reranker-on (positional)
    # and reranker-off (RRF) score scales, where the old absolute 0.15 floor
    # dropped most/all KB. Keyed on the authoritative ``collection``
    # discriminator (audit D12), not payload["scope"] — the latter is absent on
    # FTS5-only / pre-scope rows, so the old check silently let low-relevance KB
    # through that path.
    if source == "both":
        results = relative_kb_floor(
            results,
            ratio=DEFAULT_KB_FLOOR_RATIO,
            score_of=lambda r: r.score,
            is_kb=lambda r: is_external(r.collection),
        )

    # MCP-layer instrumentation: exactly ONE recall_fired per logical recall
    # (MEM-003). The standard / auto_drift paths already emitted one inside
    # recall() (its id is in recall_event_sink) — enrich THAT event in place with
    # the MCP-layer attribution (mode / pipeline_used) and the final post-filter
    # results. Drift mode emitted nothing (empty sink) → emit a fresh event here.
    recall_event_id: str | None = recall_event_sink[0] if recall_event_sink else None
    try:
        # J-9 quality metrics use the PRE-diversity-penalty score
        # (retrieval_score); ``score`` is the final ordering value with the
        # echo-cluster penalty applied — logging it understates penalized
        # results. Fall back to ``score`` for paths that don't populate
        # retrieval_score (0.0 == unset; real fused scores are always > 0).
        _top_scores = [r.retrieval_score or r.score for r in results[:5]]
        _memory_ids = [r.memory_id for r in results[:10]]
        _all_scores = [r.retrieval_score or r.score for r in results]
        _mean_score = round(sum(_all_scores) / len(_all_scores), 4) if _all_scores else None
        _latency_ms = round((_time.monotonic() - _t0) * 1000, 1)
        if recall_event_id is not None:
            from genesis.eval.j9_hooks import update_recall_metrics

            # Realign the retriever's event with the FINAL returned set — the KB
            # floor and any auto_drift fallback change result_count / ids / scores
            # after the inner emit. The MEM-005 entrenchment fields stay as the
            # retriever computed them (they need per-memory retrieved_count not
            # available here); on the rare auto_drift path they describe the
            # sparse standard pool — pipeline_used="auto_drift" flags those events.
            await update_recall_metrics(
                memory_mod._db,
                recall_event_id,
                mode=mode,
                pipeline_used=pipeline_used,
                result_count=len(results),
                top_scores=_top_scores,
                memory_ids=_memory_ids,
                mean_score=_mean_score,
                latency_ms=_latency_ms,
            )
        else:
            from genesis.eval.j9_hooks import emit_recall_fired

            recall_event_id = await emit_recall_fired(
                memory_mod._db,
                query=query,
                result_count=len(results),
                top_scores=_top_scores,
                memory_ids=_memory_ids,
                latency_ms=_latency_ms,
                source=source,
                mode=mode,
                pipeline_used=pipeline_used,
                mean_score=_mean_score,
            )
    except Exception:
        pass  # instrumentation must never break recall

    if compact:
        # WS-3 B1 gate 4 (injection): the compact branch returns external
        # previews and RETURNS before the full-path wrap/emit below — record
        # here too so compact recalls are not undercounted (observe-only).
        blockable = sum(
            1
            for r in results
            if immunity_shadow.item_is_blockable(
                collection=r.collection,
                source_pipeline=r.source_pipeline,
                origin_class=r.origin_class,
            )
        )
        await immunity_shadow.record_would_block(
            gate="injection",
            source_kind="recall_inject",
            source_ref="mcp/memory/core.py::memory_recall",
            process="server",
            blockable_count=blockable,
            db=memory_mod._db,
            detail={"path": "compact"},
        )
        return [
            {
                "memory_id": r.memory_id,
                "preview": r.content[:150].replace("\n", " "),
                "score": round(r.score, 3),
                "activation": round(r.activation_score, 3),
                "memory_class": r.memory_class,
                "wing": r.payload.get("wing", ""),
                "room": r.payload.get("room", ""),
                "source": r.source,
                "source_pipeline": r.source_pipeline or "",
                # Provenance (audit D12): first-party memory vs external-world KB,
                # so the model never treats ingested content as its own truth.
                "provenance": provenance_descriptor(
                    collection=r.collection,
                    source_pipeline=r.source_pipeline,
                    source_doc=r.source,
                ),
            }
            for r in results
        ]

    enriched = []
    graph_budget_ms = 500.0
    graph_elapsed_ms = 0.0
    for r in results:
        d = asdict(r)
        if include_graph and graph_elapsed_ms < graph_budget_ms:
            try:
                traversal = await graph_traverse(
                    memory_mod._db,
                    r.memory_id,
                    max_depth=2,
                    min_strength=0.3,
                )
                graph_elapsed_ms += traversal.query_ms
                if traversal.nodes:
                    d["graph_neighbors"] = [
                        {
                            "memory_id": n.memory_id,
                            "link_type": n.link_type,
                            "depth": n.depth,
                            "strength": n.strength,
                        }
                        for n in traversal.nodes[:5]
                    ]
            except Exception:
                logger.warning(
                    "Graph enrichment failed for %s",
                    r.memory_id,
                    exc_info=True,
                )
        enriched.append(d)

    # Selective corrective retrieval (CRAG) — high-stakes explicit recall path.
    # Default ON; gated + fail-fast so a confident/healthy recall is untouched.
    # NOTE: only the full (non-compact) path runs corrective — the compact branch
    # above returns truncated previews with no gradeable content; those callers
    # expand later via memory_expand. (compact-corrective: deferred follow-up.)
    if corrective:
        from genesis.memory.corrective import maybe_correct_recall

        enriched = await maybe_correct_recall(
            query=query,
            results=enriched,
            retriever=memory_mod._retriever,
            db=memory_mod._db,
            path="memory",
            pipeline_used=pipeline_used,
            recall_event_id=recall_event_id,
        )
    # Provenance pass (audit D12): label original + any CRAG-augmented items as
    # first-party vs external-world. Runs regardless of `corrective` so the
    # output contract is uniform.
    label_result_dicts(enriched, default_collection="episodic_memory")
    # Injection defense (PR2): structurally delimit external-world content so
    # the model treats it as data, not first-party instructions. Gate on the
    # post-label collection so CRAG web-fallback items are covered too.
    blockable = 0
    for d in enriched:
        if isinstance(d, dict) and is_external(d.get("collection")):
            d["content"] = wrap_external_recall(
                d.get("content", ""),
                source_pipeline=d.get("source_pipeline"),
            )
            if immunity_shadow.item_is_blockable(
                collection=d.get("collection"),
                source_pipeline=d.get("source_pipeline"),
                origin_class=d.get("origin_class"),
            ):
                blockable += 1
    # WS-3 B1 gate 4 (injection): shadow-record that external content reached
    # this action-capable prompt (observe-only — the item still reaches the model).
    await immunity_shadow.record_would_block(
        gate="injection",
        source_kind="recall_inject",
        source_ref="mcp/memory/core.py::memory_recall",
        process="server",
        blockable_count=blockable,
        db=memory_mod._db,
    )
    return enriched


_UUID_LEN = 36
# Hex (with optional dashes) 4–35 chars — a partial memory UUID. Anything
# else (full UUIDs, non-hex ids) bypasses prefix resolution untouched.
_ID_PREFIX_RE = re.compile(r"^[0-9a-f][0-9a-f-]{3,34}$")


async def _resolve_id_prefixes(
    db,
    memory_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Resolve short hex handles to full memory UUIDs via memory_metadata.

    The proactive memory hook surfaces memories as ``id:<8-char-prefix>``
    handles and documents memory_expand as the expansion path, so those
    handles must resolve here. Unknown prefixes pass through unchanged (they
    surface in ``not_found``); ambiguous prefixes are never guessed; DB
    errors fail open (identical behavior to no resolver).

    Returns ``(resolved_ids, ambiguous_handles)``.
    """
    resolved: list[str] = []
    ambiguous: list[str] = []
    for raw in memory_ids:
        mid = raw.strip().lower().removeprefix("id:")
        if len(mid) >= _UUID_LEN or not _ID_PREFIX_RE.match(mid):
            resolved.append(mid)
            continue
        try:
            from genesis.db.crud.memory import match_id_prefix

            matches = await match_id_prefix(db, mid, limit=2)
        except Exception:
            logger.debug("prefix resolution failed for %r", raw, exc_info=True)
            resolved.append(mid)
            continue
        if len(matches) == 1:
            resolved.append(matches[0])
        elif matches:
            ambiguous.append(raw)
        else:
            resolved.append(mid)
    return resolved, ambiguous


@mcp.tool()
async def memory_expand(
    memory_ids: list[str],
) -> list[dict]:
    """Fetch full content + graph neighbors for specific memory IDs.

    Use after a compact memory_recall to selectively expand interesting results.
    Accepts full UUIDs or the proactive hook's short handles (``id:xxxxxxxx``
    or bare 8-char hex prefixes). Returns full RetrievalResult data with graph
    enrichment for each ID found; unresolved or ambiguous handles are reported
    in a trailing ``{"not_found": [...], "ambiguous": [...]}`` entry instead
    of being silently dropped.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._qdrant is not None and memory_mod._db is not None

    memory_ids, ambiguous = await _resolve_id_prefixes(memory_mod._db, memory_ids)

    # Batch retrieve from all collections (episodic_memory + knowledge_base).
    # Track which collection each point came from — it's the authoritative
    # first-party/external discriminator (audit D12) and is otherwise lost once
    # the per-collection results are flattened into one list.
    points: list = []
    point_collection: dict[str, str] = {}
    for coll in ("episodic_memory", "knowledge_base"):
        try:
            got = memory_mod._qdrant.retrieve(
                collection_name=coll,
                ids=memory_ids,
                with_payload=True,
            )
            for p in got:
                point_collection[str(p.id)] = coll
            points.extend(got)
        except Exception:
            logger.warning("Qdrant retrieve from %s failed", coll, exc_info=True)

    if not points:
        trailer: dict = {"not_found": memory_ids}
        if ambiguous:
            trailer["ambiguous"] = ambiguous
        return [trailer]

    found_ids = {str(p.id) for p in points}
    not_found = [mid for mid in memory_ids if mid not in found_ids]

    # J-9 eval: implicit relevance signal — expanded memories are "used"
    if found_ids:
        try:
            from genesis.eval.j9_hooks import emit_recall_used

            await emit_recall_used(
                memory_mod._db,
                memory_ids=list(found_ids),
                source="memory_expand",
            )
        except Exception:
            logger.debug("eval: recall_used emit failed", exc_info=True)

    results = []
    blockable = 0
    for point in points:
        mid = str(point.id)
        payload = point.payload or {}
        _collection = point_collection.get(mid, "episodic_memory")

        d = {
            "memory_id": mid,
            "content": payload.get("content", ""),
            "source": payload.get("source", ""),
            "memory_type": payload.get("memory_type", "episodic"),
            "memory_class": payload.get("memory_class", "fact"),
            "wing": payload.get("wing", ""),
            "room": payload.get("room", ""),
            "confidence": payload.get("confidence"),
            "tags": payload.get("tags", []),
            "source_pipeline": payload.get("source_pipeline", ""),
            "source_session_id": payload.get("source_session_id"),
            "created_at": payload.get("created_at"),
            # Provenance (audit D12): first-party memory vs external-world KB.
            "collection": _collection,
            "provenance": provenance_descriptor(
                collection=_collection,
                source_pipeline=payload.get("source_pipeline"),
                source_doc=payload.get("source"),
            ),
        }

        # Injection defense (PR2): wrap full external-world content pulled into
        # context after a compact recall (the real full-payload surface).
        if is_external(_collection):
            d["content"] = wrap_external_recall(
                d["content"],
                source_pipeline=payload.get("source_pipeline"),
            )
            if immunity_shadow.item_is_blockable(
                collection=_collection,
                source_pipeline=payload.get("source_pipeline"),
                origin_class=payload.get("origin_class"),
            ):
                blockable += 1

        # Graph enrichment
        try:
            traversal = await graph_traverse(
                memory_mod._db,
                mid,
                max_depth=2,
                min_strength=0.3,
            )
            if traversal.nodes:
                d["graph_neighbors"] = [
                    {
                        "memory_id": n.memory_id,
                        "link_type": n.link_type,
                        "depth": n.depth,
                        "strength": n.strength,
                    }
                    for n in traversal.nodes[:5]
                ]
        except Exception:
            logger.warning("Graph enrichment failed for %s", mid, exc_info=True)

        results.append(d)

    # WS-3 B1 gate 4 (injection): shadow-record external content reaching this
    # expand prompt (observe-only). memory_mod._db is asserted non-None above.
    await immunity_shadow.record_would_block(
        gate="injection",
        source_kind="recall_inject",
        source_ref="mcp/memory/core.py::memory_expand",
        process="server",
        blockable_count=blockable,
        db=memory_mod._db,
    )

    if not_found or ambiguous:
        trailer = {"not_found": not_found}
        if ambiguous:
            trailer["ambiguous"] = ambiguous
        results.append(trailer)

    return results


@mcp.tool()
async def memory_store(
    content: str,
    source: str,
    memory_type: str = "episodic",
    tags: list[str] | None = None,
    confidence: float = 0.5,
    memory_class: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    collection: str | None = None,
    supersedes: str | None = None,
) -> str:
    """Store memory with source metadata and type tag. Returns memory_id.

    Args:
        memory_class: Optional classification — "rule", "fact", or "reference".
            Auto-classified from content if not provided.
        wing: Structural domain (auto-classified if not provided).
        room: Topic within the wing (auto-classified if not provided).
        collection: Explicit Qdrant collection override. Bypasses the default
            collection routing when provided (e.g. "knowledge_base").
        supersedes: Memory ID that this new memory replaces. The old memory
            will be marked as deprecated with a ``succeeded_by`` link to the
            new one. Use when correcting stale facts.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    # WS-3: dispatched sessions carry a session-level origin via env
    # (GENESIS_SESSION_ORIGIN, stamped by CCInvoker). Forward it so an
    # external-influenced session's writes stop landing first_party via the
    # "conversation" pipeline. None (foreground/unset) → pipeline-derived.
    from genesis.memory.provenance import session_origin_from_env

    return await memory_mod._store.store(
        content,
        source,
        memory_type=memory_type,
        tags=tags,
        confidence=confidence,
        memory_class=memory_class,
        source_pipeline="conversation",
        origin_class=session_origin_from_env(),
        wing=wing,
        room=room,
        collection=collection,
        supersedes=supersedes,
    )


@mcp.tool()
async def memory_extract(
    extractions: list[dict],
) -> list[str]:
    """Store fact/decision/entity extractions. Returns list of IDs."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    # WS-3: same session-origin forwarding as memory_store (see there).
    from genesis.memory.provenance import session_origin_from_env

    _origin = session_origin_from_env()
    ids: list[str] = []
    for item in extractions:
        mid = await memory_mod._store.store(
            content=item["content"],
            source=item.get("source", "extraction"),
            memory_type=item.get("type", "fact"),
            tags=item.get("tags"),
            confidence=item.get("confidence", 0.7),
            source_pipeline="harvest",
            origin_class=_origin,
        )
        ids.append(mid)
    return ids


@mcp.tool()
async def memory_proactive(
    current_message: str,
    limit: int = 5,
) -> list[dict]:
    """Cross-session context injection for prompts."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._retriever is not None
    # min_activation=0.0: use activation as a ranking signal, not a filter gate.
    # With confidence=0.5 (96% of memories) and retrieved_count=0 (80%),
    # even day-old memories fail a 0.3 threshold. Let RRF fusion rank instead.
    results = await memory_mod._retriever.recall(
        current_message, limit=limit * 2, min_activation=0.0, rerank=False
    )
    filtered = [r for r in results if "memory_operation" not in (r.payload.get("tags") or [])][
        :limit
    ]
    # Injection defense (PR2): recall defaults to source="both", so KB content
    # can appear here and flow straight into prompt context — wrap external-world
    # items so the model treats them as data, not first-party instructions.
    out: list[dict] = []
    blockable = 0
    _unsupervised = immunity_shadow.is_dispatched_session_env()
    for r in filtered:
        d = asdict(r)
        # WS-3 B4 gate-4 ENFORCE (pushed-surfaces cut): memory_proactive is a
        # query-less ambient feed — in a DISPATCHED session under enforce,
        # blockable stored-external items are DROPPED (not returned at all).
        # The emit below still records them: the enforce-mode row IS the
        # block ledger. Foreground and shadow mode keep wrap-and-return.
        if immunity_shadow.should_enforce_drop(
            gate="injection",
            collection=r.collection,
            source_pipeline=r.source_pipeline,
            origin_class=r.origin_class,
            pushed_surface=True,
            unsupervised=_unsupervised,
        ):
            blockable += 1
            continue
        if is_external(r.collection):
            d["content"] = wrap_external_recall(
                d.get("content", ""),
                source_pipeline=r.source_pipeline,
            )
            if immunity_shadow.item_is_blockable(
                collection=r.collection,
                source_pipeline=r.source_pipeline,
                origin_class=r.origin_class,
            ):
                blockable += 1
        out.append(d)
    # WS-3 B1 gate 4 (injection): shadow-record external content reaching this
    # proactive-recall prompt (observe-only). db=memory_mod._db when set, else
    # the emit self-resolves a short-lived connection.
    await immunity_shadow.record_would_block(
        gate="injection",
        source_kind="recall_inject",
        source_ref="mcp/memory/core.py::memory_proactive",
        process="server",
        blockable_count=blockable,
        db=memory_mod._db,
    )
    return out


@mcp.tool()
async def memory_core_facts(
    limit: int = 10,
) -> list[dict]:
    """Retrieve individual high-confidence memories ranked by activation score.

    Returns full memory content — different from essential knowledge
    auto-injection which provides aggregate system state briefing.
    Use when you need specific high-confidence facts, not the system overview.

    Queries Qdrant for memories with confidence >= 0.7, re-ranked by
    multi-factor activation score (recency, access frequency, connectivity).
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._qdrant is not None and memory_mod._db is not None

    # Query high-confidence memories across all wings
    # Use a broad query to get candidates, then re-rank by activation
    try:
        from qdrant_client.models import FieldCondition, Filter, Range

        points = memory_mod._qdrant.scroll(
            collection_name="episodic_memory",
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="confidence", range=Range(gte=0.7)),
                ]
            ),
            limit=limit * 3,
            with_payload=True,
        )[0]  # scroll returns (points, next_offset)
    except Exception:
        logger.warning("Qdrant scroll for core_facts failed", exc_info=True)
        return []

    now_str = datetime.now(UTC).isoformat()
    scored: list[tuple[dict, float]] = []
    for point in points:
        payload = point.payload or {}
        mid = str(point.id)
        link_count = await memory_mod.memory_links.count_links(memory_mod._db, mid)
        act = compute_activation(
            confidence=payload.get("confidence", 0.7),
            created_at=payload.get("created_at", now_str),
            retrieved_count=payload.get("retrieved_count", 0),
            link_count=link_count,
            source=payload.get("source", ""),
            now=now_str,
        )
        scored.append(
            (
                {
                    "memory_id": mid,
                    "content": payload.get("content", ""),
                    "source": payload.get("source", ""),
                    "memory_class": payload.get("memory_class", "fact"),
                    "wing": payload.get("wing", ""),
                    "room": payload.get("room", ""),
                    "confidence": payload.get("confidence"),
                    "activation_score": round(act.final_score, 3),
                    # WS-3 provenance — consumed by the injection-gate check
                    # below and honest output metadata for callers.
                    "source_pipeline": payload.get("source_pipeline"),
                    "origin_class": payload.get("origin_class"),
                },
                act.final_score,
            )
        )

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:limit]

    # WS-3 B4 gate 4 (injection): core_facts is a query-less AMBIENT surface
    # that scrolls episodic directly — it bypasses HybridRetriever, so the
    # .recall()-AST enumeration lock never saw it (gap caught during the B4
    # planning pass). Wrap external-origin items and shadow-record the
    # would-block (observe-only — the item still reaches the prompt; the
    # enforce drop for dispatched sessions is PR-3). Blockability keys on the
    # STORED origin_class: episodic content is only external when its stored
    # class says so — the collection fallback alone can never flag it here.
    blockable = 0
    _unsupervised = immunity_shadow.is_dispatched_session_env()
    _kept: list[tuple[dict, float]] = []
    for item, score in top:
        # WS-3 B4 gate-4 ENFORCE (pushed-surfaces cut): core_facts is a
        # query-less ambient feed — dispatched session + enforce -> DROP the
        # blockable item (still recorded below). Otherwise wrap-and-return.
        if immunity_shadow.should_enforce_drop(
            gate="injection",
            collection="episodic_memory",
            source_pipeline=item.get("source_pipeline"),
            origin_class=item.get("origin_class"),
            pushed_surface=True,
            unsupervised=_unsupervised,
        ):
            blockable += 1
            continue
        if immunity_shadow.item_is_blockable(
            collection="episodic_memory",
            source_pipeline=item.get("source_pipeline"),
            origin_class=item.get("origin_class"),
        ):
            item["content"] = wrap_external_recall(
                item["content"],
                source_pipeline=item.get("source_pipeline"),
            )
            blockable += 1
        _kept.append((item, score))
    top = _kept
    await immunity_shadow.record_would_block(
        gate="injection",
        source_kind="recall_inject",
        source_ref="mcp/memory/core.py::memory_core_facts",
        process="server",
        blockable_count=blockable,
        db=memory_mod._db,
    )

    # Track retrieval so activation scores reflect actual usage.
    # (Suppressed under the eval bench's frozen-snapshot mode — see
    # env.memory_writebacks_off; shared prod Qdrant must stay unmutated.)
    from genesis.env import memory_writebacks_off

    if top and not memory_writebacks_off():
        try:
            for item, _ in top:
                mid = item["memory_id"]
                pts = memory_mod._qdrant.retrieve(
                    collection_name="episodic_memory",
                    ids=[mid],
                    with_payload=True,
                )
                if pts:
                    old_count = (pts[0].payload or {}).get("retrieved_count", 0)
                    memory_mod._qdrant.set_payload(
                        collection_name="episodic_memory",
                        payload={"retrieved_count": old_count + 1},
                        points=[mid],
                    )
        except Exception:
            logger.debug("Failed to update retrieved_count for core_facts", exc_info=True)

    return [item for item, _ in top]


@mcp.tool()
async def memory_stats() -> dict:
    """Health, capacity, and structural metrics for the memory system."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    assert memory_mod._qdrant is not None

    episodic_info: dict | None = None
    knowledge_info: dict | None = None
    try:
        episodic_info = memory_mod.get_collection_info(memory_mod._qdrant, "episodic_memory")
    except Exception:
        logger.warning("Failed to query episodic_memory collection", exc_info=True)
    try:
        knowledge_info = memory_mod.get_collection_info(memory_mod._qdrant, "knowledge_base")
    except Exception:
        logger.warning("Failed to query knowledge_base collection", exc_info=True)

    pending_deltas = await memory_mod.observations.query(
        memory_mod._db, type="user_model_delta", resolved=False, limit=100000
    )

    total_links_rows = await memory_mod._db.execute_fetchall("SELECT COUNT(*) FROM memory_links")
    total_links_row = total_links_rows[0] if total_links_rows else None
    total_links = total_links_row[0] if total_links_row else 0

    # Structural data from memory_health snapshot queries
    wings = []
    classes = []
    extraction = {}
    code_index = {}
    ek_info = {}
    try:
        from genesis.observability.snapshots.memory_health import (
            _class_distribution,
            _code_index_stats,
            _essential_knowledge_stats,
            _extraction_coverage,
            _wing_distribution,
        )

        wings = await _wing_distribution(memory_mod._db)
        classes = await _class_distribution(memory_mod._db)
        extraction = await _extraction_coverage(memory_mod._db)
        code_index = await _code_index_stats(memory_mod._db)
        ek_info = _essential_knowledge_stats()
    except Exception:
        logger.debug("Structural stats unavailable", exc_info=True)

    return {
        "episodic_count": episodic_info.get("points_count", 0) if episodic_info else None,
        "knowledge_count": knowledge_info.get("points_count", 0) if knowledge_info else None,
        "pending_deltas": len(pending_deltas),
        "total_links": total_links,
        "wings": wings,
        "classes": classes,
        "extraction": extraction,
        "code_index": code_index,
        "essential_knowledge": ek_info,
    }


@mcp.tool()
async def memory_synthesize(
    content: str,
    source_memory_ids: list[str] | None = None,
    tags: list[str] | None = None,
    wing: str | None = None,
    room: str | None = None,
) -> str:
    """Store a synthesis — a conclusion derived from multiple recalled memories.

    Use this when you've combined information from multiple memories into a new
    insight worth preserving. The synthesis is stored with higher confidence
    (validated by use) and linked back to source memories.

    Args:
        content: The synthesized knowledge.
        source_memory_ids: IDs of memories that contributed to this synthesis.
        tags: Additional tags for the synthesis.
        wing: Structural domain (auto-classified if not provided).
        room: Topic within the wing (auto-classified if not provided).

    Returns:
        The memory_id of the stored synthesis.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None

    resolved_tags = list(tags or [])
    if "synthesis" not in resolved_tags:
        resolved_tags.append("synthesis")

    # WS-3: same session-origin forwarding as memory_store — a synthesis made
    # INSIDE an external-influenced session inherits that session's origin.
    from genesis.memory.provenance import session_origin_from_env

    memory_id = await memory_mod._store.store(
        content,
        source="synthesis",
        memory_type="episodic",
        tags=resolved_tags,
        confidence=0.8,  # Higher confidence — validated by cross-memory derivation
        source_pipeline="synthesis",
        origin_class=session_origin_from_env(),
        wing=wing,
        room=room,
    )

    # Create links back to source memories
    if source_memory_ids and memory_mod._store.linker:
        for source_id in source_memory_ids:
            try:
                await memory_mod._store.linker.create_typed_links(
                    memory_id,
                    [{"target": source_id, "type": "extends"}],
                )
            except Exception:
                logger.warning(
                    "Failed to link synthesis %s to source %s",
                    memory_id,
                    source_id,
                    exc_info=True,
                )

    return memory_id
