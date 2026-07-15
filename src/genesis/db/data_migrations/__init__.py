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
"""
