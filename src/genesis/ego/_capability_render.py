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


async def safe_count_unusable(db: aiosqlite.Connection) -> dict[str, int]:
    """Rows the window cannot use, split by cause. Never raises.

    Same fail-open contract as :func:`safe_count`: this runs alongside the
    degraded branch, so it must never be the thing that raises.
    """
    try:
        from genesis.db.crud import capability_map as cap_crud

        return await cap_crud.count_unusable(db)
    except Exception:
        logger.warning("Failed to count unusable capability rows", exc_info=True)
        return {"unreadable": 0, "future": 0}


def unusable_note(unusable: dict[str, int] | None) -> str:
    """A clause naming rows the window silently dropped, or "" if there are none.

    Rendered wherever the section renders — NOT only on the empty branch. A
    malformed row sitting alongside healthy ones is exactly as invisible and
    exactly as permanent, and it was the commoner case that went unreported
    when this was wired into the empty path alone.

    Also logs, because the prompt is read by a model and the log is read by an
    operator, and only one of them can act on it.
    """
    if not unusable:
        return ""
    unreadable, future = unusable.get("unreadable", 0), unusable.get("future", 0)
    if not (unreadable or future):
        return ""
    parts = []
    if unreadable:
        parts.append(f"{unreadable} with an unreadable timestamp")
    if future:
        parts.append(f"{future} dated in the future")
    logger.warning(
        "capability_map: %d row(s) unreadable, %d row(s) future-dated — "
        "permanently excluded from the self-model until rewritten",
        unreadable, future,
    )
    return f" Excluded and not recoverable on their own: {' and '.join(parts)}."


def empty_state_note(total: int | None, *, empty: str, filtered: str,
                     unknown: str,
                     unusable: dict[str, int] | None = None) -> str:
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
    clause = unusable_note(unusable)
    if clause:
        note = note.rstrip("\n").rstrip("*") + clause + "*\n"
    return note
