"""Data-migration framework (WS-C).

Once-per-install backfills/transforms of NON-schema state — Qdrant payloads,
entity graphs — that schema migrations (``db/migrations/``) can't express.
Each ``dNNNN_*.py`` module exposes:

    requires_operator: bool = False   # optional; True => auto-runner skips it
    def migrate() -> dict:  ...        # sync, idempotent; returns a summary
    def verify() -> bool:   ...        # sync; True iff the goal is now satisfied

The runner (``runner.py``) claims each migration's ledger row atomically,
runs ``migrate()`` then ``verify()`` off the event loop, and records
completed/failed. Idempotency is each migration's own contract — a re-run on
an already-migrated install must be a clean no-op (this is how a lagging peer
install self-heals on its next pull+restart, with no control plane).

Migrations run POST-boot on their own sync connection, CONCURRENTLY with the
live server. SQLite (WAL) has a single writer, and the server waits only 5s for
the write lock — so a bulk migration that loops over many rows in ONE
transaction starves every server write for the loop's duration (regression:
d0006 held it ~13s, #1179). **A migration that writes more than a handful of
rows in a loop MUST drive its writes through ``_util.commit_in_batches`` (commits
per batch, releasing the lock between them) rather than a single trailing
``commit()``.** Do any slow per-row work (disk/network I/O) BEFORE opening the
write connection (see d0005/d0006 for the read-first-then-batched-write shape).
"""
