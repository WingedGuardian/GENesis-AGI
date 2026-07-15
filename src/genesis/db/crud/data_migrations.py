"""CRUD for the ``data_migrations`` ledger (WS-C data-migration framework).

The runner (``db/data_migrations/runner.py``) drives the state machine through
these; nothing else writes the table. All writes go through the shared
``SerializedConnection`` so the atomic-claim ``UPDATE`` is genuinely atomic
against a concurrent runner.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def ensure_row(
    db: aiosqlite.Connection, *, id: str, name: str, requires_operator: bool
) -> None:
    """Insert the migration's ledger row if absent (idempotent).

    A brand-new migration starts ``operator_pending`` when it requires an
    operator (the auto-runner never claims it) else ``pending``. Existing rows
    are left untouched — status is the runner's to advance."""
    status = "operator_pending" if requires_operator else "pending"
    await db.execute(
        "INSERT OR IGNORE INTO data_migrations (id, name, status, updated_at) VALUES (?, ?, ?, ?)",
        (id, name, status, _now()),
    )
    await db.commit()


async def reset_running_to_pending(db: aiosqlite.Connection) -> int:
    """Reset orphaned ``running`` rows to ``pending`` (boot re-dispatch).

    A row left ``running`` at boot was orphaned by a crash mid-migration — the
    server and bridge-fallback never run together (documented invariant), so no
    live peer owns it. Idempotent by contract, so re-running is safe. Returns
    the number reset."""
    cur = await db.execute(
        "UPDATE data_migrations SET status='pending', updated_at=? WHERE status='running'",
        (_now(),),
    )
    await db.commit()
    return cur.rowcount


async def claim(db: aiosqlite.Connection, id: str) -> bool:
    """Atomically claim a runnable migration. True iff THIS caller won it.

    ``UPDATE ... WHERE status IN ('pending','failed')`` is atomic on the
    serialized connection: two concurrent runners racing the same row produce
    exactly one rowcount==1 winner; the loser sees 0 and skips. ``failed`` is
    claimable so a transient failure retries on the next boot."""
    cur = await db.execute(
        "UPDATE data_migrations "
        "SET status='running', started_at=?, attempts=attempts+1, updated_at=?, error=NULL "
        "WHERE id=? AND status IN ('pending', 'failed')",
        (_now(), _now(), id),
    )
    await db.commit()
    return cur.rowcount == 1


async def mark_completed(db: aiosqlite.Connection, id: str, *, summary: str) -> None:
    now = _now()
    await db.execute(
        "UPDATE data_migrations SET status='completed', completed_at=?, updated_at=?, "
        "summary=?, error=NULL WHERE id=?",
        (now, now, summary, id),
    )
    await db.commit()


async def mark_failed(db: aiosqlite.Connection, id: str, *, error: str) -> None:
    await db.execute(
        "UPDATE data_migrations SET status='failed', updated_at=?, error=? WHERE id=?",
        (_now(), error[:2000], id),
    )
    await db.commit()


async def get_status(db: aiosqlite.Connection, id: str) -> str | None:
    cur = await db.execute("SELECT status FROM data_migrations WHERE id=?", (id,))
    row = await cur.fetchone()
    return row[0] if row else None


async def get_all(db: aiosqlite.Connection) -> list[dict]:
    """All ledger rows (id-ordered) as dicts — introspection / dashboards."""
    cur = await db.execute(
        "SELECT id, name, status, attempts, started_at, completed_at, error, summary "
        "FROM data_migrations ORDER BY id"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in await cur.fetchall()]
