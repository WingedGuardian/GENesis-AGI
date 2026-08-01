"""d0009 — align pre-existing skill_proposal observations to propose-only.

Two upgrade-path gaps for ``skill_proposal`` observations created by the OLD
skill-evolution applicator (before propose-only shipped), both flagged in review:

1. ``category`` was never stamped (NULL). The new dampening gate matches an
   EXACT ``category = skill_name`` (see ``pipeline._has_pending_proposal``), so a
   NULL-category proposal is missed → a one-off duplicate re-proposal on upgrade.
2. ``expires_at`` was persisted at the old 14-day TTL. Changing ``_TTL_BY_TYPE``
   only affects NEW ``observations.create()`` calls, so an existing unresolved
   proposal keeps its early expiry instead of the new 60-day review window and
   would auto-resolve (``resolve_expired``) before a human reviews it.

Heals both on every install (idempotent, post-boot), tightly scoped to
UNRESOLVED ``skill_proposal`` rows. A fresh install (no such rows) is a clean
no-op. Sync ``migrate()``/``verify()`` on their own connections (framework
contract, cf. d0007) — never the runtime's async ``rt._db``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from genesis.env import genesis_db_path

logger = logging.getLogger(__name__)

requires_operator = False

_NEW_TTL_DAYS = 60


def migrate() -> dict:
    """Backfill ``category`` (from ``content.skill_name``) and extend
    ``expires_at`` to ``created_at`` + 60d for unresolved skill_proposal rows."""
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:
        # 1. category = skill_name where missing (parsed from the content JSON).
        cur = db.execute(
            "UPDATE observations "
            "SET category = json_extract(content, '$.skill_name') "
            "WHERE type = 'skill_proposal' AND resolved = 0 AND category IS NULL "
            "AND json_valid(content) "
            "AND json_extract(content, '$.skill_name') IS NOT NULL"
        )
        category_filled = cur.rowcount

        # 2. Extend expires_at to created_at + 60d where the persisted expiry is
        #    sooner. Computed in Python — matches observations.create()'s
        #    (fromisoformat(created_at) + ttl).isoformat() — to avoid SQLite
        #    datetime() ISO/timezone edge cases.
        rows = db.execute(
            "SELECT id, created_at, expires_at FROM observations "
            "WHERE type = 'skill_proposal' AND resolved = 0"
        ).fetchall()
        expires_bumped = 0
        for oid, created_at, expires_at in rows:
            try:
                target = (
                    datetime.fromisoformat(created_at) + timedelta(days=_NEW_TTL_DAYS)
                ).isoformat()
            except (TypeError, ValueError):
                continue
            if expires_at is None or expires_at < target:
                db.execute("UPDATE observations SET expires_at = ? WHERE id = ?", (target, oid))
                expires_bumped += 1
        db.commit()
    finally:
        db.close()
    logger.info(
        "d0009: skill_proposal category_filled=%d, expires_at_bumped=%d",
        category_filled,
        expires_bumped,
    )
    return {"category_filled": category_filled, "expires_at_bumped": expires_bumped}


def verify() -> bool:
    """Complete when no unresolved skill_proposal that has a ``content.skill_name``
    still lacks a ``category`` (the dampening-correctness invariant). Read via
    ``mode=ro`` (WAL-aware) so it sees migrate()'s just-committed write."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        (null_cat,) = db.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE type = 'skill_proposal' AND resolved = 0 AND category IS NULL "
            "AND json_valid(content) "
            "AND json_extract(content, '$.skill_name') IS NOT NULL"
        ).fetchone()
        return null_cat == 0
    finally:
        db.close()
