"""CRUD for ``marketing_prospects`` — the owner-curated cold-outreach target set.

Why a NEW store (New-Store Gate justification): the marketing cold-send path needs
a recipient source that is (1) CODE-resolvable at the send chokepoint (never the
LLM — the resolved address is what the autonomy gate's BULK scope guard validates),
(2) opt-out tracked as PERMANENT suppression, and (3) status-queryable
(active/contacted/replied). A prose research note (e.g. ``influencer-research.md``)
satisfies none of these — it cannot be joined against a resolved recipient, carries
no per-row opt-out flag, and has no queryable status. No existing table fits:
``pending_outreach`` is a transient delivery queue; ``outreach_history`` is a
delivered-message log (no target inventory / opt-out); ``campaigns`` holds campaign
config, not per-recipient rows. So this is a distinct, small, owner-curated
inventory.

Retention: opted_out rows are PERMANENT suppression — NEVER pruned (deleting one
would let a re-add silently re-enable a suppressed address). The list is
owner-curated and bounded (a hand-maintained prospect set, not machine-grown), so
there is no unbounded growth to GC. No disk-hygiene prune path is wired by design.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_email(email: str) -> str:
    """Canonical storage + lookup form: stripped + lowercased. Ensures opt-out
    suppression and the UNIQUE(email) index can't be defeated by case/whitespace
    variation (a mixed-case send would otherwise miss an opted-out lowercase row)."""
    return (email or "").strip().lower()


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    email: str,
    name: str | None = None,
    company: str | None = None,
    status: str = "active",
    source: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    """Insert a curated prospect. ``opted_out`` defaults to 0 (may be sent)."""
    now = created_at or _now()
    await db.execute(
        """INSERT INTO marketing_prospects
             (id, email, name, company, status, opted_out, source,
              created_at, updated_at, last_contacted_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)""",
        (id, _normalize_email(email), name, company, status, source, now, updated_at or now),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM marketing_prospects WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_email(db: aiosqlite.Connection, email: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM marketing_prospects WHERE email = ? COLLATE NOCASE",
        (_normalize_email(email),),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_active(db: aiosqlite.Connection) -> list[dict]:
    """Prospects eligible for a cold touch: status='active' AND NOT opted_out."""
    cursor = await db.execute(
        "SELECT * FROM marketing_prospects "
        "WHERE status = 'active' AND opted_out = 0 "
        "ORDER BY created_at"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def is_active_recipient(db: aiosqlite.Connection, email: str) -> bool:
    """True iff ``email`` is an active, non-opted-out prospect. The deterministic
    check the autonomy gate's BULK scope guard uses (fail-closed: unknown → False)."""
    cursor = await db.execute(
        "SELECT 1 FROM marketing_prospects "
        "WHERE email = ? AND status = 'active' AND opted_out = 0 LIMIT 1",
        (_normalize_email(email),),
    )
    return await cursor.fetchone() is not None


async def mark_contacted(
    db: aiosqlite.Connection, id: str, *, contacted_at: str, status: str = "contacted"
) -> bool:
    """Stamp last_contacted_at + advance status. Returns False if id is unknown."""
    cursor = await db.execute(
        "UPDATE marketing_prospects "
        "SET last_contacted_at = ?, status = ?, updated_at = ? WHERE id = ?",
        (contacted_at, status, contacted_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_contacted_by_email(
    db: aiosqlite.Connection, email: str, *, contacted_at: str
) -> bool:
    """Advance the ACTIVE prospect matching ``email`` → contacted. The by-email entry
    point for the delivery paths (which know the resolved recipient, not the id) —
    called on a CONFIRMED delivery so ``list_active`` stops re-pitching the prospect
    (the loop-fix: ``mark_contacted`` had no caller, so a delivered prospect stayed
    'active' and got re-pitched every tick).

    No-op returning False when there is no matching prospect, or the row is not
    'active' (an already-'contacted'/'replied' row is never downgraded, and a
    non-marketing recipient is never touched). Keyed on the RECIPIENT being a curated
    prospect — NOT on cell_risk_class — so a cold pitch that misclassified FINANCIAL
    (money-term body) is still stamped. Only ``opted_out`` — NOT this status — gates
    authorization, so this write can never weaken a send gate."""
    row = await get_by_email(db, email)
    if row is None or row.get("status") != "active":
        return False
    return await mark_contacted(db, row["id"], contacted_at=contacted_at)


async def mark_opted_out(db: aiosqlite.Connection, id: str, *, opted_out_at: str) -> bool:
    """PERMANENT suppression — set opted_out=1. Returns False if id is unknown.
    The row is never pruned, so the address can never be silently re-enabled."""
    cursor = await db.execute(
        "UPDATE marketing_prospects SET opted_out = 1, updated_at = ? WHERE id = ?",
        (opted_out_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0
