"""Tests for L1 wing-stats canonical filtering (essential_knowledge._wing_stats).

#17: the L1 "Wings" view must only surface controlled-vocabulary wings, so a
leaked ``wing=channels`` prefix or a one-off LLM-invented wing doesn't pollute
the display.
"""

from __future__ import annotations

import pytest

from genesis.db.crud.memory import create_metadata
from genesis.memory.essential_knowledge import _wing_stats


@pytest.mark.asyncio()
async def test_wing_stats_filters_to_canonical_vocab(empty_db):
    rows = [
        ("m1", "channels"),
        ("m2", "channels"),
        ("m3", "memory"),
        ("m4", "wing=channels"),  # leaked prefix — malformed
        ("m5", "technology"),     # LLM-invented, off-vocabulary
    ]
    for mid, wing in rows:
        await create_metadata(
            empty_db, memory_id=mid,
            created_at="2026-06-20T00:00:00+00:00", wing=wing,
        )

    stats = await _wing_stats(empty_db)

    assert stats == {"channels": 2, "memory": 1}
    assert "wing=channels" not in stats
    assert "technology" not in stats
