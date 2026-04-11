"""Schema migration framework — versioned, auto-detecting, idempotent.

Complements the existing inline migrations in genesis.db.schema._migrations
(which handle ALTER TABLE and table rebuilds). This framework handles
new DDL migrations that should be tracked and versioned.

Usage:
    python -m genesis.db.migrations --apply     # Run pending migrations
    python -m genesis.db.migrations --dry-run   # Show pending without applying
    python -m genesis.db.migrations --status    # Show applied/pending status
"""

from genesis.db.migrations.runner import MigrationRunner

__all__ = ["MigrationRunner"]
