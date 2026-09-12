"""Common engine interface + result types for the bake-off.

Every engine (nx_incremental control, ladybug, falkor) implements ``Engine``.
``available()`` uses ``importlib.util.find_spec`` so importing this package in the
prod venv / CI (where the contender libs are absent) never breaks collection —
the engine simply reports unavailable.

Parity is compared on ``node_ids`` (a frozenset) for exact_set queries; ``ranking``
(ordered ids) for topk_spearman; ``load_ms`` for timing. The engine returns raw
results; scoring/normalization lives in ``parity.py`` and ``report.py``.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .queries import QuerySpec


@dataclass
class LoadStats:
    """Result of loading the snapshot into an engine's queryable form."""

    engine: str
    nodes: int
    edges: int
    load_ms: float
    rss_mb: float | None = None


@dataclass
class QueryResult:
    """One engine's answer to one query."""

    engine: str
    query_id: str
    node_ids: frozenset[str] = field(default_factory=frozenset)  # exact_set queries
    ranking: list[str] = field(default_factory=list)  # topk_spearman queries
    scalar_ms: float | None = None  # timing queries (q8) carry the measurement here
    run_ms: float = 0.0  # wall-clock of THIS query (warm)
    cardinality: int = 0
    loc: int | None = None  # lines-of-code to express the query (expressiveness dim)
    notes: str = ""


@runtime_checkable
class Engine(Protocol):
    """Minimal contract every candidate implements. Contender engines run in a
    throwaway venv subprocess (S2); the NX control runs in-process (prod venv)."""

    name: str

    @classmethod
    def available(cls) -> bool: ...

    async def load(self, snapshot_path: str) -> LoadStats: ...

    async def run(self, query: QuerySpec, params: dict) -> QueryResult: ...

    def stats(self) -> dict: ...


def module_available(module_name: str) -> bool:
    """True if ``module_name`` is importable in the CURRENT interpreter — via
    find_spec, so it never imports (or fails) the module itself."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False
