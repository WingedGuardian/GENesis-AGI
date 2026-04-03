"""Genesis database schema — split into DDL definitions and runtime migrations.

Public API (backward-compatible with the old single-file schema.py):
- TABLES, INDEXES: DDL definitions
- create_all_tables(): creates tables, runs migrations, creates indexes
- seed_data(): inserts initial seed data
"""

from genesis.db.schema._migrations import create_all_tables, seed_data
from genesis.db.schema._tables import INDEXES, TABLES

__all__ = ["TABLES", "INDEXES", "create_all_tables", "seed_data"]
