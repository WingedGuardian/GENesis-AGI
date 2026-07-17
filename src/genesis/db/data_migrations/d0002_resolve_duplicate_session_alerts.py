"""d0002 — resolve alerts stranded by the duplicate-session guard removal.

WS-D2 removed the duplicate-session guard with cause (its process-liveness
ownership model manufactured false-positive "duplicate executor" conflicts
under the persistent cc-N slot model) — including the awareness-loop
resolver that used to auto-resolve open ``duplicate_session_monitor``
alerts once their conflict cleared. An install upgrading while such an
alert sits open would keep it open forever, since nothing that knows the
source string survives. Resolve them once, everywhere, on the next
pull+restart.

migrate()/verify() are SYNC (framework contract, cf. d0001); the runner
offloads via ``asyncio.to_thread``. Own connections only — never the
runtime's async ``rt._db``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from genesis.env import genesis_db_path

requires_operator = False

_SOURCE = "duplicate_session_monitor"
_NOTES = (
    "auto-resolved by d0002: duplicate-session guard removed "
    "(false-positive by construction under persistent slot sessions); "
    "its awareness-loop resolver no longer exists"
)


def migrate() -> dict:
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:
        cur = db.execute(
            "UPDATE observations SET resolved = 1, resolved_at = ?, "
            "resolution_notes = ? WHERE source = ? AND resolved = 0",
            (datetime.now(UTC).isoformat(), _NOTES, _SOURCE),
        )
        db.commit()
        return {"resolved": cur.rowcount}
    finally:
        db.close()


def verify() -> bool:
    """Complete when no open observation carries the retired source."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = ? AND resolved = 0",
            (_SOURCE,),
        ).fetchone()
        return row[0] == 0
    finally:
        db.close()
