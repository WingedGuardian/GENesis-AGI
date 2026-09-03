"""NetworkX-incremental control engine (the baseline candidate).

Represents "NetworkX done correctly as an engine": it REUSES production's exact
BFS (``genesis.memory.graph._bfs_with_strength``) and builds an
``nx.MultiDiGraph`` — the same shape production's ``_ensure_graph`` now builds —
so all parallel-typed edge-pairs are preserved and it matches the all-rows SQL
oracle. Its cold-start IS a full rebuild from the snapshot — the honest
architecture, and the number that quantifies today's price of every one of the
11 ``invalidate_graph_cache()`` sites.

HISTORY: production ``graph.py`` used a plain ``DiGraph`` that silently collapsed
those parallel-typed edges (last-write-wins) until the MultiDiGraph fix. This
engine predates that fix and was the finding that motivated it; any adopted
engine must keep representing the true multigraph. (The pair count quoted here
was 115 at the 2026-08-06 snapshot and is 139 as of 2026-09-02 — it tracks the
graph, so re-derive it rather than citing this line.)

One residual difference from production: ``load()`` below calls ``add_edge``
without an explicit ``key=``, so networkx assigns auto-incrementing integer
keys, whereas production passes ``key=link_type``. On a snapshot containing a
duplicate ``(source, target, link_type)`` row the two would diverge (this keeps
both, production keeps one). The production PK forbids that row, so it cannot
occur in a snapshot exported from a real database.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict, deque

import networkx as nx

from genesis.memory.graph import _bfs_with_strength

from ..base import LoadStats, QueryResult, module_available
from ..queries import QuerySpec


class NxIncrementalEngine:
    name = "nx_incremental"

    def __init__(self) -> None:
        self.G: nx.MultiDiGraph | None = None
        self.mentions_by_entity: dict[str, set[str]] = {}
        self.meta: dict[
            str, tuple
        ] = {}  # memory_id -> (valid_at, invalid_at, deprecated, deprecated_at, created_at)
        self.anchors: dict[
            str, dict
        ] = {}  # {query_id: resolved-anchor-dict}, set by the harness before run()
        self.load_ms: float = 0.0
        self._nodes = 0
        self._edges = 0

    @classmethod
    def available(cls) -> bool:
        return module_available("networkx")

    async def load(self, snapshot_path: str) -> LoadStats:
        t0 = time.perf_counter()
        conn = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
        try:
            G = nx.MultiDiGraph()
            for s, t, lt, st in conn.execute(
                "SELECT source_id, target_id, link_type, strength FROM memory_links"
            ):
                # Coalesce BOTH: production's `strength < min_strength` guard
                # already rejects a None strength before selection, but the
                # shared `_bfs_with_strength` now tie-breaks on the
                # (strength, link_type) tuple, so a NULL link_type here would
                # raise rather than sort. The production CHECK constraint makes
                # that unreachable from a real snapshot; this keeps the engine
                # honest against a hand-built one.
                G.add_edge(s, t, link_type=lt or "", strength=st if st is not None else 0.0)

            mentions: dict[str, set[str]] = defaultdict(set)
            for mid, eid in conn.execute("SELECT memory_id, entity_id FROM entity_mentions"):
                mentions[eid].add(mid)

            meta: dict[str, tuple] = {}
            for row in conn.execute(
                "SELECT memory_id, valid_at, invalid_at, deprecated, deprecated_at, created_at FROM memory_metadata"
            ):
                meta[row[0]] = tuple(row[1:])
        finally:
            conn.close()

        self.G = G
        self.mentions_by_entity = dict(mentions)
        self.meta = meta
        self._nodes = G.number_of_nodes()
        self._edges = G.number_of_edges()
        self.load_ms = (time.perf_counter() - t0) * 1000
        return LoadStats(
            engine=self.name, nodes=self._nodes, edges=self._edges, load_ms=self.load_ms
        )

    # ── per-query implementations (return the canonical reachable node-id SET) ──

    def _q1(self, entity_id: str | None, *, max_depth: int, min_strength: float) -> frozenset[str]:
        seeds = self.mentions_by_entity.get(entity_id or "", set())
        result: set[str] = set(seeds)
        for seed in seeds:
            for node in _bfs_with_strength(
                self.G, seed, max_depth=max_depth, min_strength=min_strength
            ):
                result.add(node.memory_id)
        return frozenset(result)

    def _valid_as_of(self, mid: str, d: str) -> bool:
        md = self.meta.get(mid)
        if not md or md[0] is None:  # valid_at
            return False
        valid_at, invalid_at, deprecated, deprecated_at, _ = md
        if valid_at > d:
            return False
        if invalid_at is not None and invalid_at <= d:
            return False
        return not (deprecated == 1 and deprecated_at is not None and deprecated_at <= d)

    def _q3(self, x: str | None, d: str | None, *, max_depth: int) -> frozenset[str]:
        if not x or not d or x not in self.G:
            return frozenset()
        visited = {x}
        queue: deque[tuple[str, int]] = deque([(x, 0)])
        result: set[str] = set()
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for _, t, _data in self.G.out_edges(node, data=True):
                if t in visited or not self._valid_as_of(t, d):
                    continue
                visited.add(t)
                result.add(t)
                queue.append((t, depth + 1))
        return frozenset(result)

    def _q4(self, root: str | None, *, link_type: str, max_depth: int) -> frozenset[str]:
        if not root or root not in self.G:
            return frozenset({root}) if root else frozenset()
        # Depth-bounded closure (honors the frozen max_depth); mirrors oracle_q4.
        visited = {root}
        queue: deque[tuple[str, int]] = deque([(root, 0)])
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for _, t, data in self.G.out_edges(node, data=True):
                if data.get("link_type") == link_type and t not in visited:
                    visited.add(t)
                    queue.append((t, depth + 1))
        return frozenset(visited)

    def _q5(self, a: str | None, b: str | None) -> frozenset[str]:
        co = self.mentions_by_entity.get(a or "", set()) & self.mentions_by_entity.get(
            b or "", set()
        )
        result: set[str] = set(co)
        for m in co:
            if m not in self.G:
                continue
            for _, t, _d in self.G.out_edges(m, data=True):
                result.add(t)
            for s, _, _d in self.G.in_edges(m, data=True):
                result.add(s)
        return frozenset(result)

    def _q6(self) -> frozenset[str]:
        result: set[str] = set()
        for s, t, data in self.G.edges(data=True):
            if data.get("link_type") != "contradicts":
                continue
            cs, ct = self.meta.get(s), self.meta.get(t)
            if cs and ct and cs[4] and ct[4] and cs[4] > ct[4]:  # created_at
                result.add(s)
        return frozenset(result)

    def _q2(self, *, max_depth: int) -> frozenset[str]:
        decisions: set[str] = set()
        contradicts_nodes: set[str] = set()
        for s, t, data in self.G.edges(data=True):
            lt = data.get("link_type")
            if lt == "decided":
                decisions.add(s)
            elif lt == "contradicts":
                contradicts_nodes.add(s)
                contradicts_nodes.add(t)
        result: set[str] = set()
        for d in decisions:
            visited = {d}
            queue: deque[tuple[str, int]] = deque([(d, 0)])
            found = False
            while queue:
                node, depth = queue.popleft()
                if node in contradicts_nodes:
                    found = True
                    break
                if depth < max_depth:
                    for _, t, data in self.G.out_edges(node, data=True):
                        if data.get("link_type") == "supports" and t not in visited:
                            visited.add(t)
                            queue.append((t, depth + 1))
            if found:
                result.add(d)
        return frozenset(result)

    def _q7(self, *, top_k: int, k_sample: int, seed: int) -> list[str]:
        n = self.G.number_of_nodes()
        if n == 0:
            return []
        k = min(k_sample, n)
        scores = nx.betweenness_centrality(self.G, k=k, seed=seed)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [mid for mid, _ in ranked[:top_k]]

    async def run(self, query: QuerySpec, params: dict) -> QueryResult:
        anchors = self.anchors.get(query.id, {})
        t0 = time.perf_counter()
        node_ids: frozenset[str] = frozenset()
        ranking: list[str] = []
        scalar_ms: float | None = None

        if query.id == "q1_entity_dossier":
            node_ids = self._q1(
                anchors.get("entity_id"),
                max_depth=params["max_depth"],
                min_strength=params["min_strength"],
            )
        elif query.id == "q2_decision_chain":
            node_ids = self._q2(max_depth=params["max_depth"])
        elif query.id == "q3_as_of_neighborhood":
            node_ids = self._q3(
                anchors.get("memory_id"), anchors.get("as_of"), max_depth=params["max_depth"]
            )
        elif query.id == "q4_chain_closure":
            node_ids = self._q4(
                anchors.get("root"), link_type=params["link_type"], max_depth=params["max_depth"]
            )
        elif query.id == "q5_cross_entity":
            node_ids = self._q5(anchors.get("entity_a"), anchors.get("entity_b"))
        elif query.id == "q6_contradiction_sweep":
            node_ids = self._q6()
        elif query.id == "q7_centrality":
            ranking = self._q7(
                top_k=params["top_k"], k_sample=params["k_sample"], seed=params["seed"]
            )
        elif query.id == "q8_cold_start":
            scalar_ms = self.load_ms
        run_ms = (time.perf_counter() - t0) * 1000
        return QueryResult(
            engine=self.name,
            query_id=query.id,
            node_ids=node_ids,
            ranking=ranking,
            scalar_ms=scalar_ms,
            run_ms=run_ms,
            cardinality=len(node_ids) if node_ids else len(ranking),
        )

    def stats(self) -> dict:
        return {
            "engine": self.name,
            "nodes": self._nodes,
            "edges": self._edges,
            "load_ms": round(self.load_ms, 1),
        }
