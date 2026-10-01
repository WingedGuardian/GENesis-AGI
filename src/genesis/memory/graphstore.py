"""GraphStore seam — the contract every memory-graph backend answers to.

Genesis's memory graph is an in-process NetworkX projection of ``memory_links``
today. The graph-DB adoption decision (issue #1641) replaces that projection
with a server-backed engine, and this module is the seam it plugs into: one
protocol, several interchangeable implementations, one facade
(``memory/graph.py``) that owns backend selection.

THE CONTRACT, and it is the whole reason the seam exists:

    A read that cannot REACH its store raises ``GraphUnavailableError``.
    It NEVER returns an empty result.

"Unreachable" and "empty" are different answers, and collapsing them is how a
missing library silently disarmed the importance shield: ``centrality_scores``
returned ``[]`` when NetworkX was absent, the dream-centrality consumer read
that as "no bridge memories exist", wiped ``centrality_cache``, and the shield
then computed no threshold at all. Every reader here is entitled to assume that
an empty list means the graph genuinely holds nothing. A backend that cannot
support a particular read (betweenness over SQL, say) raises the same error
rather than inventing a different metric.

Every backend also applies the SAME visibility predicate (see
``invalid_memory_ids`` below), so which store answers can never change WHICH
memories the model is shown — only how fast the answer arrives.

Implementations are structural — plain classes, no inheritance — mirroring
``EmbeddingBackend`` in ``memory/embeddings.py``, the closest sibling in this
package (a protocol, a companion unavailable-error, and a chain of concrete
non-subclassing backends).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite


@dataclass
class GraphNode:
    """A neighbour reached during traversal.

    ``link_type``/``strength`` describe the STRONGEST edge on the path that
    reached this node, not every edge between the pair — the graph is a
    multigraph and a pair may carry several typed edges.
    """

    memory_id: str
    link_type: str
    depth: int
    strength: float


@dataclass
class TraversalResult:
    """A traversal's neighbours plus its wall-clock cost.

    ``query_ms`` is consumed by the recall path's cumulative graph budget, so
    every backend must report it honestly rather than leaving it at zero.
    """

    root_id: str
    nodes: list[GraphNode]
    query_ms: float


class GraphUnavailableError(RuntimeError):
    """The graph backend cannot answer AT ALL (missing library, unreachable
    store, unsupported operation) — as distinct from answering "empty".
    Decision-tier readers (dream-centrality → the importance shield) treat
    these opposite ways: empty supersedes their cache, unavailable must LEAVE
    it alone."""


class DatabaseUnreachable(GraphUnavailableError):
    """The DATABASE could not be opened or read — not the graph engine.

    Lives here, beside the error it refines, because more than one module needs
    to RAISE it: the projector opens the database, and the Falkor store reads it
    while projecting. A private type in either one leaves the other raising the
    generic error, which is exactly the defect this exists to close — an
    operator told to check the graph engine service when the graph engine was
    working and the database was not.

    A subclass, so every existing `except GraphUnavailableError` keeps catching
    it and no caller has to learn a new type to stay correct.
    """


class GraphModeUnsupported(GraphUnavailableError):
    """This backend does not implement this READ MODE. It is reachable and well.

    Distinct from unreachability, and the distinction is what stops the facade
    logging a warning-with-traceback on a perfectly healthy, expected path. The
    seam already blesses a backend declining a read it cannot serve — FalkorDB
    raises for `centrality` because it has no betweenness, and this module's own
    contract says "a backend that cannot support a particular read (betweenness
    over SQL, say) raises the same error". This names the case rather than
    leaving it indistinguishable from a dead engine.

    Raised today by the NetworkX store for `traverse(include_deprecated=True)`: its
    projection is filtered at BUILD time, so serving hidden memories would mean
    holding a second unfiltered graph. MEASURED on the live graph, which is why
    it declines rather than doing that — a second projection costs ~148 MiB held
    for the process lifetime and 4.2s to build, and it is never warm in practice
    (the staleness token moves on ordinary memory writes, so a second call
    moments later rebuilds again). Billed into the recall enrichment budget that
    dropped 6 of 7 results. The recursive-CTE tier answers the identical
    question in 59ms for 7 roots with nothing resident, and returns
    byte-identical results — MEASURED 70/70 field-for-field across all three
    backends with the flag on.

    A subclass for the same reason `DatabaseUnreachable` is: every existing
    handler keeps working, and only a caller that WANTS to route differently has
    to know the type exists.
    """


# The predicate normal recall uses to decide a memory is still visible, kept
# HERE so every backend applies the same one. READ from the two live readers,
# not invented: db/crud/memory.py::search_ranked filters
# `invalid_at IS NULL OR invalid_at > now` AND `deprecated IS NULL OR
# deprecated = 0`, and memory/graph_expansion.py repeats it on hydrated rows
# citing "visibility parity with normal recall".
#
# It deliberately does NOT read `valid_at`. The eval oracle in
# eval/graph_bakeoff/parity.py does, but that answers an AS-OF question
# ("valid at date D"), and copying it here would hide every memory whose
# valid_at is NULL — MEASURED 5.7% of live rows (4,922 of 86,369).
#
# `deprecated != 0` rather than `= 1`, matching both readers, which hide
# anything not in (NULL, 0); SQL's NULL != 0 is NULL, so unstamped rows stay
# visible. No `IS NOT NULL` guard: the column is NOT NULL DEFAULT 0 in the base
# CREATE, so that clause would be dead.
#: The predicate ALONE, parenthesised, so a caller can AND another clause onto it
#: without changing its meaning. Kept separate from the statement below because
#: `AND` binds tighter than `OR` in SQL: appending `AND memory_id IN (…)` to an
#: unparenthesised `(expired) OR deprecated != 0` parses as
#: `(expired) OR (deprecated AND in_set)` — which returns expired memories from
#: OUTSIDE the requested set, silently and with a plausible-looking row count.
#: The two independent reasons a memory is hidden, each named once so the two
#: widths below cannot drift apart. `deprecated != 0` is NULL-safe by omission:
#: a NULL `deprecated` (legacy pre-migration rows) yields NULL, which leaves the
#: row OUT of the hidden set — i.e. visible, which is the intended reading.
def _expired_limb(prefix: str) -> str:
    return f"({prefix}invalid_at IS NOT NULL AND {prefix}invalid_at <= ?)"


def _deprecated_limb(prefix: str) -> str:
    return f"{prefix}deprecated != 0"


def hidden_predicate(*, alias: str = "", gated: bool) -> str:
    """The visibility predicate, in ONE of its two widths. Never a value.

    ``gated=False`` is the WIDE form: both reasons hide, unconditionally. Used by
    the NetworkX build-time row filter and by the labelling query, which must
    treat an expired memory as hidden however the caller asked.

    ``gated=True`` is the TRAVERSAL form, narrower and deliberately so. Expiry
    hides UNCONDITIONALLY; deprecation hides only when the caller did not ask for
    it. That mirrors ``db.crud.memory.search_ranked`` exactly — it applies its
    ``invalid_at`` clause always ("The bitemporal ``invalid_at`` filter is ALWAYS
    applied", its own docstring at :174) and gates only the ``deprecated`` clause
    on ``include_deprecated`` (:212). Matching it is the point: enrichment must
    not surface what the search beside it hides, and an earlier version of this
    code un-hid BOTH limbs, so ``include_deprecated=True`` returned expired graph
    neighbours the same call's search could never return (review on PR #2339).

    Parameter order is ``(now,)`` wide and ``(now, include_deprecated)`` gated,
    in textual order. The ``? = 0`` guard kills the deprecation limb rather than
    widening the expiry one, so no value of the flag un-hides an expired memory.

    ``alias`` qualifies the columns for a correlated subquery (``alias="m"`` ->
    ``m.invalid_at``). A FUNCTION rather than two module constants because the
    recursive-CTE caller needs the aliased spelling and hand-wrote its own copy
    when only unaliased constants existed — three copies of the gated form, which
    is exactly the drift this predicate is supposed to be the single source for.
    """
    prefix = f"{alias}." if alias else ""
    expired, deprecated = _expired_limb(prefix), _deprecated_limb(prefix)
    tail = f"(? = 0 AND {deprecated})" if gated else deprecated
    return f"""(
        {expired}
        OR {tail}
    )"""


_INVALID_MEMORY_PREDICATE = hidden_predicate(gated=False)

_INVALID_MEMORY_SQL = f"""
    SELECT memory_id FROM memory_metadata
    WHERE {_INVALID_MEMORY_PREDICATE}
"""  # noqa: S608 — interpolates a literal predicate constant, never a value


async def invalid_memory_ids(db: aiosqlite.Connection) -> set[str]:
    """Memories normal recall hides — bitemporally expired, or deprecated.

    A memory with NO ``memory_metadata`` row is NOT included: that is the
    pre-existing dangling-link class (an edge outliving its memory), which
    ``graph_expansion`` already drops at hydration time and which this
    predicate deliberately leaves alone.
    """
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(_INVALID_MEMORY_SQL, (now,))
    return {row[0] for row in await cursor.fetchall()}


#: Chunk size for the scoped variant. SQLite's default variadic-parameter ceiling
#: is 999 (`SQLITE_MAX_VARIABLE_NUMBER`), so a caller with a large candidate set
#: must not build one statement out of all of it. 500 leaves headroom for the
#: timestamp parameter and for a build with a lower ceiling.
_INVALID_CHUNK = 500


async def invalid_memory_ids_among(
    db: aiosqlite.Connection, memory_ids: set[str] | frozenset[str]
) -> set[str]:
    """Which of ``memory_ids`` normal recall hides. Scoped, not a table scan.

    Same predicate as ``invalid_memory_ids`` — it shares
    ``_INVALID_MEMORY_SQL`` rather than restating it, because two copies of a
    visibility rule are two chances to disagree about what "hidden" means.

    Use this when the candidate set is already known. MEASURED on a live install
    (97,971 ``memory_metadata`` rows, 4,276 hidden): the unscoped form is a full
    `SCAN memory_metadata` at 33.8ms, while an ``IN (…)`` over ~50 known ids uses
    `sqlite_autoindex_memory_metadata_1` at 0.100ms — 338x cheaper for the same
    answer. It is also TOTAL over the ids asked about, rather than a snapshot of
    the whole table that a caller then intersects.
    """
    if not memory_ids:
        return set()
    now = datetime.now(UTC).isoformat()
    ids = list(memory_ids)
    found: set[str] = set()
    for start in range(0, len(ids), _INVALID_CHUNK):
        chunk = ids[start : start + _INVALID_CHUNK]
        # Only the PLACEHOLDER COUNT is interpolated — never a value. Every id is
        # a bound parameter, so this is not an injection vector despite the
        # f-string (ruff S608 cannot distinguish the two).
        placeholders = ",".join("?" * len(chunk))
        cursor = await db.execute(
            "SELECT memory_id FROM memory_metadata "  # noqa: S608
            f"WHERE {_INVALID_MEMORY_PREDICATE} AND memory_id IN ({placeholders})",
            (now, *chunk),
        )
        found.update(row[0] for row in await cursor.fetchall())
    return found


class GraphStore(Protocol):
    """One memory-graph backend.

    Structural, not inherited — a concrete store simply provides these members.
    """

    name: str

    async def traverse(
        self,
        db: aiosqlite.Connection,
        root_id: str,
        *,
        max_depth: int,
        min_strength: float,
        include_deprecated: bool = False,
    ) -> list[GraphNode]:
        """Neighbours reachable from ``root_id``.

        Ordered ``(depth, -strength)``. A root that is absent from the graph
        yields ``[]`` — that is genuinely "no neighbours", not unavailability.
        Raises ``GraphUnavailableError`` if the backend cannot be reached.

        ``include_deprecated`` carries the CALLER'S visibility choice to the
        store, and it defaults to False so every existing call site keeps its
        exact present behaviour. True un-hides the DEPRECATION limb only — not
        just at the root, but at EVERY hop — so a traversal can both START from
        and PASS THROUGH a deprecated memory. A bitemporally EXPIRED memory
        stays hidden at every value of this flag.

        Why the parameter has to live here rather than above the seam: the
        predicate is applied INSIDE each backend (a build-time row filter in
        NetworkX, a Cypher clause in FalkorDB, a SQL clause in the CTE
        fallback), so a caller had no way to express the choice at all. That is
        issue #1896: ``memory_recall(include_deprecated=True)`` returned the
        deprecated memory it was asked for and then silently dropped its
        ``graph_neighbors``, because the traversal re-applied a filter the
        caller had explicitly opted out of. MEASURED on the live graph at the
        real recall parameters (max_depth=2, min_strength=0.3): five hidden
        roots carrying 10–24 out-edges each returned 0 neighbours on ALL THREE
        backends, while visible controls returned 23–41.

        The name matches the MCP-facing parameter because the WIDTHS now match.
        The default predicate hides two independent things — a non-zero
        ``deprecated`` AND a bitemporally expired ``invalid_at`` — and this flag
        un-hides only the first, exactly as ``search_ranked`` does. An earlier
        version of this parameter was called ``include_hidden`` and un-hid both,
        which let ``memory_recall(include_deprecated=True)`` return an expired
        neighbour that its own search could never return. Widening beyond the
        search contract is a change to the PUBLIC contract, so it does not get
        to happen as a side effect of naming a store-level flag broadly.
        """
        ...

    async def centrality(
        self, db: aiosqlite.Connection, top_n: int | None
    ) -> list[tuple[str, float]]:
        """Memories ranked by betweenness centrality, descending.

        ``top_n=None`` returns the full ranking. An EMPTY graph returns ``[]``
        (zero nodes really is zero bridges); an unreachable backend — or one
        that cannot compute betweenness at all — raises
        ``GraphUnavailableError``, because this feeds the importance shield.
        """
        ...

    def invalidate(self) -> None:
        """Mark any cached projection stale; the next read rebuilds.

        Must be safe to call from a writer that holds no database handle —
        every ``memory_links`` writer calls it through a lazy import inside
        CRUD and dream paths. A backend with no cache implements this as a
        no-op.
        """
        ...
