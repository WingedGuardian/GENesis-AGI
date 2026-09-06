"""NetworkX GraphStore — the in-process projection of ``memory_links``.

This is the incumbent backend, moved behind the ``GraphStore`` seam unchanged:
a lazily-built ``MultiDiGraph`` rebuilt in full whenever a writer marks it
stale. Its traversal and centrality logic are the originals from
``memory/graph.py``, comments and all — those comments record MEASURED
behaviour (tie rates, label-flip rates) and are load-bearing.

ONE behaviour is new here, and it closes a pre-existing correctness gap rather
than changing the projection: cross-process staleness. ``invalidate()`` flips a
flag on ONE store instance in ONE process, so a dream-cycle process that writes
links could never mark the MCP server's cached graph stale — the server kept
serving a graph that predated the write until it happened to write a link
itself. The store now also compares SQLite's own ``PRAGMA data_version``, which
changes when ANOTHER connection commits and (deliberately) does not change for
our own writes.

Those semantics were PROBED before being designed on (2026-09-06, WAL +
aiosqlite, with a control that must not flip): another process committing moves
the counter; our own commit does not; a second external commit moves it again.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from genesis.memory.graphstore import GraphNode, GraphUnavailableError

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)

try:
    import networkx as nx

    _NX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NX_AVAILABLE = False


def _bfs_with_strength(
    G: object,  # nx.MultiDiGraph
    root_id: str,
    *,
    max_depth: int,
    min_strength: float,
    link_type_filter: str | None = None,
) -> list[GraphNode]:
    """BFS traversal with edge-attribute filtering.

    NetworkX's bfs_edges doesn't filter by edge attributes, so we roll
    a simple BFS that respects min_strength and optional link_type.
    """
    if root_id not in G:
        return []

    visited: set[str] = {root_id}
    queue: deque[tuple[str, int]] = deque([(root_id, 0)])
    results: list[GraphNode] = []

    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue

        # A pair may carry several typed edges (MultiDiGraph), so out_edges
        # yields one tuple per parallel edge. Consider them all and keep the
        # STRONGEST that passes the filters — otherwise the arbitrary survivor
        # merely moves from load time to traversal time (the first parallel
        # edge would win via the `visited` check) and a weak edge could still
        # mask a strong one.
        #
        # Strength-max mirrors memory_links.neighbors_of's MAX(strength)
        # collapse, but note neighbors_of returns no link_type — this path is
        # the only one that must also PICK a type, so strongest-wins is a
        # deliberate choice, not an inherited convention. Revisit if polarity
        # types start appearing on multi-type pairs (0 of 139 today carry
        # `contradicts` as their max-strength edge). Note the consumers of THIS
        # path (mcp/memory/core.py:431,:704) put link_type in front of the model
        # and do NOT exclude `contradicts` — graph_expansion, which does exclude
        # it, reaches the graph through memory_links_crud.neighbors_of and never
        # calls this function. So demoting the type here is a small real
        # improvement on the one path that surfaces it, not consistency with a
        # subsystem that already filters it elsewhere.
        #
        # The comparison is on the (strength, link_type) TUPLE, not on strength
        # alone, because strength alone leaves ties to row order: 106 of the 139
        # live multi-type pairs carry EQUAL strengths (MEASURED 2026-09-02), and
        # the loader's SELECT has no ORDER BY, so a strength-only max would keep
        # reporting an arbitrary type for those — the same defect as the DiGraph
        # collapse, just narrowed. The secondary key is a DETERMINISTIC tie-break,
        # not a semantic ranking; it happens to demote `contradicts` (which sorts
        # early), the safe direction. 0 of the 106 tied pairs carries one.
        #
        # This makes the choice WITHIN one (source, target) pair well-defined. It
        # does NOT make the traversal's reported labels row-order independent, and
        # nothing here claims that: the `visited` check below means a node
        # reachable from several parents is claimed by whichever parent the queue
        # reaches first, and queue order follows row order. MEASURED 2026-09-02 on
        # the live graph: ~15.8% of reported labels flip under a reversed row
        # order. Attribution at 500 roots traced every flip to multi-parent claim
        # order and none to ties — but that zero is a SUBSAMPLE BOUND, not an
        # absolute: at 3000 roots the tie-break itself removes 16 of 21,486 flips
        # (~0.07%). Ties are a rounding error on this surface, not nil.
        #
        # Pre-existing (~15.8% before the multigraph change too) and tracked as
        # follow-up ab0d0c28 rather than changed here, since best-parent-wins is a
        # traversal-semantics change needing its own blast-radius measurement.
        # "Reach-set-neutral (0 delta)" holds for THIS function's full output, but
        # not for what the model sees: core.py:437,:709 slice nodes[:5] after a
        # stable sort on (depth, -strength), so on 3.5% of roots a different SET
        # of five memories reaches the context depending on row order. Same root
        # cause, same follow-up — stated here so the bound is not read as wider
        # than it is.
        best: dict[str, tuple[float, str]] = {}
        for _, neighbor, data in G.out_edges(node, data=True):
            if neighbor in visited:
                continue
            strength = data.get("strength", 0.0)
            edge_type = data.get("link_type", "")

            if strength < min_strength:
                continue
            if link_type_filter and edge_type != link_type_filter:
                continue

            current = best.get(neighbor)
            if current is None or (strength, edge_type) > current:
                best[neighbor] = (strength, edge_type)

        for neighbor, (strength, edge_type) in best.items():
            visited.add(neighbor)
            results.append(GraphNode(
                memory_id=neighbor,
                link_type=edge_type,
                depth=depth + 1,
                strength=strength,
            ))
            queue.append((neighbor, depth + 1))

    # Match CTE output order: depth ascending, strength descending
    results.sort(key=lambda n: (n.depth, -n.strength))
    return results


class NetworkxGraphStore:
    """In-process ``MultiDiGraph`` projection, rebuilt on demand.

    State that used to live in module globals is per-instance, so a test (or a
    future second backend) can hold its own projection without reaching into
    another's. The facade owns the single production instance.
    """

    name = "networkx"

    def __init__(self) -> None:
        self._graph: object | None = None
        self._dirty: bool = True
        # The connection the cached projection was built from, held by
        # IDENTITY rather than id() — CPython recycles id()s after GC, so an
        # id-keyed token can silently match a different connection. Holding
        # the reference pins at most one connection, which is the long-lived
        # shared one in practice.
        self._built_conn: object | None = None
        self._built_data_version: int | None = None

    def invalidate(self) -> None:
        """Mark the projection stale; the next read rebuilds it."""
        self._dirty = True

    async def _data_version(self, db: aiosqlite.Connection) -> int | None:
        """SQLite's own 'another connection committed' counter.

        Returns None if the pragma is unavailable, which degrades to the
        pre-existing flag-only behaviour rather than failing the read.
        """
        try:
            cursor = await db.execute("PRAGMA data_version")
            row = await cursor.fetchone()
            return int(row[0]) if row else None
        except Exception:  # pragma: no cover — pragma should not fail
            # NOT debug: a None here disables token-based staleness for this
            # connection's whole lifetime (degrading to the pre-seam flag-only
            # behaviour). A mechanism whose purpose is "no more silent
            # staleness" must not switch itself off quietly.
            logger.warning(
                "PRAGMA data_version unavailable — cross-process staleness "
                "detection is DISABLED for this connection; the graph will "
                "refresh only on an explicit invalidate()",
                exc_info=True,
            )
            return None

    async def _is_stale(self, db: aiosqlite.Connection) -> bool:
        """True when the cached projection must be rebuilt."""
        if self._graph is None or self._dirty:
            return True
        # A different connection than the one we built from: we cannot compare
        # its data_version against ours (the counter is per-connection), so
        # rebuild conservatively rather than trust a cache we cannot validate.
        #
        # This is cheap ONLY because each process holds ONE long-lived
        # connection, so it fires at most once per process: the MCP child
        # passes memory_mod._db, dream-centrality passes the runtime's shared
        # serialized connection, and the ambient worker opens one mode=ro
        # connection per spawn. A second long-lived connection appearing in any
        # of those processes would turn this into a rebuild storm on a
        # 264k-edge graph (seconds per rebuild) — that invariant is
        # load-bearing and worth re-checking before adding one.
        if db is not self._built_conn:
            return True
        if self._built_data_version is None:
            return False
        current = await self._data_version(db)
        return current is not None and current != self._built_data_version

    async def _ensure_graph(self, db: aiosqlite.Connection) -> object:
        """Lazy-load the graph from memory_links, rebuild if stale."""
        if not await self._is_stale(db):
            return self._graph

        start = time.monotonic()
        # Stamp BEFORE the load, and do not "optimise" this back.
        # In WAL the read snapshot is fixed when the SELECT first steps, while a
        # PRAGMA read afterwards runs in a NEW read transaction — so a token read
        # after the load can already include a commit the loaded rows do not,
        # pinning a stale projection as "fresh" for the rest of the process's
        # life. MEASURED 2026-09-06: an external commit landing inside the load
        # window vanished from the graph permanently, because stamped == live.
        # Stamping the pre-load value errs the safe way: a mid-load external
        # commit costs exactly one rebuild on the next read, and that rebuild is
        # CORRECT, not spurious.
        pre_load_version = await self._data_version(db)
        cursor = await db.execute(
            "SELECT source_id, target_id, link_type, strength FROM memory_links"
        )
        rows = await cursor.fetchall()

        # MultiDiGraph, not DiGraph: memory_links' primary key is
        # (source_id, target_id, link_type), so one pair may legitimately carry
        # several typed edges. A DiGraph cannot hold parallel edges — the second
        # add_edge for a pair overwrites the first's attributes — so the graph kept
        # an arbitrary survivor and the strength/link_type filters were
        # evaluated against it.
        G = nx.MultiDiGraph()
        for source_id, target_id, link_type, strength in rows:
            G.add_edge(
                source_id, target_id,
                key=link_type,
                link_type=link_type, strength=strength,
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Graph cache rebuilt: %d nodes, %d edges in %.1fms",
            G.number_of_nodes(), G.number_of_edges(), elapsed_ms,
        )
        if G.number_of_edges() > 50_000:
            logger.warning(
                "Graph has %d edges — measure NetworkX rebuild cost; consider an "
                "incremental or server-backed graph if rebuilds become a bottleneck",
                G.number_of_edges(),
            )

        self._graph = G
        self._dirty = False
        self._built_conn = db
        self._built_data_version = pre_load_version
        return G

    async def traverse(
        self,
        db: aiosqlite.Connection,
        root_id: str,
        *,
        max_depth: int,
        min_strength: float,
    ) -> list[GraphNode]:
        """Neighbours of ``root_id``, ordered (depth, -strength)."""
        if not _NX_AVAILABLE:
            raise GraphUnavailableError(
                "NetworkX is not importable — the in-process graph cannot be built"
            )
        G = await self._ensure_graph(db)
        return _bfs_with_strength(
            G, root_id, max_depth=max_depth, min_strength=min_strength,
        )

    async def centrality(
        self, db: aiosqlite.Connection, top_n: int | None
    ) -> list[tuple[str, float]]:
        """Memories ranked by betweenness centrality, descending."""
        if not _NX_AVAILABLE:
            # "The store is unreachable" and "no bridges exist" are DIFFERENT
            # answers, and returning [] for both let the first masquerade as the
            # second: the dream-centrality consumer reads an empty result as "no
            # bridges", wipes centrality_cache, and the importance shield then
            # computes no threshold — bridge-node protection silently disappears
            # because a library failed to import. A decision-tier consumer must
            # never degrade silently (issue #1641 / the graph-store seam contract),
            # so unavailability RAISES; an empty graph still returns [] below,
            # because zero nodes genuinely means zero bridges.
            raise GraphUnavailableError(
                "NetworkX is not importable — centrality cannot be computed"
            )

        G = await self._ensure_graph(db)
        if G.number_of_nodes() == 0:
            return []

        # Use approximate betweenness for large graphs to avoid blocking
        n_nodes = G.number_of_nodes()
        k = min(200, n_nodes) if n_nodes > 200 else None
        scores = nx.betweenness_centrality(G, k=k)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked if top_n is None else ranked[:top_n]
