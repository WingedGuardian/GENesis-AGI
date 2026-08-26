"""d0012 — normalize control characters in existing campaign names.

The campaign-name hygiene fix strips control characters at the WRITE boundary
(``crud.create_campaign`` / ``update_campaign`` / the MCP create entry), so the
derived scheduler ``job_id`` (``campaigns/runner.py:118`` → ``f"campaign_{name}"``),
log lines, the ``job_health.job_name`` column, and the reflection note stay
single-line. But rows written BEFORE that fix can still carry newlines or other
control characters, and ``CampaignRunner`` derives the scheduler/job-health id from
the raw stored name — so the garbling persists for existing campaigns until they are
rewritten. This heals the historical rows on EVERY install (idempotent, post-boot):
the gap shipped in the code, so peer installs carry dirty rows too — no per-install
hand-fix.

Tightly scoped: only rows whose ``name`` differs from ``strip_control_chars(name)``.
A UNIQUE(name) collision — the normalized form already names another campaign — is
left untouched and logged; two campaigns are NEVER silently merged onto one name.
A fresh install (no campaigns, or all clean) is a clean no-op.

``migrate()`` / ``verify()`` are SYNC on their own connections (framework contract,
cf. d0005/d0007) — never the runtime's async ``rt._db``.
"""

from __future__ import annotations

import logging
import sqlite3

from genesis.env import genesis_db_path
from genesis.security.sanitizer import strip_control_chars

logger = logging.getLogger(__name__)

requires_operator = False


def migrate() -> dict:
    """Rewrite each control-char-bearing campaign name to its normalized form,
    skipping any whose normalized form would collide with an existing name."""
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    normalized = 0
    skipped_collision = 0
    try:
        rows = db.execute("SELECT id, name FROM campaigns").fetchall()
        names = {name for (_id, name) in rows}
        for cid, name in rows:
            clean = strip_control_chars(name)
            if clean == name:
                continue
            if clean in names:
                # The normalized name already belongs to another campaign — never
                # merge two campaigns onto one name; leave this row for an operator.
                skipped_collision += 1
                logger.warning(
                    "d0012: campaign %s name normalizes to existing %r — left as-is",
                    cid,
                    clean,
                )
                continue
            db.execute("UPDATE campaigns SET name = ? WHERE id = ?", (clean, cid))
            names.discard(name)
            names.add(clean)
            normalized += 1
        db.commit()
    finally:
        db.close()
    logger.info(
        "d0012: normalized %d campaign name(s), skipped %d collision(s)",
        normalized,
        skipped_collision,
    )
    return {"normalized": normalized, "skipped_collision": skipped_collision}


def verify() -> bool:
    """Complete when no campaign name is still fixable — i.e. every remaining
    control-char-bearing name is one whose normalized form collides with another
    campaign (intentionally left as-is). Read via ``mode=ro`` (WAL-aware).
    """
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT name FROM campaigns").fetchall()
        names = {name for (name,) in rows}
        for (name,) in rows:
            clean = strip_control_chars(name)
            if clean != name and clean not in names:
                return False  # a still-fixable dirty name remains
        return True
    finally:
        db.close()
