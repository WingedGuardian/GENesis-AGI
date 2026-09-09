"""Build the FalkorDB projection of the memory graph. Explicit, not scheduled.

    python -m genesis.memory.graphstore_project

Why this exists as its own entrypoint: selecting the falkordb store with no
projection built makes EVERY root absent from the graph, and the seam is
entitled to read an absent root as "no neighbours" rather than as a failure.
`FalkorGraphStore.traverse` refuses that reading (it raises when the graph holds
no nodes at all), but a store nothing can populate would be unusable — so the
lever's documented precondition needs something that can actually satisfy it.

Automating this is F3's job (debounced on the `memory_links` dirty signal, with
a generation watermark). Until then a projection is a deliberate act, and this
is it. Safe to re-run: the projection is built under a staging key and swapped
in atomically, so readers see the previous projection until the new one is
complete, and never a partial graph.

Reads SQLite read-only via `mode=ro` — WAL-aware, so a projection started while
the server is writing still sees committed rows. Never `immutable=1`, which
ignores the -wal and would silently project a stale snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from urllib.parse import quote

from genesis.env import genesis_db_path
from genesis.memory.graphstore import GraphUnavailableError
from genesis.memory.graphstore_falkor import GRAPH_KEY, FalkorGraphStore


async def build(graph_key: str = GRAPH_KEY) -> dict[str, int]:
    """Project the live graph. Returns the projection's own counts."""
    import aiosqlite

    db_path = genesis_db_path()
    store = FalkorGraphStore(graph_key=graph_key)
    # Percent-encode the path before it becomes a URI. `?` and `#` are
    # URI-significant, so a raw interpolation lets SQLite read everything after
    # one as a query string or fragment: a database at `.../memory?copy.db`
    # would silently open `.../memory` instead — the wrong file, with no error.
    # `safe="/"` keeps the separators. Same shape as `inbox/writer.py`.
    #
    # The sweep is issue #1872, not this PR: `connect(f"file:{path}?mode=ro")`
    # without quoting is the prevailing pattern here — MEASURED 2026-09-08, 34
    # sites in `src/genesis/` across 24 files, plus 12 in `scripts/`. (An
    # earlier revision of this comment said "~20", read off a truncated grep;
    # the count above is the full one.) This fixes the instance this PR
    # introduced rather than shipping a new member of a known class.
    db = await aiosqlite.connect(f"file:{quote(str(db_path), safe='/')}?mode=ro", uri=True)
    try:
        return await store.project(db)
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--graph-key",
        default=GRAPH_KEY,
        help=f"graph key to project into (default: {GRAPH_KEY})",
    )
    args = parser.parse_args()

    started = time.monotonic()
    try:
        stats = asyncio.run(build(args.graph_key))
    except GraphUnavailableError as exc:
        # Both failures here are operator problems with operator fixes rather
        # than stack traces — but they are DIFFERENT problems, and one message
        # for both sends half the readers to the wrong one. Caught by running
        # this: the client is a NEW core dependency, so on any install that has
        # not reinstalled yet the real failure is a missing library, while the
        # message was asking whether a service was running.
        print(f"cannot project: {exc}")
        if "not importable" in str(exc):
            print("  the falkordb client is missing from this environment.")
            print("  reinstall dependencies:  ./scripts/bootstrap.sh")
        else:
            print("  the engine is not reachable over its socket.")
            print("  check the service:  systemctl --user status genesis-falkordb")
        return 1
    elapsed = time.monotonic() - started
    print(
        f"projected {stats['nodes']:,} nodes / {stats['edges']:,} edges "
        f"into {args.graph_key!r} in {elapsed:.2f}s "
        f"({stats['hidden']:,} of those nodes are currently hidden by the "
        f"validity predicate, which is applied per-read, not at projection time)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
