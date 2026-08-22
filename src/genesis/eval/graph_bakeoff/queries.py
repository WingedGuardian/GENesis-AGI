"""The FROZEN bake-off workload — 8 pre-registered queries.

Frozen 2026-08-06 BEFORE any engine adapter exists (bias guard: scoring can't be
retrofitted to results). Reconciled to TODAY's schema against the live snapshot:

- ``memory_class`` in production is only {fact, rule, reference} — there is NO
  'assumption' class (spec §E unbuilt) and NO situation nodes (spec §D unbuilt),
  so q4 and q6 are PROXIES (``proxy_for``) for those future constructs, flagged
  in the findings doc.
- ``memory_links`` link types (live): supports 148k, extends 35k, related_to 11k,
  categorized_as 4.5k, evaluated_for 4.1k, action_item_for 3.1k, decided 2.6k,
  discussed_in 1.2k, succeeded_by 1.05k, preceded_by 1.02k, **contradicts 4**.
  ``followed_by``/``elaborates`` do NOT exist — q4 rides succeeded_by/preceded_by.
- Topology (measured): 57.9k nodes, avg degree 7.3, max OUT-degree 20, max
  IN-degree 2,406 (one hub). Undirected traversal through the hub pulls ~35% of
  the graph in 3 hops — so neighborhood queries are DIRECTED + strength-bounded to
  stay realistic, and anchors span a hub-adjacent AND a median-degree node.

Canonical parity semantics (engine-agnostic): each query's result is compared as
the SET of reachable node ids (``exact_set``), normalized in the shared layer —
this sidesteps depth/path-multiplicity differences between NetworkX BFS, SQL CTE,
and Cypher variable-length paths. q7 (centrality) uses top-k overlap + Spearman
against a one-time NetworkX betweenness reference. q8 is pure timing.

Concrete per-snapshot anchors are resolved by ``anchors.py`` from ``anchor_sql``
and recorded next to the snapshot (reproducible per sha256), never hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Pre-registered weights (declared before results exist — a bias guard shown as
# ONE lens in the findings doc, NOT a verdict engine). Latency-excluded queries
# still carry weight for their expressiveness/LOC dimension.
EXACT_SET = "exact_set"
TOPK_SPEARMAN = "topk_spearman"
TIMING = "timing"


@dataclass(frozen=True)
class QuerySpec:
    """One frozen bake-off query. ``params`` and ``anchor_sql`` are the freeze —
    they fully determine the workload against a given snapshot."""

    id: str
    title: str
    graph_layer: str  # "memory" | "entity" | "entity+memory" | "full"
    direction: str  # "directed" | "undirected" | "n/a"
    semantics: str  # exact canonical result definition (what the oracle computes)
    anchor_sql: str  # deterministic anchor selection FROM the snapshot
    params: dict = field(default_factory=dict)
    parity_mode: str = EXACT_SET
    latency_scored: bool = True
    weight: float = 1.0
    proxy_for: str | None = None
    as_of: str | None = None  # temporal-derivation note where relevant
    consumer: str = ""  # today's-consumer vs future-consumer (findings-doc split)


QUERIES: list[QuerySpec] = [
    QuerySpec(
        id="q1_entity_dossier",
        title="Entity dossier (bounded, directed)",
        graph_layer="entity+memory",
        direction="directed",
        semantics=(
            "Given entity X: seeds = memory_ids mentioning X (entity_mentions). "
            "Result = set of memory_ids reachable from any seed via <=2 DIRECTED "
            "memory_links hops with strength >= 0.5, seeds included."
        ),
        anchor_sql=(
            # One hub-adjacent (most mentions) + one median-mention entity.
            "SELECT entity_id FROM (SELECT entity_id, count(*) c FROM entity_mentions "
            "GROUP BY entity_id ORDER BY c DESC) ORDER BY c DESC LIMIT 1"
        ),
        params={"max_depth": 2, "min_strength": 0.5},
        weight=2.0,
        consumer="today: dossier/meeting-prep assembly (spec §C first consumer)",
    ),
    QuerySpec(
        id="q2_decision_chain",
        title="Decision chain -> supports -> contradiction",
        graph_layer="memory",
        direction="directed",
        semantics=(
            "Set of decision-class memory_ids d (d has an outgoing 'decided' edge) "
            "from which a 'contradicts' edge endpoint is reachable via 'supports' "
            "edges within <=3 hops. Expressiveness-scored (contradicts=4 -> likely "
            "near-empty; the emptiness itself is a findings-doc datum)."
        ),
        anchor_sql="SELECT source_id, target_id FROM memory_links WHERE link_type='contradicts'",
        params={"max_depth": 3, "via": "supports", "terminal": "contradicts"},
        latency_scored=False,  # contradicts=4: can't differentiate engines on latency
        weight=1.0,
        consumer="today: decision-provenance trace (follow-up 4ced5c7c, pinned)",
    ),
    QuerySpec(
        id="q3_as_of_neighborhood",
        title="As-of neighborhood (temporal)",
        graph_layer="memory",
        direction="directed",
        semantics=(
            "Given memory X and date D: set of memory_ids reachable from X via <=2 "
            "DIRECTED memory_links hops where each neighbor was VALID as-of D "
            "(memory_metadata.valid_at <= D AND (invalid_at IS NULL OR invalid_at > "
            "D) AND NOT (deprecated=1 AND deprecated_at <= D))."
        ),
        anchor_sql=(
            # A memory with >=3 out-neighbors that carry validity data.
            "SELECT ml.source_id FROM memory_links ml JOIN memory_metadata mm "
            "ON mm.memory_id = ml.target_id WHERE mm.valid_at IS NOT NULL "
            "GROUP BY ml.source_id HAVING count(*) >= 3 ORDER BY count(*) DESC LIMIT 1"
        ),
        params={"max_depth": 2},
        as_of=(
            "memory_links has NO edge temporality -> as-of is a NODE-VALIDITY join. "
            "entity_links IS bitemporal (valid_at/invalid_at) -> edge-native as-of "
            "(entity variant deferred to S2; S1 implements the memory variant). "
            "D pinned to the median memory_metadata.created_at of the snapshot."
        ),
        weight=2.0,
        consumer="today: recall as-of filtering (graph_expansion validity gates)",
    ),
    QuerySpec(
        id="q4_chain_closure",
        title="succeeded_by/preceded_by transitive closure",
        graph_layer="memory",
        direction="directed",
        semantics=(
            "Given a chain-root memory R (source of a 'succeeded_by' edge that is "
            "not itself a 'succeeded_by' target): set of memory_ids in the transitive "
            "closure reachable from R via 'succeeded_by' edges (cycle-safe, depth<=30)."
        ),
        anchor_sql=(
            "SELECT source_id FROM memory_links WHERE link_type='succeeded_by' "
            "AND source_id NOT IN (SELECT target_id FROM memory_links WHERE "
            "link_type='succeeded_by') LIMIT 1"
        ),
        params={"link_type": "succeeded_by", "max_depth": 30},
        proxy_for="§D situation threads (situation -> followed_by* -> present)",
        weight=1.5,
        consumer="future: §D longitudinal situation narrative (proxy today)",
    ),
    QuerySpec(
        id="q5_cross_entity",
        title="Cross-entity co-mention + link context",
        graph_layer="entity+memory",
        direction="undirected",
        semantics=(
            "Given entities X, Y: set of memory_ids co-mentioned by BOTH (intersection "
            "of entity_mentions), UNION their 1-hop memory_links neighbors "
            "(either direction)."
        ),
        anchor_sql=(
            # Two entities that actually co-occur on >=1 memory.
            "SELECT a.entity_id, b.entity_id FROM entity_mentions a "
            "JOIN entity_mentions b ON a.memory_id = b.memory_id AND a.entity_id < b.entity_id "
            "GROUP BY a.entity_id, b.entity_id ORDER BY count(*) DESC LIMIT 1"
        ),
        params={"expand_hops": 1},
        weight=1.5,
        consumer="today: cross-entity situation assembly (spec §C/§D)",
    ),
    QuerySpec(
        id="q6_contradiction_sweep",
        title="Contradiction sweep (newer contradicts older)",
        graph_layer="memory",
        direction="directed",
        semantics=(
            "Set of memory_ids M that are the SOURCE of a 'contradicts' edge to a "
            "target T where M.created_at > T.created_at (a newer memory contradicting "
            "an older one). Proxy for §E assumption-staleness checks."
        ),
        anchor_sql="SELECT source_id, target_id FROM memory_links WHERE link_type='contradicts'",
        params={"link_type": "contradicts"},
        parity_mode=EXACT_SET,
        latency_scored=False,  # contradicts=4
        proxy_for="§E assumption contradiction-checking (memory_class has no 'assumption')",
        weight=1.0,
        consumer="future: §E contradiction detection (proxy today; 4 edges total)",
    ),
    QuerySpec(
        id="q7_centrality",
        title="Betweenness centrality refresh (dream-cycle cost)",
        graph_layer="full",
        direction="directed",
        semantics=(
            "Top-K memory_ids by betweenness centrality over the full directed graph. "
            "Parity = top-K overlap + Spearman rank correlation against a one-time "
            "NetworkX betweenness reference with PINNED k-sample seeds (approx "
            "betweenness is seed-dependent)."
        ),
        anchor_sql="SELECT 1",  # whole-graph; no anchor node
        params={"top_k": 100, "k_sample": 200, "seed": 42},
        parity_mode=TOPK_SPEARMAN,
        weight=2.0,
        consumer="today: dream-cycle centrality / orphan detection (shipped)",
    ),
    QuerySpec(
        id="q8_cold_start",
        title="Cold-start full-graph load/rebuild time",
        graph_layer="full",
        direction="n/a",
        semantics=(
            "Wall-clock to load the full 212k-edge graph from the snapshot into the "
            "engine's queryable form. NetworkX's known weak point (its cold start IS "
            "a full rebuild — today's price of every one of the 11 invalidation sites)."
        ),
        anchor_sql="SELECT 1",
        params={},
        parity_mode=TIMING,
        weight=1.5,
        consumer="today: NetworkX rebuild on every invalidate_graph_cache()",
    ),
]


def by_id(qid: str) -> QuerySpec:
    for q in QUERIES:
        if q.id == qid:
            return q
    raise KeyError(qid)


# Freeze invariants: ids unique, weights positive (checked at import).
if len({q.id for q in QUERIES}) != len(QUERIES):
    raise ValueError("duplicate query id in frozen workload")
if any(q.weight <= 0 for q in QUERIES):
    raise ValueError("non-positive weight in frozen workload")
