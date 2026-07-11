"""Add immunity_shadow_events — the WS-3 B1 immunity-gate SHADOW store.

Observe-only: at each recall/inject site where external-world content reaches
an action-capable LLM prompt, Genesis records what a provenance gate WOULD
decide (block vs allow) WITHOUT altering the recall — the item still reaches
the prompt exactly as before (already wrapped by ``wrap_external_recall``).
This gathers volume + per-site data before the ENFORCE stage (B4) turns any
gate on; there is NO behavioural change to any recall.

Firewall/privacy: rows are gate DECISIONS + provenance refs only — the gate,
the site (``source_ref``), the ``origin_class``, and a freeform ``detail``
(e.g. a blockable-item count). NEVER recalled content. owner/first_party
origins are never blockable, so a row is only ever written for
external_untrusted content (the never-block invariant lives in
``security.immunity.is_blockable``).

Additive + idempotent; the canonical DDL is mirrored in
``db/schema/_tables.py`` for the fresh-DB path. Individual ``db.execute()``
calls (never ``executescript``) so a concurrent ``CREATE TABLE IF NOT EXISTS``
from a subprocess writer stays serialized under WAL. No commit — the runner
owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS immunity_shadow_events (
            id            TEXT PRIMARY KEY,
            observed_at   TEXT NOT NULL,      -- ISO8601 UTC — when the recall/inject was observed
            gate          TEXT NOT NULL,      -- procedure | identity | autonomy | injection
            mode          TEXT NOT NULL,      -- shadow | enforce (mode at observation; 'off' never records)
            origin_class  TEXT NOT NULL,      -- blockable origin (external_untrusted for gate 4); a row is written only when the DERIVED origin is blockable
            would_block   INTEGER NOT NULL,   -- 1 = a live gate WOULD block; kept uniform for gates 1-3 forward-compat
            source_kind   TEXT,               -- site class: recall_inject | proactive_hook | ...
            source_ref    TEXT,               -- the site: 'mcp/memory/core.py::memory_recall'
            detail        TEXT,               -- freeform (e.g. blockable item count); NEVER recalled content
            process       TEXT                -- server | proactive_hook | outreach_mcp | ...
            -- WS-3 immunity SHADOW store: gate DECISIONS + provenance refs only. Observe-only
            -- — no recall is blocked or altered here; the item still reaches the prompt
            -- (wrapped as it already was). Never stores recalled content. Not read by any
            -- cognition job.
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_immunity_shadow_events_observed_at "
        "ON immunity_shadow_events(observed_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_immunity_shadow_events_gate "
        "ON immunity_shadow_events(gate, observed_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS immunity_shadow_events")
