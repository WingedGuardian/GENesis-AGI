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
timestamp and every read re-evaluates it against a live ``$now``, so a memory
that expires by the CLOCK becomes invisible the moment it does, with no write
event needed.

That improvement covers the time half of the predicate ONLY, and the other half
runs the other way -- stated here because an earlier version of this paragraph
claimed the whole predicate and was wrong. ``deprecated`` is SNAPSHOTTED into the
node at projection time, so a memory deprecated AFTER the last ``project()``
stays visible here until the next one, while the NetworkX store hides it on its
next rebuild (its writers rewire links, which fires ``invalidate()``). Setting or
clearing an ``invalid_at`` post-projection is the same shape. So against the
incumbent this store is FRESHER on elapsed time and STALER on every write, and
the second half is bounded only by how often the projector runs -- see
``invalidate()`` below, which is the honest statement of that bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from genesis.env import falkordb_socket_path
from genesis.memory.graphstore import GraphNode, GraphUnavailableError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable

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

#: Ceilings on a network round-trip, split by CALLER because the two callers
#: answer to different deadlines. Live worst cases MEASURED 2026-09-07: a
#: traversal 7.2ms (depth 3 on the highest out-degree root), the slowest
#: projection batch ~0.33s, a full projection 12.4s.
#:
#: READ bounds a user recall. `mcp/memory/core.py:451` gives the graph
#: enrichment phase a 500ms budget and checks it only BETWEEN results, so from
#: the budget's side a single hung traversal is unbounded — one 30s query would
#: stall a recall 60x past the phase budget with nothing to show for it. (Its
#: sibling call site at `core.py:728` has no phase budget at all, so there this
#: deadline is the ONLY bound.)
#:
#: 0.5s is chosen for HEADROOM, not because degrading is cheap, and the
#: difference matters. MEASURED 2026-09-08 on the live engine: warm traversal
#: p50 2.5ms / p95 5.5ms / max 9.5ms, cold connect+query 13.8-25.6ms. So 0.5s is
#: ~80x p95 and kills no real traversal. But timing out degrades to NetworkX,
#: and that is NOT faster: the NetworkX store rebuilds whenever its cache is
#: dirty, MEASURED at ~3.6s against this install's 269k links, and at 1,610
#: links/day it is dirtied roughly once a minute — so a fallback usually pays
#: the rebuild, and `core.py:459` charges it to the SAME 500ms budget. Between
#: roughly 0.5s and 3.6s of engine latency this deadline makes the recall
#: SLOWER than waiting would have.
#:
#: That band is accepted deliberately: an engine 80x past its p95 is
#: pathological, and the case this bound exists for is the one with no other
#: answer — an engine that never replies at all, where any finite ceiling beats
#: an unbounded wait. Do not read it as "falling back is free".
_READ_TIMEOUT_S = 0.5

#: PROJECT bounds a batch job with no reader waiting on it, so it is sized to
#: catch a HUNG engine and nothing else — 90x the slowest measured batch.
_PROJECT_TIMEOUT_S = 30.0

#: How long a projector may hold the right to publish before the engine takes
#: it back. Sized as a CRASH ESCAPE, not as a normal-path bound: a full
#: projection MEASURED at 12-16s, so 300s is ~20x, generous enough that a slow
#: run never loses its claim mid-build, and short enough that a projector killed
#: between acquire and release blocks the next one for five minutes rather than
#: forever. The engine's own expiry is what makes it self-healing — nothing has
#: to notice the crash.
_PUBLISH_LOCK_TTL_S = 300

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


#: One projection at a time per graph key, PROCESS-wide. Keyed by graph key
#: rather than held on the instance because two `FalkorGraphStore` objects can
#: name the same key — the facade builds its own, and the CLI builds another —
#: and an instance lock would not see the collision at all.
_PROJECT_LOCKS: dict[str, asyncio.Lock] = {}


