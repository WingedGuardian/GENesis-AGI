"""WS-8 deploy-state seed (Option B) — pre-grant the lowest-risk email cell.

On the email gate going live, the lowest-risk capability cell
``email:send:standard`` (a reply to a thread that already has an inbound from a
known recipient) starts GRANTED, so routine replies keep flowing autonomously
while cold outreach, bulk/campaign, and financial sends hold for owner
approval.  OUTREACH autonomy carries no earned per-cell evidence, so this is a
deliberate trust grant for the safest class, not a derived one.

``INSERT OR IGNORE`` makes it a one-time default that never overrides a cell the
owner has already set (e.g. revoked).  Depends on the ``capability_grants``
table from migration 0030 (always present here: ordered migrations on existing
installs, ``create_all_tables`` on fresh installs).
"""

from __future__ import annotations

import aiosqlite

_SEED = """
    INSERT OR IGNORE INTO capability_grants
        (id, domain, verb, risk_class, state, granted_at, created_at, updated_at)
    VALUES ('email:send:standard', 'email', 'send', 'standard', 'granted',
            datetime('now'), datetime('now'), datetime('now'))
"""


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_SEED)


async def down(db: aiosqlite.Connection) -> None:
    """Remove the seeded grant — development/testing only.  Only removes the
    row if it is still exactly the seeded default (untouched 'granted')."""
    await db.execute(
        "DELETE FROM capability_grants "
        "WHERE id = 'email:send:standard' AND state = 'granted' "
        "AND successes = 0 AND corrections = 0"
    )
