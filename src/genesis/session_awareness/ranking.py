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
- **Entity lane (E4, shadow-first)** — ledger keywords resolved to
  entity nodes (ledger keys == norm_names by construction), expanded
  ≤2 typed hops through ``entity_links``, then their ``entity_mentions``
  become candidates. This is the categorical-inference lane: a single
  "OMI" mention reaches the repo-split decision via
  OMI →is_a→ voice-edge-device →constrained_by→ rule. Gated by
  ``ENTITY_LANE_MODE`` (see below).

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

# ── Entity lane (E4) ─────────────────────────────────────────────────
# "shadow": lane computed and reported via ``entity_shadow_out``, but
# entity-only candidates never enter the returned set and existing
# candidates' ``lanes`` stay untouched (the arbiter prompt renders
# lanes — shadow must not perturb judgments; entity info rides in the
# separate ``entity_path`` key the arbiter never reads).
# "live": entity candidates rank normally with a reserved floor.
# "off": lane skipped entirely.
# The E4b flip is this ONE constant → "live", gated on the OMI replay.
ENTITY_LANE_MODE = "live"
ENTITY_RESERVED = 2  # entity-lane floor (live mode), mirrors DECISION_RESERVED
ENTITY_DEPTH_DECAY = 0.7  # per-hop decay on top of edge confidences
ENTITY_MENTIONS_PER_ENTITY = 10
ENTITY_LANE_LIMIT = 15  # max entity-lane candidates considered


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
    entity_lane: str | None = None,
    entity_shadow_out: list | None = None,
) -> list[dict]:
    """Gather the lanes, filter expired, rank, return the top *top_n*.

    Each candidate: memory_id, score, cosine, confidence, memory_class,
    inbound_links, lanes, preview (+ entity_path when the entity lane
    reached it). *entity_lane* overrides ``ENTITY_LANE_MODE``; in shadow
    mode entity-only candidates are appended to *entity_shadow_out*
    (when given) instead of the returned set.
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

    # ── Entity lane (E4) ─────────────────────────────────────────────
    # Ledger keywords ARE norm_names by construction (both normalize via
    # entity_resolution.normalize_content) — resolution doubles as the
    # noise filter: STT filler words resolve to nothing and drop out.
    mode = entity_lane or ENTITY_LANE_MODE
    if mode in ("shadow", "live"):
        try:
            from genesis.db.crud import entities as entities_crud
            from genesis.db.crud.entities import PROVENANCE_WEIGHTS

            weights: dict[str, float] = {}
            for keyword in entity_query.split():
                ent = await entities_crud.get_by_norm_name(
                    db, norm_name=keyword,
                )
                if ent and ent.get("status") == "active":
                    weights[ent["entity_id"]] = 1.0
            if weights:
                reached = await entities_crud.connected_entities(
                    db, list(weights), max_depth=2, as_of=created_before,
                )
                for eid, info in reached.items():
                    weights[eid] = info["path_confidence"] * (
                        ENTITY_DEPTH_DECAY ** info["depth"]
                    )
                mentions = await entities_crud.memories_mentioning(
                    db, list(weights),
                    limit_per_entity=ENTITY_MENTIONS_PER_ENTITY,
                )
                entity_meta: dict[str, dict] = {}
                for m in mentions:
                    path_score = (
                        weights[m["entity_id"]]
                        * m["confidence"]
                        * PROVENANCE_WEIGHTS.get(m["provenance"], 0.5)
                    )
                    prev = entity_meta.get(m["memory_id"])
                    if prev is None or path_score > prev["path_score"]:
                        entity_meta[m["memory_id"]] = {
                            "path_score": path_score,
                            "via_entity": m["entity_id"],
                            "provenance": m["provenance"],
                        }
                ranked_hits = sorted(
                    entity_meta.items(),
                    key=lambda kv: kv[1]["path_score"],
                    reverse=True,
                )[:ENTITY_LANE_LIMIT]
                for mid, meta in ranked_hits:
                    if mid in by_id:
                        by_id[mid]["entity_path"] = meta
                        if mode == "live":
                            by_id[mid]["lanes"].append("entity")
                    else:
                        by_id[mid] = {
                            "memory_id": mid,
                            "cosine": None,  # backfilled below
                            "confidence": None,
                            "memory_class": None,
                            "preview": "",
                            "lanes": ["entity"],
                            "entity_path": meta,
                            "_shadow_only": mode == "shadow",
                        }
        except Exception:
            pass  # additive lane — never breaks the worker

    if not by_id:
        return []

    # Drift/entity-only candidates need vector (cosine-vs-EMA) + payload.
    missing = [mid for mid, c in by_id.items() if c["cosine"] is None]
    if missing:
        try:
            points = qdrant_client.retrieve(
                collection_name="episodic_memory",
                ids=missing,
                with_payload=True,
                with_vectors=True,
            )
            for p in points:
                cand = by_id.get(str(p.id))
                if cand is None:
                    continue
                vec = p.vector if isinstance(p.vector, list) else None
                if vec:
                    cand["cosine"] = cosine(ema, vec)
                payload = p.payload or {}
                if not cand["preview"]:
                    cand["preview"] = _preview(payload.get("content", ""))
                if cand["confidence"] is None:
                    cand["confidence"] = payload.get("confidence")
                if cand["memory_class"] is None:
                    cand["memory_class"] = payload.get("memory_class")
                cand.setdefault("created_at", payload.get("created_at"))
        except Exception:
            pass  # cosine stays None → scored at 0 below, not dropped info
        for mid in missing:
            if by_id[mid]["cosine"] is None:
                by_id[mid]["cosine"] = 0.0

    # As-of parity for entity-only candidates: mentions don't carry the
    # memory's created_at, so the cutoff applies via the payload fetched
    # above (other lanes were already cutoff-filtered at search time).
    if created_before:
        for mid in [
            m for m, c in by_id.items()
            if c["lanes"] == ["entity"]
            and str(c.get("created_at") or "") > created_before
        ]:
            by_id.pop(mid)

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

    # Shadow-only entity candidates never enter the live ranking; they
    # are reported through entity_shadow_out for telemetry/replay.
    shadow_only = [c for c in by_id.values() if c.pop("_shadow_only", False)]
    live_pool = [c for c in by_id.values() if c not in shadow_only]

    ranked = sorted(live_pool, key=lambda c: c["score"], reverse=True)
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

    # Entity floor (live mode only) — same crowd-out logic as decisions.
    if mode == "live":
        entity_in = sum(1 for c in picked if "entity" in c["lanes"])
        if entity_in < ENTITY_RESERVED:
            extras = [
                c for c in ranked[top_n:]
                if "entity" in c["lanes"] and c not in picked
            ][: ENTITY_RESERVED - entity_in]
            if extras:
                protected = [
                    c for c in picked
                    if "entity" in c["lanes"] or "decision" in c["lanes"]
                ]
                fill = [c for c in picked if c not in protected]
                picked = (
                    protected
                    + fill[: max(0, top_n - len(protected) - len(extras))]
                    + extras
                )
                picked.sort(key=lambda c: c["score"], reverse=True)

    # Shadow report: what the entity lane WOULD have contributed.
    if entity_shadow_out is not None and mode == "shadow":
        report = shadow_only + [
            c for c in live_pool if "entity_path" in c
        ]
        report.sort(
            key=lambda c: c["entity_path"]["path_score"], reverse=True,
        )
        entity_shadow_out.extend(
            {
                "memory_id": c["memory_id"],
                "score": c["score"],
                "entity_path": c["entity_path"],
                "already_candidate": c not in shadow_only,
                "preview": c["preview"],
            }
            for c in report
        )

    return picked
