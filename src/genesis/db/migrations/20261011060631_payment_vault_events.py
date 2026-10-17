"""The payment vault's audit log: who unlocked, locked or was refused, and when.

WHY THIS TABLE EXISTS. The vault service (``payments/vault_service.py``)
holds the card vault's decrypted key in genesis-server memory between an
unlock and a lock. A same-uid process misusing the socket is an accepted
residual, so this trail is how misuse would be noticed: every unlock, lock,
refusal and relock, with the requesting process's pid, start time and names.
It never holds a card value or the passphrase.

WHY NOT AN EXISTING STORE (the New-Store Gate). ``job_run_events`` is per
scheduled job and pruned; ``events`` (the observability bus) is a high-volume
stream with its own retention; ``board_events`` belongs to the work board. This
log is low-volume, kept forever, and has one writer: the vault service.

``event`` deliberately carries NO CHECK constraint: SQLite cannot ALTER a
CHECK, and the vocabulary grows with card release and approval. The closed set
is enforced in ``db/crud/payment_vault_events.py`` (the ``board_events``
precedent). Retention: none. Backup: rides genesis.db.

Additive and idempotent. Fresh installs get the identical DDL from
``schema/_tables.py``; ``tests/test_db/test_payment_vault_events.py`` pins the
two build paths in parity.
"""

from __future__ import annotations

import aiosqlite

TABLES = {
    "payment_vault_events": """
    CREATE TABLE IF NOT EXISTS payment_vault_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        event       TEXT NOT NULL,
        detail      TEXT,
        created_at  TEXT NOT NULL
    )
    """,
}

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_payment_vault_events_created "
    "ON payment_vault_events(created_at)",
)


async def up(db: aiosqlite.Connection) -> None:
    for ddl in TABLES.values():
        await db.execute(ddl)
    for ddl in INDEXES:
        await db.execute(ddl)
