"""Recall-time 1-hop graph expansion over ``memory_links`` — shadow-first.

The A4 LongMemEval sprint proved that appending 1-hop linked neighbors to the
recalled top-K is worth +12.6pp overall (temporal +23pp, multi-session +17pp)
on the eval harness (memo: 2026-07-14 graph-DB decision). This module is the
production home of that mechanism, consumed by the MCP recall wrappers
(``genesis.mcp.memory.core``) — deliberately NOT a ``HybridRetriever.recall()``
kwarg, so:

- the ~11 direct ``.recall()`` callers (ego, voice, executor, CRAG…) are
  untouched — several post-filter results and must not receive neighbors;
- CRAG's corrective re-recall can never recurse through expansion;
- neighbors get no write-back/J-9 retrieval credit (they are linkage-reached,
  not query-relevant) — the merge happens after the retriever returns.

Three layers:

- config — ``DEFAULTS`` ← ``config/memory_recall.yaml`` ←
  ``~/.genesis/config/memory_recall.local.yaml``, re-read on EVERY call
  (``ws3_immunity`` pattern: no boot cache, a ``settings_update`` takes
  effect on the next recall in the running process). ``mode: off`` is the
  kill switch; there is no auto-demote — expansion never blocks anything.
- :func:`expand_neighbors` — the mode-independent primitive (also the
  LongMemEval graph arm's expansion, so the benchmark measures shipped prod
  code). Reads NO config; callers pass caps/exclusions explicitly.
- :func:`maybe_expand` — the surface wrapper: ``off`` passthrough, ``shadow``
  compute + emit metrics but return results unchanged, ``live`` append.
  Best-effort throughout — an expansion or metric failure never breaks
  recall.

Failure posture mirrors ``security/immunity.py``: missing/corrupt config
degrades layer-by-layer to DEFAULTS (mode ``shadow`` — observable, never
behavior-changing); an invalid mode VALUE degrades to ``shadow`` with a
warning, never silently ``live``.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import TYPE_CHECKING, Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.db.crud import j9_eval
from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links as memory_links_crud
from genesis.env import repo_root
from genesis.memory.types import RetrievalResult

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import aiosqlite

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "live")

_CONFIG_NAME = "memory_recall.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "graph_expansion": {
        "mode": "shadow",
        # eval parity: k=10 expansion won the +12.6pp
        "max_neighbors": 10,
        # proactive injection is token-budgeted — tiny cap
        "proactive_max_neighbors": 2,
        # never follow adversarial edges into LLM-visible context
        "exclude_link_types": ["contradicts"],
    },
    # PR-2 (entity lane in hybrid recall) reads this section; inert until then.
    "entity_lane": {
        "mode": "off",
    },
}

# Neighbor ordering score: organic RRF-fused scores are O(0.01)+ and rerank
# scores larger still, so 0.01 * strength (strength ∈ (0, 1]) sorts every
# neighbor strictly after every organic result while preserving
# strength order among neighbors.
_NEIGHBOR_SCORE_SCALE = 0.01


def _base_path():
    return repo_root() / "config" / _CONFIG_NAME


def load_recall_config() -> dict[str, Any]:
    """Read the merged memory_recall config fresh — per call, NO cache.

    Deep-merges (defaults ← base yaml ← .local.yaml overlay). Missing or
    corrupt files degrade layer-by-layer toward DEFAULTS.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        # The base file ships in-repo; its absence (tests, trimmed installs)
        # is normal — DEFAULTS are the same values.
        logger.debug("memory_recall base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("memory_recall overlay merge failed", exc_info=True)
    for key, value in base.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _mode_from(cfg: dict[str, Any]) -> str:
    if not cfg.get("enabled", True):
        return "off"
    section = cfg.get("graph_expansion")
    mode = section.get("mode") if isinstance(section, dict) else None
    if mode is False:
        # Hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        mode = "off"
    if mode not in MODES:
        logger.warning(
            "memory_recall graph_expansion has invalid mode %r — degrading to shadow",
            mode,
        )
        return "shadow"
    return mode


def expansion_mode() -> str:
    """Effective graph-expansion mode: ``off | shadow | live`` — read live."""
    return _mode_from(load_recall_config())


async def expand_neighbors(
    db: aiosqlite.Connection,
    seed_ids: Sequence[str],
    *,
    cap: int,
    exclude_ids: Iterable[str] = (),
    exclude_link_types: Sequence[str] = (),
) -> list[RetrievalResult]:
    """1-hop linked neighbors of *seed_ids* as ``RetrievalResult`` objects.

    Mode-independent primitive — reads NO config (the LongMemEval graph arm
    calls it directly; :func:`maybe_expand` passes prod config values).

    Provenance is STORED-FIRST and never synthetic: ``collection`` comes from
    the FTS row, ``origin_class`` from the batched ``memory_metadata`` lookup
    (0054-backfilled; ``None`` for a hypothetical unbackfilled row keeps the
    gate's fail-closed normalization honest), and ``source_pipeline`` stays
    ``None`` — it has no SQLite column and must not be fabricated, because
    ``item_is_blockable`` re-derives from these exact fields when the stored
    class is absent.

    Neighbors score ``0.01 * strength`` (sorts after any organic result) and
    carry ``payload["graph_expansion"] = {"linked_from": [...], "strength": s}``
    plus the FTS ``tags`` string (surfaces apply their own tag filters).
    Dangling links (edge rows whose neighbor no longer resolves) are skipped.
    """
    if not seed_ids or cap <= 0:
        return []
    neighbors = await memory_links_crud.neighbors_of(
        db,
        list(seed_ids),
        exclude=list(exclude_ids),
        limit=cap,
        exclude_link_types=tuple(exclude_link_types),
    )
    if not neighbors:
        return []

    neighbor_ids = [n["memory_id"] for n in neighbors]
    origins = await memory_crud.origin_class_by_ids(db, neighbor_ids)
    # Which seed(s) reach each neighbor — one bounded query over the
    # seed∪neighbor id set (direction-agnostic pair list).
    seed_set = set(seed_ids)
    linked_from: dict[str, set[str]] = {}
    for src, tgt in await memory_links_crud.inter_candidate_links(
        db,
        [*seed_ids, *neighbor_ids],
    ):
        if src in seed_set and tgt not in seed_set:
            linked_from.setdefault(tgt, set()).add(src)
        elif tgt in seed_set and src not in seed_set:
            linked_from.setdefault(src, set()).add(tgt)

    results: list[RetrievalResult] = []
    for n in neighbors:
        mid = n["memory_id"]
        row = await memory_crud.get_by_id(db, mid)
        if not row or not row.get("content"):
            continue  # dangling link — edge outlived the memory
        strength = float(n.get("strength") or 0.0)
        collection = row.get("collection") or "episodic_memory"
        results.append(
            RetrievalResult(
                memory_id=mid,
                content=row["content"],
                source=row.get("source_type") or "memory",
                memory_type=collection,
                score=_NEIGHBOR_SCORE_SCALE * strength,
                vector_rank=None,
                fts_rank=None,
                activation_score=0.0,
                payload={
                    "tags": row.get("tags") or "",
                    "graph_expansion": {
                        "linked_from": sorted(linked_from.get(mid, ())),
                        "strength": strength,
                    },
                },
                source_pipeline=None,
                origin_class=origins.get(mid),
                collection=collection,
            ),
        )
    return results


async def maybe_expand(
    db: aiosqlite.Connection,
    results: list[RetrievalResult],
    *,
    surface: str,
    recall_event_id: str | None = None,
) -> list[RetrievalResult]:
    """Apply configured graph expansion to a recall surface's results.

    ``off`` → passthrough. ``shadow`` → compute + emit metrics, return
    *results* unchanged. ``live`` → return *results* + neighbors (neighbors
    sort after organic results by score; dedup vs results is by construction —
    seeds are excluded in ``neighbors_of``).

    *surface* ∈ ``compact | full | proactive`` selects the neighbor cap
    (``proactive_max_neighbors`` vs ``max_neighbors``). Best-effort: any
    failure logs and returns *results* unchanged — expansion must never
    break recall.
    """
    if not results:
        return results
    cfg = load_recall_config()
    mode = _mode_from(cfg)
    if mode == "off":
        return results
    t0 = time.monotonic()
    try:
        # Config value extraction stays INSIDE the guard: the overlay is a
        # hand-editable file (settings_update validates, a text editor does
        # not) — a malformed cap or exclude list must degrade like any other
        # expansion failure, never crash a recall surface.
        section = cfg.get("graph_expansion")
        if not isinstance(section, dict):
            section = DEFAULTS["graph_expansion"]
        cap_key = "proactive_max_neighbors" if surface == "proactive" else "max_neighbors"
        cap = section.get(cap_key, DEFAULTS["graph_expansion"][cap_key])
        exclude_types = tuple(section.get("exclude_link_types") or ())
        seed_ids = [r.memory_id for r in results]
        neighbors = await expand_neighbors(
            db,
            seed_ids,
            cap=cap,
            exclude_ids=seed_ids,
            exclude_link_types=exclude_types,
        )
    except Exception:
        logger.warning(
            "graph expansion failed (surface=%s) — recall unaffected",
            surface,
            exc_info=True,
        )
        return results
    latency_ms = int((time.monotonic() - t0) * 1000)

    try:
        await j9_eval.insert_event(
            db,
            dimension="memory",
            event_type=f"graph_expansion_{mode}",
            subject_id=recall_event_id,
            metrics={
                "surface": surface,
                "seed_count": len(seed_ids),
                "neighbors_returned": len(neighbors),
                "neighbor_ids": [n.memory_id for n in neighbors[:10]],
                "latency_ms": latency_ms,
            },
        )
    except Exception:
        logger.warning("graph expansion metric emit failed", exc_info=True)
    logger.info(
        "graph_expansion mode=%s surface=%s seeds=%d neighbors=%d latency_ms=%d",
        mode,
        surface,
        len(results),
        len(neighbors),
        latency_ms,
    )

    if mode == "live" and neighbors:
        return [*results, *neighbors]
    return results
