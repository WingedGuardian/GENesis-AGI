"""Dream-cycle importance shield.

Protects high-salience memories from being consolidated away by the dream merge
path. The weekly clusterer enqueues near-duplicate clusters for merge; without
a shield, a heavily-retrieved, high-confidence, or bridge-of-the-graph memory
is merged into a synthesis exactly like a throwaway one (its salience and access
history uncompensated). The shield removes such members from clusters BEFORE
they are enqueued (skip-member: the unshielded remainder still merges if ≥2
survive), and re-checks live at drain time to catch members whose salience rose
during the week.

Salience signals (any one shields a member):

- **activation** ≥ the collection's activation percentile — the production
  ``compute_activation`` score (confidence × recency × access/connectivity).
- **confidence** ≥ an absolute floor — catches rare-but-critical, rarely-
  retrieved memories that activation (an access-pattern proxy) would miss.
- **centrality** ≥ the nonzero betweenness percentile — bridge nodes that
  link-degree (already inside activation) cannot detect.

Thresholds are computed at enqueue over the true (non-deprecated) population and
FROZEN into each slice payload, so the daily drain re-checks against a stable
bar (a percentile edit takes effect next weekly run). The enable/kill-switch
state is read LIVE at drain, so an operator can disable mid-week.

Pure-compute (one ``all_link_counts`` aggregate + per-member ``compute_activation``);
no LLM calls, no mutations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import memory_links
from genesis.memory import dream_shield_config
from genesis.memory.activation import compute_activation

logger = logging.getLogger(__name__)


def _percentile(values: list[float], p: float) -> float | None:
    """Value at percentile ``p`` (0..1) via nearest-rank on ascending order.

    Returns None for an empty input. ``p`` is the fraction BELOW the returned
    value, so ``p=0.9`` yields a threshold roughly the top-decile floor:
    members ``>=`` it are ~the top 10%. ``p=0.0`` returns the minimum (shields
    everything ``>=`` min, i.e. all).
    """
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(p * len(ordered)))
    return ordered[idx]


@dataclass(frozen=True)
class ShieldState:
    """Frozen snapshot of the shield's thresholds + per-member scores.

    ``activation_threshold`` is None only when the population is empty;
    ``centrality_threshold`` is None when the centrality cache holds no nonzero
    rows (fresh install) — in both cases that signal simply doesn't fire.
    """

    activation_threshold: float | None
    centrality_threshold: float | None
    confidence_floor: float
    activation_by_id: dict[str, float] = field(default_factory=dict)
    centrality_by_id: dict[str, float] = field(default_factory=dict)
    population: int = 0


def _safe_confidence(payload: dict) -> float:
    """Payload confidence coerced to a float — None/non-numeric → 0.5.

    A present-but-null ``confidence`` makes ``payload.get("confidence", 0.5)``
    return ``None`` (the default applies only to ABSENT keys), and ``None >=
    float`` raises ``TypeError``. One such payload must not crash the floor
    comparison (weekly: disables the shield for the worklist; drain: propagates
    after mark_processing and stalls the shadow drain).
    """
    v = payload.get("confidence", 0.5)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.5
    return float(v)


def _activation_for(point: dict, link_count: int, now: str) -> float:
    """Production activation for one point, mirroring HybridRetriever's field
    extraction (retrieval.py ``_compute_activations``). Missing ``created_at``
    falls back to ``now`` (recency 1.0) — conservative (over-shields).

    Returns 0.0 on any compute error (e.g. a malformed timestamp): one bad
    payload among tens of thousands must NEVER crash the whole shield. A 0.0
    activation simply means "not shielded by the activation signal" — the
    confidence floor and centrality still apply to that member.
    """
    payload = point.get("payload", {})
    try:
        return compute_activation(
            confidence=_safe_confidence(payload),
            created_at=payload.get("created_at") or now,
            retrieved_count=payload.get("retrieved_count", 0),
            link_count=link_count,
            source=payload.get("source", ""),
            tags=payload.get("tags") or [],
            now=now,
            memory_class=payload.get("memory_class", "fact"),
            last_retrieved_at=payload.get("last_retrieved_at"),
        ).final_score
    except Exception:
        logger.debug(
            "Shield: activation compute failed for %s — treating as 0.0",
            point.get("id"),
            exc_info=True,
        )
        return 0.0


async def _centrality_map(
    db: aiosqlite.Connection, memory_ids: list[str] | None = None
) -> dict[str, float]:
    """Load betweenness scores from centrality_cache. Whole cache when
    ``memory_ids`` is None (enqueue-time percentile population); a specific
    id-set at drain time."""
    if memory_ids is None:
        rows = await db.execute_fetchall("SELECT memory_id, centrality_score FROM centrality_cache")
        return {r[0]: r[1] for r in rows}
    if not memory_ids:
        return {}
    _CHUNK = 800
    out: dict[str, float] = {}
    for off in range(0, len(memory_ids), _CHUNK):
        chunk = memory_ids[off : off + _CHUNK]
        ph = ",".join("?" * len(chunk))
        rows = await db.execute_fetchall(
            f"SELECT memory_id, centrality_score FROM centrality_cache "  # noqa: S608 - placeholders bound
            f"WHERE memory_id IN ({ph})",
            chunk,
        )
        for r in rows:
            out[r[0]] = r[1]
    return out


async def compute_shield_state(
    db: aiosqlite.Connection,
    points: list[dict],
    *,
    now: str | None = None,
) -> ShieldState | None:
    """Compute enqueue-time thresholds + per-member scores over the full
    (non-deprecated) collection. Returns None when the shield is disabled.

    ``points`` are ``{"id", "payload": {...}}`` dicts (the flattened dream
    buckets). Uses one ``all_link_counts`` aggregate for connectivity.
    """
    if not dream_shield_config.shield_enabled():
        return None

    cfg = dream_shield_config.load_config()
    act_pct = dream_shield_config.knob_float01(cfg, "activation_percentile")
    cent_pct = dream_shield_config.knob_float01(cfg, "centrality_percentile")
    conf_floor = dream_shield_config.knob_float01(cfg, "confidence_floor")
    now_str = now or datetime.now(UTC).isoformat()

    link_counts = await memory_links.all_link_counts(db)
    activation_by_id: dict[str, float] = {}
    for pt in points:
        mid = pt["id"]
        activation_by_id[mid] = _activation_for(pt, link_counts.get(mid, 0), now_str)

    activation_threshold = _percentile(list(activation_by_id.values()), act_pct)

    # Centrality percentile over the LIVE population only. NOTE (2026-09-06):
    # centrality_cache is no longer built from the full memory_links graph —
    # the graph loader now applies recall's visibility predicate, so deprecated
    # and bitemporally-expired nodes are gone BEFORE betweenness runs. The
    # original reason for this restriction (stale high-centrality deprecated
    # rows inflating the bar) is therefore largely handled upstream. The
    # restriction still stands on its own: activation uses only these live
    # points, so the two populations must match, and a memory can be outside
    # this dream slice without being hidden from recall.
    point_ids = {pt["id"] for pt in points}
    full_centrality = await _centrality_map(db)
    centrality_by_id = {mid: v for mid, v in full_centrality.items() if mid in point_ids}
    nonzero = [v for v in centrality_by_id.values() if v > 0.0]
    centrality_threshold = _percentile(nonzero, cent_pct) if nonzero else None

    return ShieldState(
        activation_threshold=activation_threshold,
        centrality_threshold=centrality_threshold,
        confidence_floor=conf_floor,
        activation_by_id=activation_by_id,
        centrality_by_id=centrality_by_id,
        population=len(points),
    )


def _shielded(
    activation: float,
    confidence: float,
    centrality: float,
    *,
    act_thr: float | None,
    cent_thr: float | None,
    conf_floor: float,
) -> bool:
    """Shared predicate: any salience signal at/above its bar shields."""
    if act_thr is not None and activation >= act_thr:
        return True
    if confidence >= conf_floor:
        return True
    return cent_thr is not None and centrality >= cent_thr


def _member_is_shielded(member: dict, state: ShieldState) -> bool:
    """Enqueue-time predicate — scores come from the precomputed state maps."""
    mid = member["id"]
    return _shielded(
        state.activation_by_id.get(mid, 0.0),
        _safe_confidence(member.get("payload", {})),
        state.centrality_by_id.get(mid, 0.0),
        act_thr=state.activation_threshold,
        cent_thr=state.centrality_threshold,
        conf_floor=state.confidence_floor,
    )


def apply_shield_to_clusters(
    clusters: list[list[dict]],
    state: ShieldState | None,
) -> tuple[list[list[dict]], dict[str, int]]:
    """Remove shielded members from each cluster (skip-member).

    A cluster keeps its unshielded members if ≥2 survive; otherwise it is
    dropped entirely (a 1-member cluster is not mergeable). Returns the
    surviving clusters and stats. ``state=None`` (shield disabled) is a no-op.
    """
    stats = {"members_shielded": 0, "clusters_trimmed": 0, "clusters_dropped": 0}
    if state is None:
        return clusters, stats

    out: list[list[dict]] = []
    for cluster in clusters:
        survivors = [m for m in cluster if not _member_is_shielded(m, state)]
        removed = len(cluster) - len(survivors)
        stats["members_shielded"] += removed
        if len(survivors) < 2:
            if removed:
                stats["clusters_dropped"] += 1
            else:
                out.append(cluster)  # untouched (already sub-2 upstream — keep as-is)
            continue
        if removed:
            stats["clusters_trimmed"] += 1
        out.append(survivors)
    return out, stats


async def shield_filter_live(
    db: aiosqlite.Connection,
    cluster: list[dict],
    *,
    activation_threshold: float | None,
    centrality_threshold: float | None,
    now: str | None = None,
) -> tuple[list[dict], int]:
    """Drain-time re-check on LIVE payloads against the FROZEN slice thresholds.

    Catches members whose salience rose during the drain week (e.g.
    retrieved_count climbed). Reads the confidence floor and enable state LIVE
    from config. Returns (survivors, shielded_count). No-op when disabled.
    """
    if not dream_shield_config.shield_enabled():
        return cluster, 0

    conf_floor = dream_shield_config.knob_float01(
        dream_shield_config.load_config(), "confidence_floor"
    )
    now_str = now or datetime.now(UTC).isoformat()

    ids = [m["id"] for m in cluster]
    link_counts = await memory_links.batch_link_counts(db, ids)
    cent_map = await _centrality_map(db, ids) if centrality_threshold is not None else {}

    survivors: list[dict] = []
    shielded = 0
    for m in cluster:
        mid = m["id"]
        act = _activation_for(m, link_counts.get(mid, (0, 0))[0], now_str)
        if _shielded(
            act,
            _safe_confidence(m.get("payload", {})),
            cent_map.get(mid, 0.0),
            act_thr=activation_threshold,
            cent_thr=centrality_threshold,
            conf_floor=conf_floor,
        ):
            shielded += 1
        else:
            survivors.append(m)
    return survivors, shielded
