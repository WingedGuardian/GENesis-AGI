"""CRUD for ``payment_vault_events``, the vault service's append-only audit log.

The closed event set lives here, not in a CHECK constraint (see the migration's
docstring). ``detail`` is value-free by contract: who asked (pid, start time,
process names) and why a request was refused, never a card value or the
passphrase. Rows are kept forever.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite

EVENTS = frozenset(
    {
        "service_start",
        "service_stop",
        "unlock",
        "unlock_refused",
        "lock",
        "idle_relock",
        "stale_identity_relock",
        "peer_refused",
        # Owner approval of a card release (payments/approval.py).
        "approval_requested",
        "approval_granted",
        "approval_refused",
        "approval_expired",
        # A card release (payments/release.py).
        "release_requested",
        "release_granted",
        "release_claimed",
        "release_refused",
        "release_cancelled",
    }
)
# The events that say whether the key is held; approval and release rows
# interleave with them.
KEY_EVENTS = EVENTS - {e for e in EVENTS if e.startswith(("approval_", "release_"))}


async def record_event(db: aiosqlite.Connection, event: str, detail: dict | None = None) -> None:
    """Append one event. Raises ValueError for an event outside ``EVENTS``."""
    if event not in EVENTS:
        raise ValueError(f"unknown payment vault event {event!r}")
    await db.execute(
        "INSERT INTO payment_vault_events (event, detail, created_at) VALUES (?, ?, ?)",
        (
            event,
            json.dumps(detail, sort_keys=True) if detail else None,
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )
    await db.commit()


async def list_recent(
    db: aiosqlite.Connection, limit: int = 20, *, events: frozenset[str] | None = None
) -> list[dict]:
    """The newest ``limit`` events, newest first; only ``events`` when given."""
    where, args = "", ()
    if events is not None:
        where = f"WHERE event IN ({','.join('?' * len(events))}) "
        args = tuple(sorted(events))
    cursor = await db.execute(
        "SELECT event, detail, created_at FROM payment_vault_events "  # noqa: S608 - placeholders only
        f"{where}ORDER BY id DESC LIMIT ?",
        (*args, limit),
    )
    rows = await cursor.fetchall()
    return [
        {"event": e, "detail": json.loads(d) if d else None, "created_at": c} for e, d, c in rows
    ]
