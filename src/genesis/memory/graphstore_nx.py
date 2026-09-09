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
from typing import TYPE_CHECKING

from genesis.memory.graphstore import (
    GraphNode,
    GraphUnavailableError,
    invalid_memory_ids,
)

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)

try:
    import networkx as nx

    _NX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NX_AVAILABLE = False


def _connection_identity(db: object) -> object:
    """The object whose replacement means "this is a different connection".

    NOT `db` itself. Production passes a `SerializedConnection` PROXY, and its
    recovery path swaps the connection it wraps IN PLACE — `db/connection.py`
    closes the old handle and rebinds `_conn` via `object.__setattr__` — while
    the proxy's own identity never changes. So an identity check on `db` cannot
    fire after a reconnect, and the `data_version` comparison below then reads a
    FRESH connection's counter against one stamped from the connection that was
    just closed. Those counters are per-connection and unrelated, so staleness
    detection silently stops working at exactly the moment the database has just
    recovered from errors.

    Unwrapping is uniform rather than a special case: a bare
    `aiosqlite.Connection` also exposes `_conn` (its underlying sqlite3 handle),
    and in both cases that attribute changes precisely when the real connection
    underneath does. `_conn` is in `SerializedConnection._OWN_ATTRS`, so reading
    it returns the proxy's own wrapped handle and is never delegated onward.

    Two proxies sharing one underlying connection deliberately compare EQUAL —
    same connection, same data, nothing to rebuild.
    """
    return getattr(db, "_conn", db)


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
    frontier: list[str] = [root_id]
    results: list[GraphNode] = []
    depth = 0

    while frontier and depth < max_depth:

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
        # `best` is scoped to the whole LEVEL, not to one expanding parent, and
        # that is the point. Per-parent, a node reachable from several parents was
        # claimed by whichever one the queue happened to reach first — so the
        # reported edge followed the loader's row order (its SELECT has no ORDER
        # BY) and could be the WEAKER of the two. That is not cosmetic: `strength`
        # is put in front of the model AND is what the consumers sort on before
        # taking the top five, so a node credited to a weaker parent sinks in that
        # order and can leave the slice entirely.
        #
        # MEASURED on main@36000ebbc against `memory/graph.py`, whose loader
        # applies NO visibility filter — carried here verbatim with the function.
        # THIS module's loader drops every edge with a hidden endpoint first
        # (29,868 of 264,191, 11.3%), so 256,063 links and 66,856 roots are not
        # its denominators and the rates below are UPPER BOUNDS on the filtered
        # graph, not measurements of it. The direction and the mechanism carry
        # over unchanged; only the magnitudes are someone else's population.
        # Old vs new, over that unfiltered population, at the real call
        # parameters max_depth=2, min_strength=0.3:
        #   68,330 of 1,066,912 reported nodes (6.40%) gained a higher, truer
        #     strength, across 50.4% of roots; 0 were ever lowered.
        #   top-five SET churn between a forward and a reversed row order: 3.09%
        #     before, 0 after; the full output is likewise identical under both.
        #   reach-set unchanged (0 roots) and no reported depth changed (0 nodes).
        # Draining the level is what allows the cross-parent comparison.
        #
        # State the DENOMINATOR when quoting any of this. 6.40% is over every node
        # the walk computes (~16 per root); restricted to the five that actually
        # reach the model it is 0.16% of surfaced nodes and 0.7% of lookups. The
        # blast radius at that slice is separate again: the surfaced SET changes on
        # 1.94% of roots and its ORDER on 8.15%. An earlier revision of this comment
        # quoted a 1,000-root sample and read an order of magnitude high on the
        # surfaced surface — which is why every figure here is a population count.
        #
        # Sample-drawn figures also drift between runs on an unchanged table: the
        # loader's SELECT has no ORDER BY, so node insertion order — and any sample
        # drawn from it — varies per rebuild. Prefer the population numbers above.
        #
        # The commit below is ordered by a TOTAL key, and that alone is what makes
        # the whole output deterministic: it fixes the append sequence, and the
        # final sort is stable, so equal `(depth, -strength)` keys keep that
        # sequence rather than the row order they used to keep. Note `drift.py`
        # consumes that sequence as a RANKED list for RRF (its `local_ids`,
        # drift.py:202) even though it reads no labels, so the ordering here is
        # load-bearing for a second consumer, not just for the sliced view.
        #
        # A total key on the FINAL sort as well was tried and dropped. It is
        # redundant by construction — a stable sort of an already-deterministic
        # list cannot reintroduce nondeterminism — and measured redundant too
        # (0 differences either way across 1,447 live roots). Keeping it would only
        # have reordered ties gratuitously and widened the divergence from the CTE
        # fallback's documented `(depth, strength DESC)`.
        best: dict[str, tuple[float, str]] = {}
        for node in frontier:
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

        next_frontier: list[str] = []
        # Key is (-strength, neighbour_id) and DELIBERATELY excludes link_type,
        # unlike the within-pair comparison above. The id alone already makes the
        # key total, so link_type buys no determinism here — and it is not neutral:
        # sorting equal-strength neighbours alphabetically by TYPE front-loads
        # early-sorting relationships in the order the model reads. MEASURED over
        # all 66,856 roots, 13.8% of which carry a (depth, strength) tie group:
        # including link_type moved the reported top-1 type by +32.8%
        # (categorized_as), +31.8% (action_item_for), -23.8% (preceded_by) and
        # -39.6% (succeeded_by) against the id-only key. Memory ids are UUIDs, so
        # they carry no such correlation. The within-pair key above is a different
        # case: there the two candidates are the SAME pair and a type must be
        # picked, so a stated rule beats an arbitrary one.
        for neighbor, (strength, edge_type) in sorted(
            best.items(), key=lambda kv: (-kv[1][0], kv[0])
        ):
            visited.add(neighbor)
            results.append(GraphNode(
                memory_id=neighbor,
                link_type=edge_type,
                depth=depth + 1,
                strength=strength,
            ))
            next_frontier.append(neighbor)
        frontier = next_frontier
        depth += 1

    # Match CTE output order: depth ascending, strength descending. Deliberately
    # left as a partial key — ties now resolve to the deterministic commit order
    # established above, so no further tiebreak is needed to make this stable.
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
        if _connection_identity(db) is not self._built_conn:
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
        # Visibility parity with normal recall: drop edges whose endpoints
        # recall itself hides. Two queries + a set membership test rather than
        # one SQL pass with NOT EXISTS — both were measured to produce the
        # IDENTICAL set on the live graph (234,323 of 264,191 rows), but the
        # SQL form costs +2.8s per cold build (a correlated subquery per edge)
        # against +99ms for this one, and the staleness token makes rebuilds
        # MORE frequent, not less.
        # Two freshness caveats, both stated because the cache's staleness
        # signal does not cover them:
        #  - Every invalidate_graph_cache() site is a memory_links writer, but
        #    this predicate reads memory_metadata. Deprecation is covered
        #    incidentally (its writers rewire links); TIME-DRIVEN expiry is not
        #    — an `invalid_at` in the future arrives with no write event at all,
        #    so a quiet process keeps serving the memory until some unrelated
        #    rebuild. MEASURED exposure: 114 memories carry a future invalid_at,
        #    4 of them have any edge.
        #  - These are two autocommit statements, hence two WAL read snapshots;
        #    an edge committed between them is filtered against an invalid-set
        #    read just before it. Self-healing — pre_load_version is stamped
        #    ahead of BOTH, so the next read rebuilds.
        # The two DB reads are wrapped TOGETHER, and only they. The seam's
        # contract says a store that cannot reach its backend raises
        # GraphUnavailableError — and this store honoured that for exactly one
        # cause, a missing NetworkX. Everything else escaped raw: a locked,
        # closed or corrupt connection came out of `traverse()` as an aiosqlite
        # error, or (measured) a bare `ValueError: no active connection`.
        # `graph.py` catches only GraphUnavailableError, so those bypassed the
        # facade's entire degrade chain — no fallback to the CTE, no warning,
        # the raw error surfacing at whatever called `traverse()`. `drift.py`
        # and `dream_centrality.py` are the exposed readers; `mcp/memory/core.py`
        # survives only on a bare `except`.
        #
        # Scoped to the READS on purpose. A failure in the graph BUILD below is
        # a defect in this module, not an unreachable backend, and laundering it
        # into "unavailable" would send a caller to a fallback for a bug that
        # the fallback shares. `_data_version` guards itself already and
        # degrades to None rather than raising, which is its own documented
        # choice and is left alone.
        try:
            invalid = await invalid_memory_ids(db)
            cursor = await db.execute(
                "SELECT source_id, target_id, link_type, strength FROM memory_links"
            )
            fetched = await cursor.fetchall()
        except Exception as exc:
            raise GraphUnavailableError(
                f"the memory database cannot be read — the graph cannot be built: {exc}"
            ) from exc
        rows = [row for row in fetched if row[0] not in invalid and row[1] not in invalid]

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
        # DO NOT introduce an `await` between the fetch above and this line.
        # Clearing _dirty is safe only because the two staleness signals cover
        # complementary cases (MEASURED 2026-09-07):
        #   * ANOTHER connection's mid-load commit is invisible to our held
        #     snapshot — and moves data_version, which the token catches.
        #   * OUR OWN connection's mid-load commit is VISIBLE to the fetch that
        #     races it (a connection reads its own writes), so the graph already
        #     contains it and there is no staleness to signal. data_version
        #     deliberately does not move for it, and does not need to.
        # An await here would break the second case and only the second case: a
        # same-connection writer could then commit AFTER the fetch, set _dirty,
        # and have it cleared on the next line with nothing else left to notice.
        # Locked by test_a_same_connection_write_is_seen_by_the_load_that_races_it.
        self._dirty = False
        self._built_conn = _connection_identity(db)
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