def _connect_in_daemon_thread(fn: Any, **kwargs: Any) -> asyncio.Future[Any]:
    """Run a blocking constructor off-loop on a thread that cannot outlive us.

    `asyncio.to_thread` would be the obvious call, and it is what this used to
    do — but its worker lives in the loop's DEFAULT executor, and `asyncio.run`
    JOINS that executor during teardown. So a constructor that never returns
    stopped blocking the traversal (the deadline handles that) and started
    blocking process EXIT instead: the projector CLI would print its error and
    then hang forever with nothing left to do.

    A daemon thread is not joined at interpreter exit, so an abandoned connect
    costs a parked thread until the process ends rather than preventing the
    process from ending. That is the right trade for a construction we have
    already given up on — and it only ever happens once per store, because the
    in-flight future is cached and shielded.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def _settle(setter: Any, value: Any) -> None:
        # The awaiting side may have been cancelled by its deadline while this
        # thread was still blocked; settling a done future raises.
        if not fut.done():
            setter(value)

    def _deliver(setter: Any, value: Any) -> None:
        # The loop may be CLOSED by the time an abandoned connect finally
        # returns — which is the normal end of the case this helper exists for,
        # since the process was already tearing down. `call_soon_threadsafe`
        # raises RuntimeError then, inside a daemon thread with nobody to catch
        # it, so it surfaces as an unhandled-thread-exception with no reader.
        # There is nothing left to deliver to; dropping it is the answer.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_settle, setter, value)

    def _work() -> None:
        try:
            result = fn(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed to the future verbatim
            _deliver(fut.set_exception, exc)
        else:
            _deliver(fut.set_result, result)

    threading.Thread(target=_work, name="falkordb-connect", daemon=True).start()
    return fut


def _project_lock(graph_key: str) -> asyncio.Lock:
    """The lock guarding projections of ``graph_key``.

    `setdefault` on a plain dict is safe here: it does not await, so on a
    single-threaded event loop two coroutines cannot both create one.
    """
    return _PROJECT_LOCKS.setdefault(graph_key, asyncio.Lock())


class FalkorGraphStore:
    """GraphStore over a long-lived FalkorDB server on a unix socket."""

    name = "falkordb"

    def __init__(self, socket_path: str | None = None, graph_key: str = GRAPH_KEY) -> None:
        self._socket_path = socket_path or str(falkordb_socket_path())
        self._graph_key = graph_key
        self._db: Any | None = None
        #: The in-flight client construction, shared so a timed-out caller does
        #: not start a second one. See `_graph` for why that matters.
        self._connecting: asyncio.Future[Any] | None = None
        #: Engine-side key naming who may publish this graph. Derived from the
        #: graph key so a test key and the canonical one never contend.
        self._publish_lock_key = f"{graph_key}_publishing"

    # ── connection ────────────────────────────────────────────────────

    async def _graph(self) -> Any:
        """The graph handle, constructing the client at most once.

        Constructed in a THREAD and cached. The async client's ``__init__`` is
        synchronous and performs a blocking `Is_Cluster` round-trip: MEASURED at
        3.0ms of socket I/O that stalled the event loop 15.6ms against a 1.2ms
        median heartbeat. Per-query construction would put that on the recall
        hot path; once per process puts it nowhere that matters.

        Deliberately carries NO deadline of its own. The construction is one leg
        of whatever operation asked for it, and bounding the legs separately
        gives a caller no bound it can reason about — so `_bounded` wraps the
        connect and the operation TOGETHER, and this stays inside that wrapper.

        SHIELDED, and that is what makes the deadline safe to apply here at all.
        MEASURED 2026-09-08: cancelling an `asyncio.wait_for` does NOT stop the
        `to_thread` worker it was waiting on — four timed-out calls leave four
        live threads. So bounding the connect naively would trade one thread
        held forever (the old unbounded behaviour) for a NEW thread burned on
        every attempt, exhausting the default executor under a wedged engine and
        then stalling every other `to_thread` in the process. Caching the
        in-flight construction and shielding it means the deadline cancels the
        WAIT and never the CONSTRUCTION: one thread total, however many callers
        time out on it.

        A construction that FAILS clears the cache so the next call retries; only
        a construction still running is shared.
        """
        if not _FALKOR_AVAILABLE:
            raise GraphUnavailableError(
                "the falkordb client is not importable — the graph engine cannot be reached"
            )
        if self._db is None:
            if self._connecting is None:
                # No await between the test and the assignment, so two coroutines
                # cannot both start one on a single-threaded loop.
                self._connecting = _connect_in_daemon_thread(
                    _FalkorDB, unix_socket_path=self._socket_path
                )
            try:
                self._db = await asyncio.shield(self._connecting)
            except asyncio.CancelledError:
                # Our WAIT was cancelled (the deadline), not the construction.
                # Leave the task in place so the next caller joins it instead of
                # starting a second one, and let the timeout surface as itself.
                raise
            except Exception as exc:
                # Covers a missing socket (engine not armed) and a refused
                # connection alike: from a reader's side both mean unreachable.
                self._connecting = None
                raise GraphUnavailableError(
                    f"cannot reach the graph engine at {self._socket_path}: {exc}"
                ) from exc
            self._connecting = None
        return self._db.select_graph(self._graph_key)

    async def _connection(self) -> Any:
        """The raw redis connection, for key-level operations the graph API lacks.

        MEASURED 2026-09-07: this is an ASYNC client — `rename`/`delete` return
        coroutines, and a caller that forgets to await one gets a silent no-op
        plus a RuntimeWarning it will never see in a service log.

        Inside this module only `_key_op` may call this, so every key operation
        inherits a deadline; a test asserts that allow-list. Tests call it
        directly to clean up their own graph keys, which is outside the guard.
        """
        await self._graph()
        return self._db.connection

    async def _bounded(
        self, factory: Callable[[], Awaitable[Any]], *, timeout: float, what: str
    ) -> Any:
        """Run ONE network round-trip under a deadline. The chokepoint.

        Every network operation this store performs goes through here, and a
        test fails if a new one does not. That guard is the actual finding: an
        earlier version bounded `graph.query` alone, so the client CONSTRUCTION
        (a blocking `Is_Cluster` round-trip) and the raw `delete`/`rename` key
        operations each reached the socket with no deadline at all. Three of the
        five network paths could hang forever, and a review that named two of
        them would have left the other three exactly as they were. Bounding a
        sample of a class leaves the class unbounded.

        ``factory`` is a zero-arg coroutine FUNCTION rather than a coroutine,
        because the deadline has to cover connecting as well as the operation
        and the connect happens inside it.
        """
        try:
            return await asyncio.wait_for(factory(), timeout=timeout)
        except TimeoutError as exc:
            raise GraphUnavailableError(
                f"{what} exceeded {timeout}s — the engine is not answering"
            ) from exc
        except GraphUnavailableError:
            # Already the right type and already carries the better message
            # (e.g. "cannot reach the graph engine at ..."). Re-wrapping would
            # bury the cause behind a generic one.
            raise
        except Exception as exc:
            # BusyLoadingError lands here too: the engine refuses reads for
            # ~0.9s after a restart while it loads. Unavailable, NOT empty --
            # returning [] would tell dream-centrality there are no bridges and
            # let it wipe the shield's thresholds.
            raise GraphUnavailableError(f"{what} failed: {exc}") from exc

    async def _query(
        self,
        cypher: str,
        params: dict[str, Any],
        *,
        key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Run Cypher against ``key`` (default: the canonical projection).

        The default deadline is the READ one, so a call site that forgets to
        choose gets the tighter of the two — the fail direction that degrades a
        recall to NetworkX rather than the one that stalls it.

        Resolved in the BODY rather than as `timeout: float = _READ_TIMEOUT_S`,
        because a signature default binds the constant's VALUE at def time: the
        module attribute is then unreachable, and a test patching it changes
        nothing while reading as though it had. Two tests here did exactly that
        (verified: `_query.__kwdefaults__["timeout"]` stayed 0.5 under the
        patch). The constant stays the single source this way.
        """
        timeout = _READ_TIMEOUT_S if timeout is None else timeout

        async def _run() -> Any:
            await self._graph()
            return await self._db.select_graph(key or self._graph_key).query(cypher, params)

        return await self._bounded(_run, timeout=timeout, what="graph query")

    async def _key_op(self, op: str, *args: Any, timeout: float, **kwargs: Any) -> Any:
        """A raw key operation (`delete`, `rename`, `set`, `eval`), under a deadline.

        The graph API has no key-level verbs, so these go through the redis
        connection — which is exactly why they used to escape the query
        timeout. Routing them here is what makes the bound a property of the
        STORE rather than of one method.
        """

        async def _run() -> Any:
            conn = await self._connection()
            return await getattr(conn, op)(*args, **kwargs)

        return await self._bounded(_run, timeout=timeout, what=f"graph key {op}")

    async def _acquire_publish_lock(self) -> str | None:
        """Claim the right to publish this graph key, ACROSS PROCESSES.

        The in-process lock cannot see another process, and the staging keys are
        deliberately per-process, so two projector runs build independently and
        then both `RENAME` onto the canonical key. Publication order is decided
        by who finishes LAST, not by who read the newer data — so a run that
        read an older snapshot and built slowly can overwrite a newer projection
        and walk the live graph backwards until something projects again.

        `SET NX EX` in the engine both runs already talk to is the smallest
        thing that orders them. The TTL is the crash escape: a projector killed
        mid-build cannot hold this forever.
        """
        token = f"{os.getpid()}:{time.monotonic_ns()}"
        acquired = await self._key_op(
            "set",
            self._publish_lock_key,
            token,
            nx=True,
            ex=_PUBLISH_LOCK_TTL_S,
            timeout=_PROJECT_TIMEOUT_S,
        )
        return token if acquired else None

    async def _release_publish_lock(self, token: str) -> None:
        """Release the lock ONLY if we still hold it.

        A plain `DELETE` would be wrong: if our build overran the TTL, the lock
        has already been handed to someone else, and deleting it then frees a
        lock another run is relying on — reintroducing the race this closes, at
        the one moment it is most likely. Compare-and-delete in a script so the
        check and the delete cannot be separated.
        """
        # `eval` here is the ENGINE's EVAL command — a fixed Lua literal run
        # server-side — not Python's builtin. The script is a constant in this
        # file and takes no caller input; the key and token are bound as KEYS/
        # ARGV, which is the parameterised form, so nothing interpolates.
        with contextlib.suppress(Exception):
            await self._key_op(
                "eval",
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                self._publish_lock_key,
                token,
                timeout=_PROJECT_TIMEOUT_S,
            )

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
        reaching a node — best-parent-wins.

        THAT DIVERGES FROM THE INCUMBENT TODAY, and the divergence is measured
        rather than estimated. The NetworkX store in this tree marks a node
        visited through whichever parent the queue reached first
        (`graphstore_nx.py`, which documents the row-order dependence and defers
        the fix); PR #1628 changes it to best-parent-wins, and #1628 is OPEN, not
        merged. So until it lands the two backends AGREE on the node SET and can
        DISAGREE on a multi-parent node's reported label.

        MEASURED 2026-09-08 against the live graph (72,876 nodes / 269,187
        edges): 400 roots sampled with a fixed seed from the 2,000 highest
        out-degree source ids, replayed through both stores at the real call
        parameters (`max_depth=2, min_strength=0.3`). Node-SET differences
        0/400. Top-5 slice differs on 13/400, reported strength somewhere on
        240/400. `mcp/memory/core.py` shows the model `nodes[:5]`, so the 13 is
        the number a reader would actually notice.

        The two divergence counts move with the sample and with the data — an
        earlier run on a different sample read 39 and 179 — so treat them as the
        SCALE of the divergence, not as a trend. The 0 is the invariant, and it
        is the one this pass re-ran to protect.

        This store is not the side to change: best-parent-wins is the correct
        semantics and #1628 is what makes the incumbent agree. Recorded here so
        the cutover order is a decision rather than a surprise — #1628 lands
        before the lever moves, or the label divergence ships with it knowingly.
        """
        if max_depth < 1:
            return []
        # ONE deadline for the whole call, not one per query. An empty traversal
        # makes a SECOND awaited query (`_assert_projection_exists`), and giving
        # each its own `_READ_TIMEOUT_S` let a slow engine spend ~1s reaching an
        # empty answer — twice the phase budget the ceiling was chosen to
        # respect, and the empty path is exactly the one that then pays for a
        # multi-second NetworkX rebuild on top. The budget belongs to the
        # TRAVERSAL, so it is stamped once here and spent down.
        deadline = time.monotonic() + _READ_TIMEOUT_S
        result = await self._query(
            _TRAVERSE.replace("{depth}", str(int(max_depth))),
            {
                "root": root_id,
                "min_strength": float(min_strength),
                # FLOAT, not int. Truncating both sides to whole seconds hides a
                # memory for up to a second before it actually expires: with now
                # at T.100 and invalid_at at T.900, `invalid_epoch > now` is
                # T > T -> False here while SQLite and NetworkX both still say
                # visible. Genesis's timestamps carry microseconds
                # (`db/timeutil.py`), so the precision is real data, not a
                # theoretical tail.
                "now": time.time(),
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
            await self._assert_projection_exists(deadline=deadline)
        nodes.sort(key=lambda n: (n.depth, -n.strength, n.memory_id))
        return nodes

    async def _assert_projection_exists(self, *, deadline: float | None = None) -> None:
        """Raise if the projection was never built, rather than answering [].

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

        EXISTENCE, NOT POPULATION, and the difference is a real install. Node
        count answers the wrong question: on a fresh or fully pruned install
        `memory_links` legitimately holds zero rows, so a perfectly good
        projection of an empty graph counted zero and reported itself as never
        built — the facade then fell back and logged a warning on every
        graph-enriched recall, about a backend that was representing the empty
        graph exactly right. It also contradicted the seam's own contract, which
        blesses an empty graph as an answer.

        A MARKER NODE, and the two obvious alternatives are both wrong. Key
        existence looks right and is not: MEASURED on the live engine 2026-09-08,
        a read-only `MATCH` against a MISSING graph key CREATES that key, so by
        the time an empty traversal asks, the traversal itself has materialised
        it and `EXISTS` can only ever answer 1. (Checked in isolation, `EXISTS`
        does distinguish the two states — which is exactly how that fix passed a
        probe and failed against the engine. The isolated fact was true; the
        inference about the sequence the code runs was not.)

        So `project()` writes one `:ProjectionMeta` node as its last act before
        the swap, and this asks for that. Present -> a projection was built, and
        an empty result is the honest answer for a graph that really is empty.
        Absent -> never built, or the engine restarted and lost it (it holds no
        persistence), or a read just conjured a blank key. The marker carries
        `built_at` because F3's projector wants exactly this number for staleness
        reporting, and a second mechanism for it would be one too many.

        `:ProjectionMeta` is not `:Memory`, so it cannot appear in a traversal:
        `_TRAVERSE` binds both endpoints as `:Memory`.

        ``deadline`` is the caller's REMAINING budget, not a fresh one. This is
        the second query an empty traversal makes, and giving it an independent
        `_READ_TIMEOUT_S` meant an empty answer could cost twice the ceiling.
        A budget already spent is itself the answer — the engine is too slow to
        confirm anything, which is unavailability — so it raises rather than
        borrowing more time.
        """
        if deadline is None:
            timeout = _READ_TIMEOUT_S
        else:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise GraphUnavailableError(
                    "the traversal budget was spent before the projection could be "
                    "confirmed — the engine is not answering fast enough to tell an "
                    "empty graph from an unbuilt one"
                )
        result = await self._query("MATCH (m:ProjectionMeta) RETURN count(m)", {}, timeout=timeout)
        rows = result.result_set or []
        if not (rows and rows[0] and rows[0][0]):
            raise GraphUnavailableError(
                f"graph {self._graph_key!r} carries no projection marker — the "
                "projection has never been built, or the engine restarted and "
                "lost it (it holds no persistence). Build it with "
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
        nothing here to invalidate. Must stay cheap and handle-free — every
        `memory_links` writer calls this through a lazy import.

        WHAT THAT COSTS, stated rather than left for a reader to derive. Every
        link insert and delete calls this (`db/crud/memory_links.py` among 13
        sites); the NetworkX store answers by marking itself dirty and rebuilding
        on the next read, so it is current within one read. Here the call does
        nothing, so the projection is stale from the last `project()` until the
        next one — serving removed links and omitting new ones, and NOT raising,
        because a stale projection is indistinguishable from a current one from
        the engine's side.

        The bound on that window is the projector's cadence, and the projector is
        F3's first slice: a scheduled full re-projection, decided at HOURLY. At
        the measured 1,610 links/day that is at most ~70 links stale (a burst can
        exceed it — dream cycles write in batches), and a restart self-heals
        within the hour. It is a bounded window, not invalidation-on-write; the
        DB-side change signal that would give write-level freshness is filed and
        unbuilt (issue #1641), and is F3's second slice.

        None of it is reachable today: the lever defaults to `networkx`
        (`config/graphstore.yaml`), so nothing reads this projection. The window
        is what the cutover has to accept, or close first.
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

        SERIALISED TWICE, because there are two different collisions and one
        lock cannot see both.

        IN-PROCESS (`_project_lock`): the staging key is per-PROCESS, so two
        coroutines projecting the same key in one process share it — and with
        the whole-build cleanup, the loser's cleanup wipes the winner's staging
        mid-build, after which the winner writes its marker and renames a
        PARTIAL graph that reads as complete. That is the shape F3's in-server
        scheduled projector introduces, closed here rather than left to a future
        PR remembering `max_instances=1`.

        CROSS-PROCESS (`_acquire_publish_lock`): two projector PROCESSES get
        DIFFERENT staging keys, so they never corrupt each other's build — and
        then both rename onto the canonical key. Publication order is decided by
        who finishes LAST rather than who read newer data, so a run that read an
        older snapshot and built slowly overwrites a newer projection and walks
        the live graph BACKWARDS until something projects again. An asyncio lock
        is invisible across processes; the ordering has to live in the engine
        both of them already talk to.

        A refused claim RAISES rather than waiting. Waiting would mean holding a
        SQLite read open for the length of someone else's build, and the work is
        redundant anyway — the run that holds the lock is projecting the same
        rows.
        """
        async with _project_lock(self._graph_key):
            token = await self._acquire_publish_lock()
            if token is None:
                raise GraphUnavailableError(
                    f"another projection of {self._graph_key!r} is already in progress "
                    "— not starting a second one, because the two would race to "
                    "publish and the SLOWER build wins, which can walk the live "
                    "graph backwards. Wait for it to finish, or re-run once it has."
                )
            try:
                return await self._project_locked(db)
            finally:
                await self._release_publish_lock(token)

    async def _project_locked(self, db: aiosqlite.Connection) -> dict[str, int]:
        """The body of `project()`, run under its per-key lock."""
        cursor = await db.execute(
            "SELECT source_id, target_id, link_type, strength FROM memory_links"
        )
        edges = list(await cursor.fetchall())
        meta = await self._metadata(db)
        now = time.time()

        ids = sorted({e[0] for e in edges} | {e[1] for e in edges})
        # PER-PROCESS staging key — not per-run, which an earlier version of
        # this comment claimed and the code never did. With a key shared across
        # concurrent projections the second run's opening `delete` lands
        # mid-build of the first, which then renames a PARTIAL graph onto the
        # live key; worse, the marker is written last, so that partial graph
        # carries a VALID projection marker and reads as complete. Silent wrong
        # answers, which is the worst outcome available here.
        #
        # The pid separates two PROCESSES. It does nothing for two coroutines in
        # ONE process, which is exactly the shape F3's in-server scheduled
        # projector introduces — so the lock above serialises them rather than
        # leaving that to a future PR remembering `max_instances=1`. Keeping the
        # pid (rather than a uuid per run) is deliberate: it means a run whose
        # cleanup never happened — a SIGKILL, a hard cancellation — has its
        # orphan reclaimed by this same process's NEXT run, which under an
        # hourly projector is an hour later rather than never.
        staging = f"{self._graph_key}_staging_{os.getpid()}"
        await self._key_op("delete", staging, timeout=_PROJECT_TIMEOUT_S)

        # CLEANUP COVERS THE WHOLE BUILD, not just the swap. An earlier version
        # wrapped only the rename, so a failure or timeout in index creation or
        # in any batch left the partial staging graph behind — and a staging
        # graph is a full copy of the projection against a 512mb engine cap.
        # The opening `delete` above reclaims THIS process's own orphan on the
        # next run, which bounds the leak at one per process rather than one per
        # failure; it does nothing for a process that never runs again, which is
        # every CLI invocation and every server restart. Once the projector runs
        # on a schedule (F3) this path stops being rare, so it is handled here
        # rather than left to the next run's opening delete.
        try:
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
                    timeout=_PROJECT_TIMEOUT_S,
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
                    timeout=_PROJECT_TIMEOUT_S,
                )

            # The projection-built marker, written LAST so it can only be
            # present in a graph whose nodes and edges are already there — it
            # attests to a COMPLETE build, not a started one. `_assert_projection_
            # exists` reads it to tell "built, and genuinely empty" from "never
            # built", which node count cannot do and key existence cannot either
            # (a read-only MATCH materialises a missing key — measured).
            # `built_at` is here because F3's projector needs exactly this number
            # to report staleness.
            await self._query(
                "CREATE (:ProjectionMeta {built_at: $at, nodes: $n, edges: $e})",
                {"at": now, "n": len(ids), "e": len(edges)},
                key=staging,
                timeout=_PROJECT_TIMEOUT_S,
            )

            try:
                await self._key_op("rename", staging, self._graph_key, timeout=_PROJECT_TIMEOUT_S)
            except Exception as exc:
                raise GraphUnavailableError(
                    f"projection built but the swap onto {self._graph_key!r} failed: {exc}"
                ) from exc
        except BaseException:
            # BaseException, not Exception: a cancelled projection (the job's
            # deadline, a shutdown) must not leave the copy behind either.
            try:
                await self._key_op("delete", staging, timeout=_PROJECT_TIMEOUT_S)
            except Exception:
                # Caught rather than suppressed, and LOUD. The cleanup's own
                # failure must not replace the real cause — hence the bare
                # re-raise below — but swallowing it silently loses a full copy
                # of the projection (MEASURED 57MB for this install's 72,879
                # nodes / 269,201 edges, against the unit's 512mb maxmemory).
                # An engine that died mid-build is exactly when this fails, and
                # a CLI run never comes back to reclaim it.
                logger.warning(
                    "falkordb: could not drop staging graph %r after a failed "
                    "projection — it holds a full copy of the projection against "
                    "the engine's memory cap, and this process may not run again "
                    "to reclaim it",
                    staging,
                    exc_info=True,
                )
            raise

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
            await self._query(
                "CREATE INDEX FOR (m:Memory) ON (m.id)",
                {},
                key=key,
                timeout=_PROJECT_TIMEOUT_S,
            )
        except GraphUnavailableError as exc:
            if "already indexed" not in str(exc):
                raise
            logger.debug("falkordb: id index already present, reusing it")

    @staticmethod
    async def _metadata(db: aiosqlite.Connection) -> dict[str, tuple[float | None, int]]:
        """``{memory_id: (invalid_epoch_or_None, deprecated_int)}``.

        Epoch SECONDS, not an ISO string. Both forms were proven to filter
        correctly in the spike, but numbers need no temporal type and carry no
        collation question — and this engine has no temporal types at all.

        An UNPARSEABLE non-null value takes SQLite's answer, not "never
        expires". The two are different, and treating them as the same was a
        real parity break: SQLite never parses `invalid_at` at all — it compares
        the raw TEXT lexicographically against an ISO `now` — so `2020-bad`
        sorts before a 2026 timestamp and is HIDDEN there, while a store that
        read it as unparseable-therefore-NULL kept it VISIBLE. Same memory, two
        backends, opposite answers, and the schema does not constrain the format
        so nothing prevents it.

        MEASURED 2026-09-08: 0 of 1,633 live non-null values are unparseable, so
        this is latent rather than firing — which is exactly when it is cheap to
        close. The comparison is made HERE, at projection time, because a
        malformed string has no epoch to compare later; its verdict is therefore
        as fresh as the projection, the same bound everything else in this store
        carries.
        """
        cursor = await db.execute("SELECT memory_id, invalid_at, deprecated FROM memory_metadata")
        now_iso = datetime.now(UTC).isoformat()
        out: dict[str, tuple[float | None, int]] = {}
        for memory_id, invalid_at, deprecated in await cursor.fetchall():
            epoch = _to_epoch(invalid_at)
            if epoch is None and invalid_at:
                # Unparseable and non-null. Ask what SQLite would say.
                epoch = 0.0 if str(invalid_at) <= now_iso else None
            out[memory_id] = (epoch, 1 if deprecated else 0)
        return out


def _is_hidden(entry: tuple[float | None, int] | None, now: float) -> bool:
    """Would the validity predicate hide this memory right now?

    Same predicate the Cypher applies, evaluated in Python over the metadata the
    projection already read — so the count costs no extra scan of
    `memory_metadata`. A memory with no metadata row at all stays VISIBLE.
    """
    if entry is None:
        return False
    epoch, deprecated = entry
    return bool(deprecated) or (epoch is not None and epoch <= now)


def _to_epoch(stamp: str | None) -> float | None:
    """ISO-8601 -> epoch seconds as a FLOAT, or None when unparseable.

    Fractional seconds are kept. Flooring to whole seconds hides a memory up to
    a second before it expires — with `invalid_at` at T.900 and a read at T.100,
    `invalid_epoch > now` is T > T and the memory vanishes, while SQLite and
    NetworkX compare the full ISO strings and still call it visible. Genesis
    writes microseconds (`db/timeutil.py::canonical_iso` preserves `.ffffff`),
    so this is real data rather than a theoretical tail. MEASURED on the live
    engine 2026-09-08 in both directions: stored 100 against now 100 hides the
    node, stored 100.9 against now 100.1 keeps it.

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
        return parsed.timestamp()
    return None
