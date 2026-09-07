"""FalkorDB-backed GraphStore — the server-side engine behind the seam.

SQLite remains the system of record. This store answers reads from a DERIVED,
rebuildable projection of ``memory_links``; losing it costs a re-projection and
nothing else, which is why nothing here is backed up.

Three dialect facts were MEASURED against the live engine (module 4.20.4) on
2026-09-07 rather than read from documentation, because the documentation is
wrong about one of them:

1. A variable-length relationship variable binds as an Edge, NOT a List, so
   ``ALL(x IN l ...)`` throws at EVERY length -- including ``*2..2``. Only the
   NAMED-PATH form works. This is not a style preference; the other form fails
   unconditionally.
2. FalkorDB has NO temporal types. ``RETURN datetime(...)`` answers
   ``Unknown function 'datetime'``, even though docs.falkordb.com's own Cypher
   coverage page lists Date/DateTime/LocalDateTime as supported in two places.
   Timestamps are therefore mirrored as NUMERIC epoch seconds, which the
   Neo4j->FalkorDB migration guide also prescribes.
3. The engine refuses queries for ~0.9s while loading a snapshot
   (``BusyLoadingError``). That is UNAVAILABLE, never empty -- see the seam's
   contract, which this store exists to honour.

The visibility predicate is applied at QUERY time over mirrored node properties
rather than at projection time. That is a deliberate improvement on the NetworkX
store, which filters at load and therefore cannot notice a future ``invalid_at``
that has since passed (``graphstore_nx`` documents that gap: 114 memories carry a
future ``invalid_at``, 4 of them with edges). Here the projection carries the
timestamp and every read re-evaluates it, so the same memory becomes invisible
the moment it expires, with no write event needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from genesis.env import falkordb_socket_path
from genesis.memory.graphstore import GraphNode, GraphUnavailableError

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import-time capability probe
    from falkordb.asyncio import FalkorDB as _FalkorDB

    _FALKOR_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FalkorDB = None  # type: ignore[assignment]
    _FALKOR_AVAILABLE = False

#: The canonical graph key. A spike used `spike_f2` precisely so it could never
#: collide with this one.
GRAPH_KEY = "genesis_memory"

#: Per-query ceiling. Live worst case MEASURED at 7.2ms (depth 3 on the highest
#: out-degree root) and the slowest projection batch at ~0.33s, so this bounds a
#: HUNG engine and nothing else.
_QUERY_TIMEOUT_S = 30.0

#: Batch size for the projection. MEASURED at 10k: 60,384 nodes/s and 29,867
#: edges/s, a 9.06s full projection of 68,064 nodes / 236,937 edges.
_PROJECT_BATCH = 10_000

# Hop-wise validity, over the properties the projection mirrors. Mirrors the
# SQLite predicate in `graphstore.invalid_memory_ids` -- a non-zero deprecated
# hides, a NULL invalid_at never expires, and a memory with no metadata row at
# all stays visible (the pre-existing dangling-link class, deliberately
# unchanged).
#
# Both terms are NULL-SAFE, and the `deprecated` one has to be stated explicitly
# because Cypher and SQL disagree here. MEASURED on the live engine: for a node
# carrying no `deprecated` property at all, `x.deprecated = 0` evaluates to NULL,
# so `ALL(...)` is NULL and the WHERE drops the whole path -- while SQLite's
# `deprecated != 0` is also NULL, which leaves the row OUT of the invalid set and
# therefore VISIBLE. Opposite outcomes from the same three-valued logic. Today's
# projector always writes the property (0 of 72,262 live nodes lack it), so this
# is latent -- but the seam's promise is that which store answers cannot change
# WHICH memories the model sees, and an incremental projector that MERGEs a node
# without the full property set is the ordinary way that stops being latent.
_VALID = (
    "(x.invalid_epoch IS NULL OR x.invalid_epoch > $now) "
    "AND (x.deprecated IS NULL OR x.deprecated = 0)"
)

# Best-parent-wins, resolved in the engine. The ORDER BY runs BEFORE the
# aggregation, so `head(collect(...))` picks the first row per node under that
# order: shallowest depth, then strongest last hop, then link_type as a
# deterministic tie-break. That tie-break is not cosmetic -- 106 of 139 live
# multi-type pairs carry EQUAL strengths, so strength alone would leave the
# reported label to row order, which is the very defect PR #1628 fixes in the
# NetworkX store.
#
# `link_type` must come from the SAME edge as `strength`. Returning
# `max(strength)` and the type separately would pair a strength with another
# edge's label -- and that label is emitted straight to the model at
# mcp/memory/core.py:465 and :738.
_TRAVERSE = f"""
MATCH p=(a:Memory {{id: $root}})-[:LINK*1..{{depth}}]->(b:Memory)
WHERE ALL(x IN relationships(p) WHERE x.strength >= $min_strength)
  AND ALL(x IN nodes(p) WHERE {_VALID})
