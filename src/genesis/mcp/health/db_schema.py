"""db_schema tool — query database table and column information.

Helps CC sessions discover the DB schema before querying, avoiding
trial-and-error PRAGMA calls and wrong column name guesses.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


async def _impl_db_schema(table: str = "") -> dict:
    """Return DB schema: table list or column details for a specific table.

    Args:
        table: If empty, returns list of all table names.
               If provided, returns column details for that table.
    """
    import genesis.mcp.health as health_mod

    _service = health_mod._service
    if _service is None or _service._db is None:
        return {"error": "DB not available"}

    db = _service._db

    if table:
        # Validate table name to prevent injection (PRAGMA doesn't support params)
        if not table.replace("_", "").isalnum():
            return {"error": f"Invalid table name: {table}"}
        cursor = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608
        rows = await cursor.fetchall()
        if not rows:
            return {"error": f"Table not found: {table}"}
        columns = [
            {
                "name": r[1],
                "type": r[2],
                "notnull": bool(r[3]),
                "default": r[4],
                "pk": bool(r[5]),
            }
            for r in rows
        ]
        return {"table": table, "columns": columns, "column_count": len(columns)}

    # List all tables
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    )
    tables = [r[0] for r in await cursor.fetchall()]
    return {"tables": tables, "count": len(tables)}


@mcp.tool()
async def db_schema(table: str = "") -> dict:
    """Query database schema: list all tables, or get columns for a specific table.

    Examples:
      db_schema()                → list of all table names
      db_schema(table="events")  → column names, types, constraints for 'events'
    """
    return await _impl_db_schema(table)
