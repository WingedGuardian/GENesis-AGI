"""LadybugDB contender — S1 STUB (full adapter is S2).

LadybugDB is the community successor to Kùzu (Kùzu Inc. acquired by Apple, Oct
2025; repo archived). Embedded, schema-first, Kùzu-API (``ladybug.Database`` +
``ladybug.Connection`` + Cypher). S1 smoke PROVEN 2026-08-06 on this box (py3.12/
LXC): install + import + 3-node build + 1-hop query round-trip all pass.

S2 fleshes out ``load``/``run``: because ``ladybug`` lives in the throwaway venv
(not the prod venv), the harness drives it as a SUBPROCESS — this class emits the
per-query Cypher + normalization; the runner shells to
``~/tmp/graph-bakeoff/venv/bin/python`` with ``PYTHONPATH`` bridged, exchanging
JSON node-id sets. The as-of query (q3) is the first Cypher written in S2 (the
riskiest expressibility unknown).
"""

from __future__ import annotations

from ..base import LoadStats, QueryResult, module_available
from ..queries import QuerySpec

_MODULE = "ladybug"


class LadybugEngine:
    name = "ladybug"

    @classmethod
    def available(cls) -> bool:
        # False in the prod venv (module is throwaway-venv only) -> harness skips
        # until the S2 subprocess bridge lands.
        return module_available(_MODULE)

    async def load(self, snapshot_path: str) -> LoadStats:  # pragma: no cover - S2
        raise NotImplementedError("LadybugEngine.load is S2 (subprocess bridge)")

    async def run(self, query: QuerySpec, params: dict) -> QueryResult:  # pragma: no cover - S2
        raise NotImplementedError("LadybugEngine.run is S2 (subprocess bridge)")

    def stats(self) -> dict:
        return {"engine": self.name, "status": "S1-stub", "smoke": "passed 2026-08-06"}