WITH b.id AS id, length(p) AS d, relationships(p)[-1] AS e
ORDER BY d ASC, e.strength DESC, e.link_type DESC
WITH id, head(collect(d)) AS depth, head(collect(e)) AS best
RETURN id, depth, best.strength AS strength, best.link_type AS link_type
"""


class FalkorGraphStore:
    """GraphStore over a long-lived FalkorDB server on a unix socket."""

    name = "falkordb"

    def __init__(self, socket_path: str | None = None, graph_key: str = GRAPH_KEY) -> None:
        self._socket_path = socket_path or str(falkordb_socket_path())
        self._graph_key = graph_key
        self._db: Any | None = None

    # ── connection ────────────────────────────────────────────────────

    async def _graph(self) -> Any:
        """The graph handle, constructing the client at most once.

        Constructed in a THREAD and cached. The async client's ``__init__`` is
        synchronous and performs a blocking `Is_Cluster` round-trip: MEASURED at
        3.0ms of socket I/O that stalled the event loop 15.6ms against a 1.2ms
        median heartbeat. Per-query construction would put that on the recall
        hot path; once per process puts it nowhere that matters.
        """
        if not _FALKOR_AVAILABLE:
            raise GraphUnavailableError(
                "the falkordb client is not importable — the graph engine cannot be reached"
            )
        if self._db is None:
            try:
                self._db = await asyncio.to_thread(_FalkorDB, unix_socket_path=self._socket_path)
            except Exception as exc:
                # Covers a missing socket (engine not armed) and a refused
                # connection alike: from a reader's side both mean unreachable.
                raise GraphUnavailableError(
                    f"cannot reach the graph engine at {self._socket_path}: {exc}"
                ) from exc
        return self._db.select_graph(self._graph_key)

    async def _connection(self) -> Any:
        """The raw redis connection, for key-level operations the graph API lacks.

        MEASURED 2026-09-07: this is an ASYNC client — `rename`/`delete` return
        coroutines, and a caller that forgets to await one gets a silent no-op
        plus a RuntimeWarning it will never see in a service log.
        """
        await self._graph()
        return self._db.connection

    async def _query(self, cypher: str, params: dict[str, Any], *, key: str | None = None) -> Any:
        graph = await self._graph() if key is None else self._db_graph(key)
        try:
            return await asyncio.wait_for(graph.query(cypher, params), timeout=_QUERY_TIMEOUT_S)
        except TimeoutError as exc:
            # A hung engine would otherwise have NO bound, and `query_ms` only
            # counts queries that return — so the recall path would blow its
            # 500ms graph budget with nothing to show for it. Live worst case
            # MEASURED at 7.2ms, so this ceiling is far above any real shape.
            raise GraphUnavailableError(
                f"graph query exceeded {_QUERY_TIMEOUT_S}s — the engine is not answering"
            ) from exc
        except Exception as exc:
            # BusyLoadingError lands here too: the engine refuses reads for
            # ~0.9s after a restart while it loads. Unavailable, NOT empty --
            # returning [] would tell dream-centrality there are no bridges and
            # let it wipe the shield's thresholds.
            raise GraphUnavailableError(f"graph query failed: {exc}") from exc

    def _db_graph(self, key: str) -> Any:
        """A handle on a NON-canonical key. Callers must have connected already."""
        return self._db.select_graph(key)

    # ── GraphStore protocol ───────────────────────────────────────────

    async def traverse(
        self,
        db: aiosqlite.Connection,
        root_id: str,
        *,
        max_depth: int,
        min_strength: float,
    ) -> list[GraphNode]:
        """Neighbours of ``root_id``, ordered ``(depth, -strength)``.

        ``db`` is unused: unlike the NetworkX store, the validity predicate is
        answered from properties already in the projection rather than from a
        SQLite read per traversal. It stays in the signature because it is the
        seam's contract, and because a future store may need it.

        The reported strength is the strongest LAST hop among the shortest paths
        reaching a node. That is best-parent-wins, which is what PR #1628 makes
        the NetworkX store do; against pre-#1628 NetworkX the node SET agrees but
        a multi-parent node's reported label can differ, because there the
        claiming parent is whichever the queue reached first.
        """
        if max_depth < 1:
            return []
        result = await self._query(
            _TRAVERSE.replace("{depth}", str(int(max_depth))),
            {
                "root": root_id,
                "min_strength": float(min_strength),
                "now": int(time.time()),
            },
        )
        nodes = [
            GraphNode(
                memory_id=row[0],
                link_type=row[3] or "",
                depth=int(row[1]),
                strength=float(row[2]) if row[2] is not None else 0.0,
            )
            for row in (result.result_set or [])
            if row[0] != root_id
        ]
        if not nodes:
            await self._assert_projection_exists()
        nodes.sort(key=lambda n: (n.depth, -n.strength, n.memory_id))
        return nodes

    async def _assert_projection_exists(self) -> None:
        """Raise if the graph holds no nodes at all, rather than answering [].

        THE hazard this closes, and it arrives from the opposite side to the one
        the seam usually guards. The contract blesses "a root absent from the
        graph yields [] — genuinely no neighbours, not unavailability". An
        UNBUILT projection makes every root absent, so selecting this store
        before running `project()` answers [] for every memory in the system:
        the engine is perfectly reachable, so nothing raises, nothing falls back
        to NetworkX, and nothing logs. `mcp/memory/core.py` then sees an empty
        traversal and simply omits `graph_neighbors`, showing the model a memory
        with no connections. The health probe declines to catch it by design (a
        reachable engine with an empty projection is a healthy engine), so this
        is the only place that can.

        Consulted on EVERY empty traversal, deliberately unlatched. An earlier
        version cached "the projection exists" for the process lifetime, which
        reintroduced the very defect this method closes: the engine holds NO
        persistence (`--save "" --appendonly no`, so its unit says the graph is
        a rebuildable projection), and a traversal against a MISSING graph key
        returns an empty result with NO error — both MEASURED on the live engine
        2026-09-07. So after an engine restart under a long-lived server, the
        client reconnects transparently, every traversal answers empty, and a
        latched check would never look again: every memory would read as having
        no connections until the SERVER process restarted. The latch saved
        0.70ms (p95 0.91ms) on the empty path only — 0.14% of the 500ms graph
        budget — which is not worth a silent outage.
        """
        result = await self._query("MATCH (n:Memory) RETURN count(n)", {})
        rows = result.result_set or []
        if not (rows and rows[0] and rows[0][0]):
            raise GraphUnavailableError(
                f"graph {self._graph_key!r} holds no nodes — the projection has never "
                "been built, or the engine restarted and lost it (it holds no "
                "persistence). Build it with "
                "`python -m genesis.memory.graphstore_project`, or leave the lever "
                "on networkx."
            )

    async def centrality(
        self, db: aiosqlite.Connection, top_n: int | None
    ) -> list[tuple[str, float]]:
        """Unsupported here, and that is a raise rather than a substitution.

        FalkorDB offers no betweenness. The seam's contract is explicit that a
        backend which cannot compute a metric raises rather than inventing a
        different one: the importance shield consumes this to pick which
        memories are protected from consolidation, and silently handing it
        PageRank or degree would change WHICH memories survive. Betweenness
        stays on an ephemeral NetworkX graph in the dream-cycle batch job.
        """
        raise GraphUnavailableError(
            "FalkorDB does not implement betweenness centrality — "
            "this metric stays on the NetworkX store by design"
        )

    def invalidate(self) -> None:
        """No-op: this store holds no cached projection to mark stale.

        The projection lives in the engine, not in this process, so there is
        nothing here to invalidate. Keeping the projection current is the
        projector's job (F3); until it exists, `project()` is explicit and
        manual. Must stay cheap and handle-free — every `memory_links` writer
        calls this through a lazy import.
        """
        return None

    # ── projection ────────────────────────────────────────────────────

    async def project(self, db: aiosqlite.Connection) -> dict[str, int]:
        """Rebuild the whole projection from ``memory_links``. Explicit, not scheduled.

        F3 owns automating this (debounced on the dirty signal, with a
        generation watermark). It lives here now because a store nothing ever
        populates cannot be verified against anything.

        BUILD-THEN-SWAP, and the alternative is why. Projecting in place means
        `DETACH DELETE` followed by ~12.8s of batched writes, during which a
        concurrent traversal sees an empty, node-only, or half-edged graph and
        returns a SHORTER answer — never an error. Same failure as an unbuilt
        projection, reached while the store is correctly configured. So the new
        graph is built under a staging key and swapped in with a single RENAME,
        which is atomic from a reader's side: a reader sees the old projection
        or the new one, never a partial. MEASURED on the live engine 2026-09-07,
        including that the id index rides along with the rename.
        """
        cursor = await db.execute(
            "SELECT source_id, target_id, link_type, strength FROM memory_links"
        )
        edges = list(await cursor.fetchall())
        meta = await self._metadata(db)
        now = int(time.time())

        ids = sorted({e[0] for e in edges} | {e[1] for e in edges})
        # Per-RUN staging key, not a fixed one. With a shared key two concurrent
        # projections do not merely race to swap — the second run's opening
        # `delete` lands mid-build of the first, which then renames a PARTIAL
        # graph onto the live key. That is worse than either run losing, and it
        # is silent. The pid makes the builds disjoint so the only contention
        # left is the rename itself, where last-writer-wins is harmless because
        # both runs projected the same source rows.
        staging = f"{self._graph_key}_staging_{os.getpid()}"
        conn = await self._connection()
        await conn.delete(staging)
        await self._ensure_index(key=staging)

        for i in range(0, len(ids), _PROJECT_BATCH):
            batch = [
                {
                    "id": mid,
                    "epoch": meta.get(mid, (None, 0))[0],
                    "dep": meta.get(mid, (None, 0))[1],
                }
                for mid in ids[i : i + _PROJECT_BATCH]
            ]
            await self._query(
                "UNWIND $b AS r CREATE (:Memory "
                "{id: r.id, invalid_epoch: r.epoch, deprecated: r.dep})",
                {"b": batch},
                key=staging,
            )
        for i in range(0, len(edges), _PROJECT_BATCH):
            batch = [
                {"s": e[0], "t": e[1], "ty": e[2], "st": e[3]}
                for e in edges[i : i + _PROJECT_BATCH]
            ]
            await self._query(
                "UNWIND $b AS r MATCH (a:Memory {id: r.s}), (b:Memory {id: r.t}) "
                "CREATE (a)-[:LINK {link_type: r.ty, strength: r.st}]->(b)",
                {"b": batch},
                key=staging,
            )

        try:
            await conn.rename(staging, self._graph_key)
        except Exception as exc:
            # Leave nothing behind: an orphaned staging graph holds a full copy
            # of the projection, and the engine caps at 512mb.
            await conn.delete(staging)
            raise GraphUnavailableError(
                f"projection built but the swap onto {self._graph_key!r} failed: {exc}"
            ) from exc
        hidden = sum(1 for mid in ids if _is_hidden(meta.get(mid), now))
        return {"nodes": len(ids), "edges": len(edges), "hidden": hidden}

    async def _ensure_index(self, *, key: str | None = None) -> None:
        """Create the id index if it is not already there.

        Without it every edge MATCH in the projection is a full scan, i.e. the
        projection goes quadratic. The already-indexed case is tolerated because
        the postcondition wanted is "an index exists", and it does — a staging
        key that survived a crash keeps its index even after its nodes go. Only
        that one message is tolerated; anything else still raises.
        """
        try:
            await self._query("CREATE INDEX FOR (m:Memory) ON (m.id)", {}, key=key)
        except GraphUnavailableError as exc:
            if "already indexed" not in str(exc):
                raise
            logger.debug("falkordb: id index already present, reusing it")

    @staticmethod
    async def _metadata(db: aiosqlite.Connection) -> dict[str, tuple[int | None, int]]:
        """``{memory_id: (invalid_epoch_or_None, deprecated_int)}``.

        Epoch SECONDS, not an ISO string. Both forms were proven to filter
        correctly in the spike, but numbers need no temporal type and carry no
        collation question — and this engine has no temporal types at all.
        """
        cursor = await db.execute("SELECT memory_id, invalid_at, deprecated FROM memory_metadata")
        out: dict[str, tuple[int | None, int]] = {}
        for memory_id, invalid_at, deprecated in await cursor.fetchall():
            out[memory_id] = (_to_epoch(invalid_at), 1 if deprecated else 0)
        return out


def _is_hidden(entry: tuple[int | None, int] | None, now: int) -> bool:
    """Would the validity predicate hide this memory right now?

    Same predicate the Cypher applies, evaluated in Python over the metadata the
    projection already read — so the count costs no extra scan of
    `memory_metadata`. A memory with no metadata row at all stays VISIBLE.
    """
    if entry is None:
        return False
    epoch, deprecated = entry
    return bool(deprecated) or (epoch is not None and epoch <= now)


def _to_epoch(stamp: str | None) -> int | None:
    """ISO-8601 -> epoch seconds, or None when unparseable.

    None means "never expires", which is the same answer a NULL gives, so an
    unparseable timestamp fails toward VISIBLE. That matches the SQLite
    predicate, where `invalid_at IS NULL` also leaves a memory visible; the
    alternative would hide memories because of a formatting problem.
    """
    if not stamp:
        return None
    from contextlib import suppress
    from datetime import UTC, datetime

    with suppress(ValueError, TypeError):
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # A naive stamp would otherwise resolve in the PROCESS timezone, and
            # this box runs EST/EDT — a 4-5h skew in whether a memory counts as
            # expired. Everything Genesis writes carries +00:00 (checked: 1,620
            # of 1,620 live non-NULL `invalid_at`), so this is a guard on the
            # fail direction, which is silent, not on today's data.
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())
    return None
