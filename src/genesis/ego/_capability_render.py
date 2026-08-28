"""Shared empty-state rendering for the capability-map prompt sections.

Three builders render this map, and they drifted: the same message was fixed in
one and left stale in the others, twice. Keeping the decision here means there
is one place to be right rather than three places to keep in step.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def safe_count(db: aiosqlite.Connection) -> int | None:
    """Total rows in the map, or ``None`` if the count itself failed.

    This runs on the branch that exists to survive a failed read, so it must not
    raise: an unguarded second query here turns a section that would have
    degraded to one italic line into an exception that aborts the whole ego
    cycle (nothing between here and ``assemble_context`` catches per-section
    errors).
    """
    try:
        from genesis.db.crud import capability_map as cap_crud

        return await cap_crud.count_all(db)
    except Exception:
        logger.warning("Failed to count capability map rows", exc_info=True)
        return None


def empty_state_note(total: int | None, *, empty: str, filtered: str,
                     unknown: str) -> str:
    """Pick the sentence that is TRUE for this state.

    Three distinct states, three sentences: the map is genuinely empty; the map
    holds rows that were all filtered out; or the count could not be read. Each
    message is a false claim in the other two situations, which is why the
    choice is made once, here.
    """
    if total is None:
        return unknown
    if total == 0:
        return empty
    return filtered.format(total=total)
