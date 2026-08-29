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


async def safe_count_unusable(db: aiosqlite.Connection) -> int:
    """Rows the window cannot classify, or 0 if the count itself failed.

    Same fail-open contract as :func:`safe_count` and for the same reason: this
    runs on the degraded branch, so it must never be the thing that raises.
    """
    try:
        from genesis.db.crud import capability_map as cap_crud

        return await cap_crud.count_unusable(db)
    except Exception:
        logger.warning("Failed to count unusable capability rows", exc_info=True)
        return 0


def empty_state_note(total: int | None, *, empty: str, filtered: str,
                     unknown: str, unusable: int = 0) -> str:
    """Pick the sentence that is TRUE for this state.

    Four distinct states, four sentences: the map is genuinely empty; the map
    holds rows that were all filtered out; some of those rows were filtered
    because their timestamp is MALFORMED rather than merely old or thin; or the
    count could not be read. Each message is a false claim in the others'
    situations, which is why the choice is made once, here.

    The malformed case is called out separately because it is the only one that
    is a BUG rather than a normal state, and because it is otherwise permanent
    and invisible — nothing rewrites such a row, so it never recovers on its
    own. Telling the ego "stale or thin" when the truth is "corrupt" is the
    same class of false statement this helper exists to prevent.
    """
    if total is None:
        return unknown
    if total == 0:
        return empty
    note = filtered.format(total=total)
    if unusable:
        logger.warning(
            "capability_map: %d row(s) have an unusable updated_at and are "
            "permanently excluded from the self-model", unusable,
        )
        note = note.rstrip("\n").rstrip("*") + (
            f" — {unusable} of them have an unreadable timestamp.*\n"
        )
    return note
