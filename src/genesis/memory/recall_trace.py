"""Durable per-recall retrieval trace for the proactive recall path.

``recall_fired`` (eval_events) records WHAT came back — the query, the top-5
PRE-rerank RRF scores and the first 10 ids. It cannot answer WHY: which
retriever found each candidate and at what rank, what the reranker made of it,
where it finished, and whether it was actually injected or dropped (and for
what reason). Graph-expansion events for this path were also written with no
recall id, so they could not be joined back.

This module is that trace. One ``recall_trace`` row per proactive recall in
``eval_events`` — the existing per-recall telemetry store, alongside its
siblings ``recall_fired`` / ``recall_diagnostics`` / ``graph_expansion_*``,
so no new store (New-Store Gate) and the existing ``(event_type, timestamp)``
index serves both reads and the retention prune.

Join keys (all the same value, ``trace_id``):
  * ``eval_events.id`` of the ``recall_trace`` row;
  * ``metrics_json.trace_id`` on the matching ``recall_fired`` row;
  * ``eval_events.subject_id`` on the matching ``graph_expansion_*`` row.

Hot-path contract: during the request the engine only drops REFERENCES and one
dict copy into a caller-owned ``trace`` sink (microseconds). Assembling the
per-candidate records and the DB write both happen in a background task after
the response is built — the request never waits on them.

Size: candidates are bounded BY MEANING, not by a blanket cap — the trace
window is everything that could influence the outcome: the rerank window
(top ``3 × recall limit`` by pre-rerank fused score, exactly the set
``_maybe_rerank`` scores), every returned result, every graph neighbor and
every candidate the proactive layer dropped. The rest of the fused pool
(activation-only tail) is counted in ``omitted``, never silently cut. On the
proactive path recall limit ≤ 16 (budget ≤ 8, ×2), so the window is ≤ ~60
records ≈ 10 KB.

Retention: ``RETENTION_DAYS`` via the daily learning-scheduler prune
(``runtime/init/learning.py``). Kill switch: ``proactive.trace: off`` in
``config/memory_recall.yaml`` (read live).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPE = "recall_trace"
SCHEMA_VERSION = 1
# 14 days: long enough to investigate a ranking complaint from last week, short
# enough to bound the store. At the measured ~225 proactive recalls/day
# (385 in ~41h) and ≤ ~10 KB/trace that is ≤ ~30 MB on this install.
RETENTION_DAYS = 14

# Ranked lists recall() fuses. The FINDERS (which retriever surfaced the id) are
# vector / fts / event; activation and intent are re-orderings of the whole
# candidate pool and are recorded as ranks, not as "found by".
_FINDER_LANES = ("vector", "fts", "event")
_ORDERING_LANES = ("activation", "intent")


def new_trace_id() -> str:
    """Same shape as ``j9_eval._new_id`` so the trace id IS its row id."""
    return uuid.uuid4().hex[:16]


def _rank_maps(lanes: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name, ids in lanes.items():
        ranks: dict[str, int] = {}
        for pos, mid in enumerate(ids, start=1):
            ranks.setdefault(mid, pos)
        out[name] = ranks
    return out


def _r(value: Any) -> Any:
    return round(value, 6) if isinstance(value, float) else value


def build_record(trace: dict[str, Any]) -> dict[str, Any]:
    """Assemble the persisted ``metrics_json`` from the raw sink (pure; off-path).

    Tolerates a PARTIAL sink — a cancelled (503) recall writes whatever stages
    completed, and the record says so via ``outcome`` and absent fields.
    """
    lanes: dict[str, list[str]] = trace.get("lanes") or {}
    fused: dict[str, float] = trace.get("fused") or {}
    rerank: dict[str, float] = trace.get("rerank") or {}
    post: dict[str, float] = trace.get("post_rerank") or {}
    results: list[tuple[str, float, float]] = trace.get("results") or []
    drops: dict[str, str] = trace.get("drops") or {}
    # (id, graph score) pairs for EVERY computed neighbor — in shadow mode too,
    # where none is injected. A bare id (older sink shape) carries no score.
    raw_neighbors = trace.get("graph_neighbors") or []
    neighbors: list[str] = []
    graph_score: dict[str, float] = {}
    for item in raw_neighbors:
        if isinstance(item, (list, tuple)):
            neighbors.append(item[0])
            graph_score[item[0]] = item[1]
        else:
            neighbors.append(item)
    delivered: list[str] = trace.get("delivered") or []
    delivered_rank = {mid: pos for pos, mid in enumerate(delivered, start=1)}
    recall_limit = trace.get("recall_limit")

    ranks = _rank_maps(lanes)
    final_rank = {mid: pos for pos, (mid, _rs, _s) in enumerate(results, start=1)}
    final_scores = {mid: (rs, s) for mid, rs, s in results}
    delivered_set = set(delivered)
    neighbor_set = set(neighbors)

    fused_order = sorted(fused, key=lambda m: fused[m], reverse=True)
    window_n = recall_limit * 3 if isinstance(recall_limit, int) else len(fused_order)
    window: list[str] = []
    seen: set[str] = set()
    for mid in [*fused_order[:window_n], *final_rank, *neighbors, *drops, *delivered]:
        if mid not in seen:
            seen.add(mid)
            window.append(mid)

    candidates = []
    for mid in window:
        found_by = {lane: ranks[lane][mid] for lane in _FINDER_LANES if mid in ranks.get(lane, {})}
        if mid in neighbor_set:
            found_by["graph"] = neighbors.index(mid) + 1
        rec: dict[str, Any] = {"id": mid, "found_by": found_by}
        for lane in _ORDERING_LANES:
            if mid in ranks.get(lane, {}):
                rec[f"{lane}_rank"] = ranks[lane][mid]
        if mid in fused:
            rec["fused"] = _r(fused[mid])
        if mid in rerank:
            rec["rerank"] = _r(rerank[mid])
        if mid in post:
            rec["post_rerank"] = _r(post[mid])
        if mid in final_rank:
            rec["final_rank"] = final_rank[mid]
            rec["final_score"] = _r(final_scores[mid][1])
        if mid in graph_score:
            # The score expansion ordered neighbors by — the only score a
            # graph-only candidate has (it never went through recall's fusion).
            rec["graph_score"] = _r(graph_score[mid])
        rec["injected"] = mid in delivered_set
        if mid in delivered_set:
            # Position in what was actually handed to the prompt — organic and
            # graph-injected alike.
            rec["delivered_rank"] = delivered_rank[mid]
            # Neighbors exclude the organic seeds (= everything delivered
            # organically), so a delivered neighbor can only have come via graph.
            rec["outcome"] = "injected_via_graph" if mid in neighbor_set else "injected"
        elif mid in drops:
            rec["outcome"] = f"dropped:{drops[mid]}"
        elif mid in neighbor_set:
            rec["outcome"] = "graph_not_injected"  # shadow mode, or no room left
        elif mid in final_rank:
            rec["outcome"] = "not_reached"  # returned by recall, past the budget
        else:
            rec["outcome"] = "not_returned"  # ranked/reranked/scoped out inside recall
        candidates.append(rec)

    reasons = list(trace.get("degraded_reasons") or [])
    return {
        "v": SCHEMA_VERSION,
        "trace_id": trace.get("trace_id"),
        "caller": trace.get("caller"),
        "profile": trace.get("profile"),
        "outcome": trace.get("outcome", "ok"),
        "stance": trace.get("stance"),
        "intent": trace.get("intent"),
        "budget": trace.get("budget"),
        "recall_limit": recall_limit,
        "embedding_available": trace.get("embedding_available"),
        "fts_query_rewritten": trace.get("fts_query_rewritten"),
        "file_lane_fallback_used": trace.get("file_lane_fallback_used"),
        # Pre-expiry hit counts per finder lane; with ``zero_hit`` this tells a
        # completed no-candidate recall apart from one that never got that far.
        "lane_hits": trace.get("lane_hits"),
        "zero_hit": bool(trace.get("zero_hit")),
        "graph_mode": trace.get("graph_mode"),
        "reranked": bool(rerank),
        "degraded": bool(reasons),
        "degraded_reasons": reasons,
        "timings_ms": trace.get("timings_ms") or {},
        "pool_size": len(fused),
        "window": len(candidates),
        "omitted": max(0, len(fused) - sum(1 for c in candidates if c["id"] in fused)),
        "delivered": delivered,
        "candidates": candidates,
    }


async def write_trace(db: Any, trace: dict[str, Any], *, session_id: str | None = None) -> None:
    """Build + persist one trace row. Best-effort: never raises (off-path)."""
    try:
        from genesis.db.crud import j9_eval

        record = build_record(trace)
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type=EVENT_TYPE,
            metrics=record,
            session_id=session_id or None,
            event_id=trace.get("trace_id"),
        )
    except Exception:
        logger.debug("recall trace write failed", exc_info=True)
