"""CRUD for ``tool_call_outcomes`` — read side.

The table is written out-of-process by the CC hook script
(``scripts/edit_failure_sensor.py``, raw sqlite3 by necessity — it runs
outside the server). In-process readers go through here.
"""

from __future__ import annotations

import aiosqlite


async def aggregate_success_rates(
    db: aiosqlite.Connection, *, since: str | None = None
) -> list[dict]:
    """Per-tool call counts and success base rates, optionally windowed.

    ``since`` is a canonical-ISO floor compared sargably against the stored
    timestamps (both sides come from ``datetime.isoformat()``; a
    microsecond-less row sorts sub-second early at the boundary — negligible
    on day-scale windows). Rides ``idx_tco_tool_ts``.
    """
    if since is None:
        cursor = await db.execute(
            "SELECT tool_name, COUNT(*) AS n, AVG(success) AS base_rate "
            "FROM tool_call_outcomes GROUP BY tool_name"
        )
    else:
        cursor = await db.execute(
            "SELECT tool_name, COUNT(*) AS n, AVG(success) AS base_rate "
            "FROM tool_call_outcomes WHERE timestamp >= ? GROUP BY tool_name",
            (since,),
        )
    return [dict(r) for r in await cursor.fetchall()]
