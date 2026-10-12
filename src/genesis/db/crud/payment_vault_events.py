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
    }
)


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


async def list_recent(db: aiosqlite.Connection, limit: int = 20) -> list[dict]:
    """The newest ``limit`` events, newest first."""
    cursor = await db.execute(
        "SELECT event, detail, created_at FROM payment_vault_events ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {"event": e, "detail": json.loads(d) if d else None, "created_at": c} for e, d, c in rows
    ]
