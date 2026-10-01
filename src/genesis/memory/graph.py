"""Memory-graph facade — picks a backend, owns the public read surface.

The traversal and centrality logic now live behind the ``GraphStore`` seam
(``memory/graphstore.py``); this module is what the rest of Genesis imports. It
owns the single production store instance, because ``invalidate_graph_cache()``
is called by every ``memory_links`` writer — 13 call sites across 9 modules at
the time of writing — none of which holds a store reference, and several of
which hold no database handle either.

Fallback: the recursive CTE at the bottom of this module, reached on ONE
condition — the active store raised ``GraphUnavailableError``. A cold cache is
NOT a trigger, though this docstring said so for years: the store's first query
builds its projection and returns it. Worth stating precisely, because the wrong
version made the fallback sound routine when it is in fact dormant on a healthy
install — which is how the two paths were free to disagree unnoticed.

Backend today: ``NetworkxGraphStore`` — the in-process MultiDiGraph projection,
unchanged. When NetworkX cannot be imported at all, ``traverse`` still degrades
to the recursive-CTE fallback exactly as before; ``centrality_scores``
deliberately does NOT degrade — it raises, because its consumer (the importance
shield) treats "unavailable" and "empty" oppositely.

The seam exists for the graph-DB adoption (issue #1641): a server-backed engine
becomes another ``GraphStore`` and this facade's selection changes, with no
reader touched. The four readers are ``mcp/memory/core.py`` (recall enrichment
and ``memory_expand``), ``memory/drift.py``, and ``memory/dream_centrality.py``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.memory.graphstore import (
    GraphModeUnsupported,
    GraphNode,
    GraphStore,
    GraphUnavailableError,
    TraversalResult,
    hidden_predicate,
)

# Re-exported deliberately: `_bfs_with_strength` is imported ACROSS packages by
# eval/graph_bakeoff/engines/nx_incremental.py, which reuses production's exact
# BFS so the bake-off control is honest. Moving it must not break that import.
from genesis.memory.graphstore_nx import (  # noqa: F401
    NetworkxGraphStore,
    _bfs_with_strength,
)

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)

#: The gated visibility predicate, aliased for the CTE's correlated subqueries.
#: Bound ONCE and interpolated at all three sites, so the anchor's two clauses and
#: the recursive step's clause cannot drift apart — they were three separate
#: hand-written copies until a review on PR #2339 pointed out that the constant
#: introduced to prevent exactly that drift had no consumer at all.
_HIDDEN_CTE = hidden_predicate(alias="m", gated=True)


__all__ = [
    "GraphNode",
    "GraphUnavailableError",
    "TraversalResult",
    "centrality_scores",
    "invalidate_graph_cache",
    "traverse",
]

# The ONE production store. Module-level by necessity, not convenience: the
# writer sites that invalidate it (memory_links CRUD, linker, the dream jobs,
# connection pass, integrity repair) reach it through a lazy `from ... import
# invalidate_graph_cache` and have no other handle on it.
#
# Annotated against the protocol deliberately: CI runs no type checker, so this
# annotation plus the conformance test is the only thing standing between a
# future backend and silently violating the raise-never-return-empty contract.
_store: GraphStore = NetworkxGraphStore()

# The FalkorDB store, built only if the lever ever selects it. Kept module-level
# alongside `_store` rather than constructed per call for the same reason
# `_store` is: NetworkX caches a projection that costs ~5s to rebuild, and the
# FalkorDB client's constructor does a blocking round-trip. Per-call
# construction would pay both on every traversal.
_falkor_store: GraphStore | None = None


def _reset_store_for_tests() -> None:
    """Drop the production stores and their pinned connections.

    Mirrors ``memory/health.py::_reset_top_tags_state``. The store holds a
    strong reference to the last connection it built from, which over a long
    test session keeps closed aiosqlite connections (each a Thread) alive.
    """
    global _store, _falkor_store
    _store = NetworkxGraphStore()
    _falkor_store = None
    # The lever's config cache is keyed on file mtimes, and a test that writes a
    # config then reads it back can move faster than mtime resolves — so the
    # reset seam has to clear it too, or a test sees the previous test's mode.
    from genesis.memory.graphstore_config import reset_config_cache

    reset_config_cache()


def _traversal_store() -> GraphStore:
    """The store TRAVERSALS use, per the config lever, read fresh.

    Scoped to traversal on purpose. `centrality_scores` deliberately does NOT
    consult this: FalkorDB cannot compute betweenness and says so by raising,
    and `centrality_scores` has no fallback by design — so routing it here
    would turn a mode flip into a silent shutdown of the importance shield.
    Betweenness stays on NetworkX whatever this lever says.
    """
    global _falkor_store
    try:
        from genesis.memory.graphstore_config import effective_mode

        if effective_mode() != "falkordb":
            return _store
        if _falkor_store is None:
            from genesis.memory.graphstore_falkor import FalkorGraphStore

            _falkor_store = FalkorGraphStore()
        return _falkor_store
    except Exception:
        # A broken config or an unimportable client must not take traversal
        # down — it selects the incumbent, which is the whole degrade rule.
        logger.warning("graph store selection failed — using %r", _store.name, exc_info=True)
        return _store


def invalidate_graph_cache() -> None:
    """Mark the in-memory graph as stale.

    Called by writers after link creation/deletion. The next query triggers a
    full rebuild from memory_links.

    Reaches EVERY store that exists, not just the selected one: the lever can
    move between them at any time, and a store that missed invalidations while
    unselected would serve a stale projection the moment it was chosen again.
    FalkorDB's is a no-op today (its projection lives in the engine, not in
    this process), which costs nothing and keeps the rule simple.
    """
    _store.invalidate()
    if _falkor_store is not None:
        _falkor_store.invalidate()


async def _cte_or_unavailable(
    db: aiosqlite.Connection,
    root_id: str,
    max_depth: int,
    min_strength: float,
    include_deprecated: bool,
) -> list[GraphNode]:
    """The SQL fallback, with the seam's typed-error contract enforced.

    A HELPER rather than a second try/except, because the guard below was
    previously inlined in one fallback path and a later change added a second
    path without it. An exception raised inside an `except` clause is not caught
    by that clause's siblings, so the new path re-opened the exact leak this
    guard exists to close — MEASURED: a decline whose CTE then failed raised a
    bare `ValueError: no active connection` at the caller, while the older path
    correctly raised `GraphUnavailableError`.

    The fallback reads the SAME connection a store may have just failed on, so
    every non-transient cause — a closed handle, a missing table, a corrupt
    file — fails it identically. The one cause it genuinely rescues is a
    transient `database is locked`, which is why it still runs.
    """
    try:
        return await _traverse_cte(db, root_id, max_depth, min_strength, include_deprecated)
    except Exception as cte_exc:
        raise GraphUnavailableError(
            f"the graph store and its SQL fallback both failed: {cte_exc}"
        ) from cte_exc


async def traverse(
    db: aiosqlite.Connection,
    root_id: str,
    *,
    max_depth: int = 3,
    min_strength: float = 0.0,
    include_deprecated: bool = False,
) -> TraversalResult:
    """Traverse the memory graph from a root node.

    Uses the active graph store; falls back to the recursive CTE when the
    store cannot answer at all (today: NetworkX missing).

    Args:
        db: Database connection.
        root_id: Starting memory ID.
        max_depth: Maximum traversal depth (default 3).
        min_strength: Minimum link strength to follow (default 0.0).
        include_deprecated: Traverse DEPRECATED memories — as root AND as
            intermediate hops. Defaults to False, which is the correct default
            and stays the hot path: the point of the predicate is that graph
            enrichment must not surface what the search beside it hides. True is
            the caller's explicit opt-out, threaded from
            ``memory_recall(include_deprecated=True)`` (issue #1896), and it is
            honoured identically by all three implementations below — which is
            the property that matters, since which backend answers must never
            change WHICH memories are shown.

            A bitemporally EXPIRED memory is NOT reached at any value of this
            flag. That is deliberate and matches ``search_ranked``, which
            applies its ``invalid_at`` clause unconditionally and gates only the
            ``deprecated`` one — so the two halves of one recall agree about
            what is visible.

    Returns:
        TraversalResult with connected nodes and query timing.
    """
    start = time.monotonic()

    active = _traversal_store()
    try:
        nodes = await active.traverse(
            db, root_id, max_depth=max_depth, min_strength=min_strength,
            include_deprecated=include_deprecated,
        )
    except GraphModeUnsupported as exc:
        # NOT a degradation, and deliberately handled BEFORE the generic
        # handler below: the backend is reachable and healthy, it simply does
        # not implement this READ MODE. Today that is the NetworkX store
        # declining `include_deprecated=True`, because its projection is filtered at
        # build time and serving hidden memories would mean holding a second
        # unfiltered graph — measured at ~148 MiB never freed, 4.2s to build,
        # never warm in practice, and billed into a recall budget that then
        # dropped 6 of 7 enrichments (see `graphstore_nx.traverse`).
        #
        # Route straight to the CTE, skipping the NetworkX tier: the store that
        # just declined IS that tier, so trying it would decline again. The CTE
        # answers this mode from SQL in ~1ms per root with nothing resident, and
        # returns byte-identical rows — MEASURED 70/70 field-for-field against
        # every backend with the flag on.
        #
        # DEBUG, not WARNING, and that is the point of the distinct type: this
        # path is expected and correct, so logging it at warning level with a
        # traceback would train readers to ignore the genuine unavailability
        # warning below, which fires for a dead engine.
        logger.debug(
            "Graph store %r does not serve this mode — using the SQL tier: %s",
            getattr(active, "name", "?"), exc,
        )
        nodes = await _cte_or_unavailable(
            db, root_id, max_depth, min_strength, include_deprecated,
        )
    except GraphUnavailableError as exc:
        # Traversal is an ENRICHMENT path — its readers already treat a thin
        # result as "no neighbours", so degrading keeps them working.
        # centrality_scores below is the opposite case and must not do this.
        #
        # LOUD, because this stopped being a once-per-process import verdict
        # the moment a server-backed store landed: a backend that times out
        # would otherwise route every recall enrichment through the fallback
        # while looking perfectly healthy.
        logger.warning(
            "Graph store %r unavailable — falling back: %s",
            getattr(active, "name", "?"), exc, exc_info=True,
        )
        # FalkorDB degrades to NetworkX before SQL. NetworkX answers the same
        # question with the same visibility predicate, so it is a far smaller
        # step down than the CTE — which stays the last resort it always was.
        nodes = None
        if active is not _store:
            try:
                nodes = await _store.traverse(
                    db, root_id, max_depth=max_depth, min_strength=min_strength,
                    include_deprecated=include_deprecated,
                )
            except GraphModeUnsupported:
                # Reached when the PRIMARY store was unavailable AND the caller
                # asked for hidden memories: NetworkX declines that mode, so the
                # CTE below answers it. Quiet, for the same reason as above —
                # the loud warning already fired for the real failure, and this
                # second line would only describe a tier that was never going to
                # serve this mode.
                pass
            except GraphUnavailableError as nx_exc:
                logger.warning(
                    "NetworkX store also unavailable — falling back to the recursive CTE: %s",
                    nx_exc, exc_info=True,
                )
        if nodes is None:
            nodes = await _cte_or_unavailable(
                db, root_id, max_depth, min_strength, include_deprecated,
            )

    elapsed_ms = (time.monotonic() - start) * 1000

    if elapsed_ms > 100:
        logger.warning(
            "Graph traversal from %s took %.1fms (threshold: 100ms, "
            "%d nodes, depth %d)",
            root_id, elapsed_ms, len(nodes), max_depth,
        )

    return TraversalResult(root_id=root_id, nodes=nodes, query_ms=elapsed_ms)


async def centrality_scores(
    db: aiosqlite.Connection,
    top_n: int | None = 100,
) -> list[tuple[str, float]]:
    """Return memories ranked by betweenness centrality.

    Identifies memories that are "bridges" between clusters of knowledge.
    Raises GraphUnavailableError if the backend cannot answer (an EMPTY graph
    still returns [] — zero nodes means zero bridges). The NetworkX store
    additionally raises when the library itself is unimportable. Deliberately
    does NOT fall back: a decision-tier consumer must never be handed a
    silently different metric.

    ``top_n`` caps the returned slice; ``top_n=None`` returns EVERY scored
    node (the full ranking). Betweenness is computed over all nodes regardless
    — ``top_n`` is only a post-sort slice — so ``None`` adds no compute cost,
    just a longer list.
    """
    return await _store.centrality(db, top_n)


# ─── CTE fallback ────────────────────────────────────────────────────────────


async def _traverse_cte(
    db: aiosqlite.Connection,
    root_id: str,
    max_depth: int,
    min_strength: float,
    include_deprecated: bool = False,
) -> list[GraphNode]:
    """Original recursive CTE traversal (fallback).

    The walk this mirrors is `graphstore_nx._bfs_with_strength` — it lives
    behind the seam now, not above this function, and these two implementations
    answering differently is exactly what must not happen.

    ONE ROW PER MEMORY, picked the same way the NetworkX walk picks: shallowest
    depth, then the strongest edge reaching it, then link_type as a deterministic
    tie-break. That is not tidiness — it is the same correctness property this
    module's walk exists to provide, and the fallback used to contradict it.

    `SELECT DISTINCT target_id, link_type, depth, strength` keeps one row per
    COMBINATION, not per memory, so a node reached through two parents at the
    same depth came back TWICE — once credited its strongest edge and once its
    weakest. `mcp/memory/core.py` takes `traversal.nodes[:5]` and does NOT sort
    it — the order this function emits IS the selection — so the duplicate both
    occupied two of those five slots and dragged a false weaker strength into
    what the model reads. Which implementation answers must not change that.

    The window's ORDER BY mirrors that walk's `(strength, link_type)` maximum
    exactly, and the outer `ORDER BY depth, strength DESC, target_id` mirrors the
    walk's committed sequence — `(-strength, memory_id)` within a level,
    preserved through a stable final sort on `(depth, -strength)`. That matters
    because `drift.py:202` reads this sequence as a RANKED list for RRF without
    reading a single label, so two implementations agreeing on every field and
    disagreeing on order still hand that consumer different answers.

    The trailing `target_id` is EXPLICIT, not load-bearing, and the distinction is
    measured rather than assumed: with three equal-strength neighbours inserted in
    a deliberately adversarial order (z, a, m), this query returns them id-sorted
    WITH the key and identically WITHOUT it — the window's `PARTITION BY
    target_id` already groups them that way. So no test can tell the two apart,
    and none claims to. It is kept because that is a property of one engine's
    query plan, which SQLite does not promise, and stating the order costs
    nothing; do not read it as a guard something exercises.

    Window functions need SQLite >= 3.25 (2018); this install runs 3.45, and the
    repo already hard-depends on 3.35+ elsewhere (`UPDATE…RETURNING`,
    `ALTER TABLE DROP COLUMN` in migrations 0010/0014/0016), so this floor sits
    strictly below an existing one and cannot newly break a clone.

    Carries the SAME visibility predicate as the graph stores — a degraded path
    that showed the model memories the primary path hides would be worse than
    the degradation itself. Expressed in SQL here (rather than reusing
    ``invalid_memory_ids``) because the traversal is recursive; the cost is
    bounded by the edges actually walked, not the whole table.

    GUARDS ON THE ANCHOR, because the anchor row skipped constraints both the
    recursive step and the walk apply — one generator, several symptoms. Two of
    them are described here; the visibility clauses above are the others:

    * `target_id <> source_id`. The walk seeds `visited = {root_id}` and can
      therefore never emit the root; the anchor had no such guard, so a memory
      linked to itself was returned as its own related memory, burning one of the
      five slots `core.py` shows. MEASURED on the live table: 30 of 269,757 rows
      are self-links, so this fired for 30 roots.
    * `max_depth < 1` returns early (below). The anchor emits depth 1
      unconditionally, ignoring the bound that walk's `while depth < max_depth`
      respects — so `max_depth=0` asked for nothing and got a level. Inert today
      (no caller passes 0; production passes 1, 2 or the default 3) and closed
      anyway, because the claim being made here is that the two paths agree.
    """
    if max_depth < 1:
        return []
    now = datetime.now(UTC).isoformat()
    # An INT, not the bool: sqlite3 adapts bools to 0/1 already, but `? = 0` is
    # what the clauses read as and spelling it here keeps the query's own
    # semantics legible from the params tuple.
    dep_ok = 1 if include_deprecated else 0
    # The f-string interpolates `_HIDDEN_CTE` — a literal predicate built at
    # import time from constants in `graphstore`, never a value. Every id, depth
    # and flag below is a bound parameter; ruff S608 cannot tell the two apart.
    cursor = await db.execute(
        f"""
        WITH RECURSIVE connected(target_id, link_type, depth, strength, path) AS (
            SELECT target_id, link_type, 1, strength,
                   source_id || ',' || target_id
            FROM memory_links
            WHERE source_id = ?
              AND target_id <> source_id
              AND strength >= ?
              -- The ROOT is filtered too. The NX loader drops an edge when
              -- EITHER endpoint is hidden, so a hidden memory has no edges at
              -- all there; filtering only the target here would let the CTE
              -- traverse FROM a hidden root and return a subtree the primary
              -- path returns nothing for. MEASURED: 2,827 live memories are
              -- hidden AND have out-edges, and the two forms otherwise
              -- classify 6,503 edges (2.5% of the graph) differently.
              --
              -- All three visibility clauses carry the same bound
              -- `include_deprecated` flag (issue #1896), and the flag sits
              -- INSIDE the predicate rather than in front of the whole
              -- `NOT EXISTS`. That placement is the fix for a review finding on
              -- #2339: the earlier `(<flag> OR NOT EXISTS ...)` form switched
              -- off BOTH hiding reasons at once, so asking for deprecated
              -- memories also returned bitemporally EXPIRED ones — which the
              -- search beside this walk (`search_ranked`) can never return,
              -- since it applies its `invalid_at` clause unconditionally. Here
              -- the expiry term is outside the flag and the `<flag> = 0` guard
              -- kills only the deprecation term, so no flag value un-hides an
              -- expired
              -- memory. Still ONE query string for both modes: two
              -- near-identical recursive CTEs would be a drift hazard, and this
              -- walk's agreement with the two graph stores is the property the
              -- whole function exists to provide.
              AND NOT EXISTS (SELECT 1 FROM memory_metadata m
                              WHERE m.memory_id = memory_links.source_id
                                AND {_HIDDEN_CTE})
              AND NOT EXISTS (SELECT 1 FROM memory_metadata m
                              WHERE m.memory_id = memory_links.target_id
                                AND {_HIDDEN_CTE})
            UNION ALL
            SELECT ml.target_id, ml.link_type, c.depth + 1, ml.strength,
                   c.path || ',' || ml.target_id
            FROM memory_links ml
            JOIN connected c ON ml.source_id = c.target_id
            WHERE c.depth < ?
              AND ml.strength >= ?
              AND c.path NOT LIKE '%' || ml.target_id || '%'
              -- Only the TARGET is gated in the recursive step, unchanged: a
              -- row's source is some earlier row's target and was already
              -- checked there, so the anchor is the only place a source needs
              -- its own clause.
              AND NOT EXISTS (SELECT 1 FROM memory_metadata m
                              WHERE m.memory_id = ml.target_id
                                AND {_HIDDEN_CTE})
        )
        SELECT target_id, link_type, depth, strength
        FROM (
            SELECT target_id, link_type, depth, strength,
                   ROW_NUMBER() OVER (
                       PARTITION BY target_id
                       ORDER BY depth ASC, strength DESC, link_type DESC
                   ) AS rn
            FROM connected
        )
        WHERE rn = 1
        ORDER BY depth, strength DESC, target_id
        """,  # noqa: S608 — interpolates a literal predicate constant, never a value
        # Positional, so the order tracks the clauses above exactly: each
        # visibility gate contributes the `now` its expiry limb compares
        # against, then the `include_deprecated` flag its deprecation limb is
        # guarded by. (now, flag) — NOT the reverse; the flag moved INSIDE the
        # predicate when the expiry limb was taken out from under it.
        (
            root_id, min_strength,
            now, dep_ok,             # anchor source visibility
            now, dep_ok,             # anchor target visibility
            max_depth, min_strength,
            now, dep_ok,             # recursive target visibility
        ),
    )
    rows = await cursor.fetchall()
    return [
        GraphNode(memory_id=row[0], link_type=row[1], depth=row[2], strength=row[3])
        for row in rows
    ]
