"""Add capability_shadow_events — the WS5 Discord capability-gate SHADOW store.

Observe-only: at each autonomous Discord egress door (the outreach pipeline
``_deliver``, the ``outreach_poll`` webhook, and the discord-bot ``send_reply``
API call) Genesis records what a capability gate WOULD decide (hold vs allow)
WITHOUT actually holding. This gathers volume + pattern data before the ENFORCE
stage turns the gate on; there is NO behavioural change to any send.

Firewall/privacy: rows are gate DECISIONS + routing refs + a BOUNDED excerpt
(<=200 chars of otherwise-public Discord content) + a hash over the FULL content.
``content_preview`` and ``content_hash`` are NOT paired — the hash is over the
full text, the preview is a truncated excerpt. Additive + idempotent; the canonical
DDL is mirrored in ``db/schema/_tables.py`` for the fresh-DB path. Individual
``db.execute()`` calls (never ``executescript``) so a concurrent
``CREATE TABLE IF NOT EXISTS`` from a subprocess writer stays serialized under WAL.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_shadow_events (
            id               TEXT PRIMARY KEY,
            observed_at      TEXT NOT NULL,      -- ISO8601 UTC — when the send was observed
            path             TEXT NOT NULL,      -- egress door: deliver | poll | reply
            channel          TEXT NOT NULL,      -- 'discord'
            cell_domain      TEXT NOT NULL,      -- capability cell domain ('discord')
            cell_verb        TEXT NOT NULL,      -- send | poll | reply
            cell_risk_class  TEXT NOT NULL,      -- bulk | standard | identity
            cell_state       TEXT,               -- capability_grants.state at observation; NULL => cell not yet created (not_determined)
            would_hold       INTEGER NOT NULL,   -- 1 = a live gate WOULD hold this send; 0 = would allow (GRANTED cell)
            target           TEXT,               -- routing target (webhook name / channel_id / recipient) — NOT content
            content_preview  TEXT,               -- truncated excerpt (<=200 chars); NOT paired with content_hash
            content_hash     TEXT                -- hash over the FULL content (analysis/dedup); != content_preview
            -- SHADOW/OBSERVE-ONLY: gate DECISIONS + refs only. No hold/approval is
            -- created here; the send always proceeds. Not read by any cognition job.
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_shadow_events_observed_at "
        "ON capability_shadow_events(observed_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_shadow_events_cell "
        "ON capability_shadow_events(cell_domain, cell_verb, cell_risk_class)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS capability_shadow_events")
