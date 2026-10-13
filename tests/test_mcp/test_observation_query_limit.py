"""observation_query must not turn a bounded page into the whole table.

SQLite reads a negative LIMIT as "no limit", so the tool refuses a limit below 1
before it reaches the query.
"""

from __future__ import annotations

import pytest

from genesis.mcp.memory.observations import observation_query


@pytest.mark.parametrize("limit", [-1, 0, True, "5"])
async def test_invalid_limit_is_refused_before_the_query(limit):
    result = await observation_query.fn(limit=limit)
    assert result == [{"error": "limit must be an integer of 1 or more"}]
