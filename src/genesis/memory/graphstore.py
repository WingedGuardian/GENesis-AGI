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

Implementations are structural — plain classes, no inheritance — mirroring
``EmbeddingBackend`` in ``memory/embeddings.py``, the closest sibling in this
package (a protocol, a companion unavailable-error, and a chain of concrete
non-subclassing backends).
"""

from __future__ import annotations

from dataclasses import dataclass
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
    ) -> list[GraphNode]:
        """Neighbours reachable from ``root_id``.

        Ordered ``(depth, -strength)``. A root that is absent from the graph
        yields ``[]`` — that is genuinely "no neighbours", not unavailability.
        Raises ``GraphUnavailableError`` if the backend cannot be reached.
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
