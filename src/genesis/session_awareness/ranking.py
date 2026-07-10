"""Candidate retrieval + ranking for the ambient worker.

Three lanes, unioned (all Qdrant lanes use **exact search** — this is
an offline worker, and filtered HNSW without payload indexes measurably
drops valid results; found 2026-07-09 hunting the OMI kill-criterion
memory):

- **Vector lane** — the theme EMA straight into Qdrant
  (``qdrant_ops.search``; deprecated points filtered by default).
- **Decisions lane** — the EMA against ``tags~decision`` memories only.
  This is the OMI-incident class: decisions/facts drown under walls of
  near-duplicate operational memories on the same topic (measured: the
  repo-split fact sat past rank 200 unfiltered, rank 12 in this lane).
- **Drift lane** — the entity ledger's top keywords as a query string
  through ``drift_recall`` (it has no vector entry point; it re-embeds
  internally and only READS retrieval bookkeeping).

Bitemporal parity with the main retrieval path via
``_expired_candidate_ids``. Rank mirrors the graph-boost shape from
``HybridRetriever._apply_graph_boost``:

    (1 + 0.05·log1p(inbound_links)) × confidence × CLASS_WEIGHT × cosine

The final candidate set is stratified: top-N overall by score, but at
least ``DECISION_RESERVED`` decision-lane candidates are always included
when available — otherwise the operational wall crowds out exactly the
memories this layer exists to surface.

**This lane writes NOTHING** — no retrieved_count bump, ever, even after
a live flip. Retrieval baselines (MEM-005 / H-1) must stay clean.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .accumulator import cosine

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.embeddings import EmbeddingProvider

VECTOR_LANE_LIMIT = 24
DECISION_LANE_LIMIT = 15
DRIFT_LANE_LIMIT = 10
TOP_N = 8
DECISION_RESERVED = 2  # decision-lane floor in the final candidate set
BACKLINK_COEF = 0.05  # mirrors retrieval._BACKLINK_BOOST_COEF
DEFAULT_CONFIDENCE = 0.5
PREVIEW_CHARS = 200


def _preview(content: str) -> str:
    return content[:PREVIEW_CHARS]


async def rank_candidates(
    *,
    ema: list[float],
    entity_query: str,
    db: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    embedding_provider: EmbeddingProvider,
    top_n: int = TOP_N,
    created_before: str | None = None,
) -> list[dict]:
    """Gather both lanes, filter expired, rank, return the top *top_n*.

    Each candidate: memory_id, score, cosine, confidence, memory_class,
    inbound_links, lanes, preview.
    """
    from genesis.db.crud.memory_links import batch_link_counts
    from genesis.memory.classification import CLASS_WEIGHTS
    from genesis.memory.drift import drift_recall
    from genesis.memory.retrieval import _expired_candidate_ids
    from genesis.qdrant import collections as qdrant_ops

    # ── Vector + decisions lanes (both exact) ────────────────────────
    by_id: dict[str, dict] = {}
    for lane, limit, tags in (
        ("vector", VECTOR_LANE_LIMIT, None),
        ("decision", DECISION_LANE_LIMIT, ["decision"]),
    ):
        for hit in qdrant_ops.search(
            qdrant_client,
            collection="episodic_memory",
            query_vector=ema,
            limit=limit,
            tags_any=tags,
            exact=True,
            created_before=created_before,
        ):
            mid = str(hit["id"])
            if mid in by_id:
                by_id[mid]["lanes"].append(lane)
                continue
            payload = hit.get("payload") or {}
            by_id[mid] = {
                "memory_id": mid,
                "cosine": float(hit.get("score", 0.0)),  # cosine metric: score IS cos
                "confidence": payload.get("confidence"),
                "memory_class": payload.get("memory_class"),
                "preview": _preview(payload.get("content", "")),
                "lanes": [lane],
            }

    # ── Drift lane ───────────────────────────────────────────────────
    if entity_query.strip():
        for r in await drift_recall(
            entity_query,
            db=db,
            qdrant_client=qdrant_client,
            embedding_provider=embedding_provider,
            source="episodic",
            limit=DRIFT_LANE_LIMIT,
        ):
            if r.memory_id in by_id:
                by_id[r.memory_id]["lanes"].append("drift")
                continue
            payload = r.payload or {}
            # drift_recall has no as-of entry point — post-filter (the
            # replay's fidelity concern; live runs pass no cutoff)
            if created_before and str(payload.get("created_at") or "") > created_before:
                continue
            by_id[r.memory_id] = {
                "memory_id": r.memory_id,
                "cosine": None,  # backfilled below from the stored vector
                "confidence": payload.get("confidence"),
                "memory_class": payload.get("memory_class"),
                "preview": _preview(r.content),
                "lanes": ["drift"],
            }

    if not by_id:
        return []

    # Drift-only candidates need their stored vector for cosine-vs-EMA.
    missing = [mid for mid, c in by_id.items() if c["cosine"] is None]
    if missing:
        try:
            points = qdrant_client.retrieve(
                collection_name="episodic_memory",
                ids=missing,
                with_payload=False,
                with_vectors=True,
            )
            for p in points:
                vec = p.vector if isinstance(p.vector, list) else None
                if vec:
                    by_id[str(p.id)]["cosine"] = cosine(ema, vec)
        except Exception:
            pass  # cosine stays None → scored at 0 below, not dropped info
        for mid in missing:
            if by_id[mid]["cosine"] is None:
                by_id[mid]["cosine"] = 0.0

    # ── Bitemporal parity ────────────────────────────────────────────
    expired = await _expired_candidate_ids(db, set(by_id))
    for mid in expired:
        by_id.pop(mid, None)
    if not by_id:
        return []

    # ── Rank ─────────────────────────────────────────────────────────
    link_counts = await batch_link_counts(db, list(by_id))
    for mid, cand in by_id.items():
        inbound = link_counts.get(mid, (0, 0))[1]
        conf_raw = cand.get("confidence")
        confidence = conf_raw if isinstance(conf_raw, int | float) else DEFAULT_CONFIDENCE
        class_w = CLASS_WEIGHTS.get(cand.get("memory_class") or "", 1.0)
        cand["inbound_links"] = inbound
        cand["confidence"] = confidence
        cand["score"] = (
            (1.0 + BACKLINK_COEF * math.log1p(inbound))
            * confidence
            * class_w
            * max(cand["cosine"] or 0.0, 0.0)
        )

    ranked = sorted(by_id.values(), key=lambda c: c["score"], reverse=True)
    picked = ranked[:top_n]

    # Stratified floor: guarantee decision-lane representation. Without
    # it the operational near-duplicate wall crowds out exactly the
    # class of memory this layer exists to surface (the OMI incident).
    decision_in = sum(1 for c in picked if "decision" in c["lanes"])
    if decision_in < DECISION_RESERVED:
        extras = [
            c for c in ranked[top_n:] if "decision" in c["lanes"]
        ][: DECISION_RESERVED - decision_in]
        if extras:
            keep = [c for c in picked if "decision" not in c["lanes"]]
            picked = (
                [c for c in picked if "decision" in c["lanes"]]
                + keep[: top_n - decision_in - len(extras)]
                + extras
            )
            picked.sort(key=lambda c: c["score"], reverse=True)

    return picked
