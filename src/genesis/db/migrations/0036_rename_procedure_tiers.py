"""Rename procedure activation tiers L1-L4 -> CORE/ADVISORY/LIBRARY/DORMANT.

The procedural-memory tiers were named *backwards* vs every other ``L#`` ladder
in Genesis: ``_TIER_RANK = {"L1":4,"L2":3,"L3":2,"L4":1}`` made **L1 the most-
proven** and L4 the unproven draft. This one-time migration rewrites the values
to self-documenting names (CORE = most-proven, DORMANT = unproven draft) so the
rank map reads ``{"CORE":4 … "DORMANT":1}``.

Two value-only rewrites, no schema change (the column is ``TEXT`` with no CHECK
constraint, so the index reindexes the new values automatically):

  1. ``activation_tier`` column  — a CASE rewrite.
  2. ``promotion_history`` JSON  — the embedded ``from_tier``/``to_tier`` values
     in the audit trail, so the stored history matches the renamed column.
     Non-tier values (e.g. ``"quarantined"``) are preserved verbatim.

Idempotent: a re-run finds no remaining ``L#`` values (CASE falls through to
ELSE; the JSON pass marks nothing changed), so applying twice is a no-op. The
code rename (defaults, ``_TIER_RANK``, query literals) ships in the same PR, so
new rows are written with the new names from the moment this lands.

Rollback path: revert the PR and apply a values-reversed migration; this file
has no ``down()`` (matches the migration-set norm; the rename is reversible by a
symmetric forward migration).
"""

from __future__ import annotations

import json

import aiosqlite

# L1 = most-proven (top of ladder) -> CORE; L4 = unproven draft -> DORMANT.
_TIER_MAP = {"L1": "CORE", "L2": "ADVISORY", "L3": "LIBRARY", "L4": "DORMANT"}


async def up(db: aiosqlite.Connection) -> None:
    # The runner applies migrations against a bare DB in its own unit tests,
    # where base tables (created by `create_all_tables` in production) are
    # absent. Skip cleanly rather than fail the apply-all sequence. (Mirrors
    # 0035 / 0013.)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='procedural_memory'"
    )
    if not await cursor.fetchone():
        return

    # 1. activation_tier column — value-only CASE rewrite. ELSE keeps any
    #    already-renamed value, making a re-run a no-op.
    await db.execute(
        """
        UPDATE procedural_memory
        SET activation_tier = CASE activation_tier
            WHEN 'L1' THEN 'CORE'
            WHEN 'L2' THEN 'ADVISORY'
            WHEN 'L3' THEN 'LIBRARY'
            WHEN 'L4' THEN 'DORMANT'
            ELSE activation_tier
        END
        """
    )

    # 2. promotion_history JSON — rewrite the embedded from_tier/to_tier tier
    #    values so the audit trail matches the renamed column. Only L1-L4 are
    #    mapped; non-tier values (e.g. "quarantined") pass through untouched.
    cursor = await db.execute(
        "SELECT id, promotion_history FROM procedural_memory "
        "WHERE promotion_history IS NOT NULL"
    )
    for row in await cursor.fetchall():
        pid, raw = row[0], row[1]
        try:
            history = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue  # malformed/legacy blob — leave it as-is
        if not isinstance(history, list):
            continue

        changed = False
        for entry in history:
            if not isinstance(entry, dict):
                continue
            for key in ("from_tier", "to_tier"):
                mapped = _TIER_MAP.get(entry.get(key))
                if mapped is not None:
                    entry[key] = mapped
                    changed = True

        if changed:
            await db.execute(
                "UPDATE procedural_memory SET promotion_history = ? WHERE id = ?",
                (json.dumps(history), pid),
            )
