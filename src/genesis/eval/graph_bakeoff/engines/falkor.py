"""FalkorDB contender — S1 STUB (full adapter is S2).

FalkorDB via ``falkordblite`` runs FULLY EMBEDDED: the package ships a patched
``redislite`` (embedded redis) plus the compiled ``falkordb.so`` module, driven
through ``from redislite import FalkorDB`` — no external server to provision. S1
smoke PROVEN 2026-08-06 on this box: install + embedded start + 3-node build +
1-hop Cypher round-trip all pass.

S2 fleshes out ``load``/``run`` as a throwaway-venv subprocess (same bridge as the
ladybug engine). FalkorDBLite is the S2/S3 workhorse; the lite->server escalation
gates (capability parity, perf plausibility band, decision sensitivity) are S3.
"""

from __future__ import annotations

from ..base import LoadStats, QueryResult, module_available
from ..queries import QuerySpec

# falkordblite patches `redislite` to expose FalkorDB; the import-name probe is
# `redislite` (the `falkordblite` dist has no top-level python module of its own).
_MODULE = "redislite"


class FalkorEngine:
    name = "falkor"

    @classmethod
    def available(cls) -> bool:
        # find_spec("redislite") is only meaningful in the throwaway venv; False in
        # the prod venv -> harness skips until the S2 subprocess bridge lands.
        return module_available(_MODULE)

    async def load(self, snapshot_path: str) -> LoadStats:  # pragma: no cover - S2
        raise NotImplementedError("FalkorEngine.load is S2 (subprocess bridge)")

    async def run(self, query: QuerySpec, params: dict) -> QueryResult:  # pragma: no cover - S2
        raise NotImplementedError("FalkorEngine.run is S2 (subprocess bridge)")

    def stats(self) -> dict:
        return {"engine": self.name, "status": "S1-stub", "smoke": "passed 2026-08-06"}
