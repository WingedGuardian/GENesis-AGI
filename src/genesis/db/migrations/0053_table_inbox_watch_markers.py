"""Re-classify existing inbox WATCH/BOOKMARK follow-ups into the tabled lane.

Inbox evaluation used to route WATCH/BOOKMARK recommendations into the
actionable ``ego_judgment`` lane, where they piled up forever — the ego never
elects to "close a watch marker", so only dedup ever drained them. They are
attention markers, not tasks, and now route to ``kind='tabled'`` at creation
time (see inbox/monitor.py ``_ACTION_MAP``). This migration moves the rows that
were already created under the old behaviour so they stop polluting the
actionable ledger + reports.

Keyed on the content prefix (``[WATCH]``/``[BOOKMARK]``) as belt-and-suspenders
in addition to source — verified against the live DB to catch exactly the
WATCH/BOOKMARK markers and zero ADOPT/ADAPT/EXPLORE rows. Only NON-TERMINAL rows
are moved (``status NOT IN ('completed', 'failed')`` — i.e. pending / blocked /
in_progress / scheduled), the exact set ``decay_stale_inbox_markers`` reaps, so
every moved row has a reaper. Terminal completed/failed rows keep their history
(and, being terminal with a completed_at, are handled by ``purge_completed``).
Idempotent: re-running is a no-op once the rows are already tabled.
"""

from __future__ import annotations

import aiosqlite

_WHERE = (
    "source = 'inbox_evaluation' "
    "AND status NOT IN ('completed', 'failed') "
    "AND (content LIKE '[WATCH]%' OR content LIKE '[BOOKMARK]%')"
)


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        f"UPDATE follow_ups SET kind = 'tabled' "  # noqa: S608 - _WHERE is a module constant, no user input
        f"WHERE {_WHERE} AND kind = 'follow_up'"
    )


async def down(db: aiosqlite.Connection) -> None:
    # Intentional no-op. This is a one-way data reclassification: once the new
    # _ACTION_MAP creates WATCH/BOOKMARK rows natively as kind='tabled', a blanket
    # `kind='follow_up'` revert could not tell those apart from the rows this
    # migration moved, and would wrongly promote user-curated markers back into
    # the actionable lane. Leaving the reclassification in place is the safe
    # reverse (matches the no-op-down convention of other data-only migrations).
    pass
