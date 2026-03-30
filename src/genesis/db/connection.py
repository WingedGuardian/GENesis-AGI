"""Database connection management for Genesis v3.

Provides async SQLite access via aiosqlite with WAL mode.
"""

from pathlib import Path

import aiosqlite

from genesis.env import genesis_db_path

DEFAULT_DB_PATH = genesis_db_path()

BUSY_TIMEOUT_MS = 5000


async def get_db(path: str | Path = DEFAULT_DB_PATH) -> aiosqlite.Connection:
    """Open a connection to the Genesis SQLite database.

    Enables WAL mode and foreign keys. Caller is responsible for closing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return db


async def init_db(path: str | Path = DEFAULT_DB_PATH) -> aiosqlite.Connection:
    """Initialize the database: create all tables, indexes, and seed data.

    Returns the open connection.
    """
    from genesis.db.schema import create_all_tables, seed_data

    db = await get_db(path)
    await create_all_tables(db)
    await seed_data(db)
    await db.commit()
    return db
