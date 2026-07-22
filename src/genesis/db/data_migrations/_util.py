"""Shared helpers for data migrations (WS-C).

The one piece here — :func:`commit_in_batches` — exists to keep bulk write/delete
migrations from starving the live server's writers.

Data migrations run POST-boot, on their own sync sqlite connection, CONCURRENTLY
with the running server. SQLite (WAL) allows exactly one writer at a time, and
the server's shared connection waits only ``BUSY_TIMEOUT_MS`` (5s) for the write
lock before failing with ``database is locked``. A migration that loops over many
rows inside a SINGLE transaction holds that lock for the whole loop — so a large
bulk migration blocks every server write, AND the runner's own ledger bookkeeping
(``mark_completed`` on the shared connection), for the loop's full duration.

Regression this prevents: d0006 (surplus ops-telemetry purge) deleted 256 units ×
~7 cross-store rows in one ~13s transaction; the live server logged a burst of
``database is locked`` and its ``mark_completed`` failed, leaving the ledger row
``running`` until a restart re-ran the (idempotent) migration.

Committing every ``batch_size`` items releases the write lock between batches so
concurrent server writes interleave — each batch is far under the 5s ceiling.
Crash-safety is unchanged for idempotent migrations: a crash mid-run leaves the
un-applied items for the next idempotent retry (better durability granularity,
not worse).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence

from genesis.db.connection import MIGRATION_BUSY_TIMEOUT_MS

logger = logging.getLogger(__name__)

# 100 keeps each committed batch comfortably under the server's 5s busy_timeout
# even for a fan-out migration (e.g. d0006's ~7 DELETEs/item ⇒ ~700 statements
# per batch, sub-second), while amortizing commit/fsync overhead across items.
DEFAULT_BATCH_SIZE = 100


def commit_in_batches[T](
    conn: sqlite3.Connection,
    items: Sequence[T],
    apply: Callable[[sqlite3.Connection, T], None],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Apply ``apply(conn, item)`` for each item, committing every ``batch_size``.

    Sets a generous ``busy_timeout`` on ``conn`` (``MIGRATION_BUSY_TIMEOUT_MS``)
    so the migration itself waits out a brief concurrent server write instead of
    failing, then commits after every ``batch_size`` items (and once more for the
    final partial batch) so the WAL write lock is never held across the whole
    loop. Returns the number of items applied.

    ``apply`` must NOT commit — batching owns transaction boundaries. It may skip
    an item's writes internally (e.g. a pre-step failed); the item still counts
    toward ``batch_size`` pacing and the return total. ``batch_size`` is clamped
    to ``>= 1``.
    """
    batch_size = max(1, batch_size)
    conn.execute(f"PRAGMA busy_timeout={MIGRATION_BUSY_TIMEOUT_MS}")
    applied = 0
    for item in items:
        apply(conn, item)
        applied += 1
        if applied % batch_size == 0:
            conn.commit()
    conn.commit()  # flush the final (possibly partial) batch
    logger.debug("commit_in_batches applied %d items (batch_size=%d)", applied, batch_size)
    return applied
