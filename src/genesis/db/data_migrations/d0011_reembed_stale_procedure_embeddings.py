"""d0011 — re-embed stale procedure principle_embeddings (pre-#1277 repair).

Auto-runs on boot. Repairs ``procedural_memory`` rows whose ``principle_embedding``
still describes an OLDER principle than the row's current text — the residue of
the pre-#1277 refine path, which overwrote a matched row's principle but left its
embedding untouched. On a ``version > 1`` row refined repeatedly, that stale
vector defeats the #1277 embedding-cosine identity check (a distinct incoming
lesson can mis-match the stale vector and overwrite the row). #1277 fixed the
mechanism going forward; this heals the rows it already left stale.

On an install that never ran the pre-fix path (or has no ``version > 1`` rows)
this is a clean no-op — the whole point of the data-migration framework: a
lagging install self-heals on its next pull+restart with no control plane.

migrate()/verify() are SYNC (blocking SQLite + network embed I/O); the runner
offloads them via ``asyncio.to_thread``. The heal logic drives the async embedder
via ``asyncio.run`` from that worker thread and opens its OWN sqlite connections —
never the runtime's async ``rt._db``.
"""

from __future__ import annotations

import logging
import sqlite3

from genesis.db.data_migrations.stale_embedding_repair import (
    count_stale_procedure_embeddings,
    reembed_stale_procedure_embeddings,
)
from genesis.env import genesis_db_path

logger = logging.getLogger(__name__)

requires_operator = False


def migrate() -> dict:
    """Re-embed stale version>1 procedure embeddings (see module docstring).

    Fail-closed: raises if the embedder is unavailable, so the runner marks the
    migration failed and retries next boot rather than recording success with
    rows left stale. Returns per-run counts; a clean ``reembedded: 0`` where
    nothing was stale."""
    return reembed_stale_procedure_embeddings(genesis_db_path())


def verify() -> bool:
    """Complete when no version>1 row's stored embedding disagrees with a fresh
    embed of its current principle.

    Reached only after a successful ``migrate()`` (same run), so the embedder is
    normally still up. If it went away in between, treat as not-done (retry next
    boot) rather than raising."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        return count_stale_procedure_embeddings(db) == 0
    except RuntimeError:
        logger.warning("d0011 verify: embedder unavailable — deferring to next boot")
        return False
    finally:
        db.close()
