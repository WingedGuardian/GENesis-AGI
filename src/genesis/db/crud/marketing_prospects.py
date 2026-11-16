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

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses

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


#: Anything shaped like an address, wherever it sits in the string. A net under
#: the header parser, which gives up on a malformed list ("a@x;b@y", an unclosed
#: "<") and would otherwise leave the whole string as the only thing looked up.
_ADDRESS_SHAPE = re.compile(r'[^\s<>,;"()]+@[^\s<>,;"()]+')
#: Addresses per query; well under SQLite's bound-variable limit.
_LOOKUP_CHUNK = 500


def _recipient_emails(recipient: str | None) -> list[str]:
    """Every normalized address a recipient string names, in first-seen order:
    what the header parser finds, every address-shaped run, and the string as
    given. Empty for a blank recipient."""
    raw = recipient or ""
    if not raw.strip():
        return []
    candidates = [addr for _name, addr in getaddresses([raw])]
    candidates.extend(_ADDRESS_SHAPE.findall(raw))
    candidates.append(raw)
    return list(dict.fromkeys(e for e in map(_normalize_email, candidates) if e))


def _delivery_emails(recipient: str | None) -> list[str]:
    """The normalized addresses a send to ``recipient`` actually reaches.

    Read the way the email adapter sends: the string becomes an
    ``EmailMessage`` To header and ``smtplib.send_message`` delivers to
    ``getaddresses`` of that header. Narrower than ``_recipient_emails`` on
    purpose: an address inside a display name (``"a@x" <b@y>``) is a hold
    candidate but is never sent to, so it must not be stamped contacted.
    Empty when the header cannot be built or parsed, since that send raises
    too; the delivery paths stamp after a send, so this never raises."""
    raw = recipient or ""
    if not raw.strip():
        return []
    msg = EmailMessage()
    try:
        msg["To"] = raw
        headers = [str(h) for h in msg.get_all("To") or []]
        found = [addr for _name, addr in getaddresses(headers)]
    except Exception:  # noqa: BLE001 - an unparseable header was never delivered
        return []
    return list(dict.fromkeys(e for e in map(_normalize_email, found) if e))


async def find_in_recipient(db: aiosqlite.Connection, recipient: str | None) -> dict | None:
    """The curated prospect a recipient STRING names, or None.

    A recipient can arrive as a bare address, as ``Name <addr>``, as a list of
    addresses, or malformed. Every address the header parser finds, every
    address-shaped run in the string, and the string as given are looked up.
    A caller that asks "is this a prospect?" with ``get_by_email`` on the raw
    string gets "no" for every form but the first, which fails OPEN wherever
    "no" means "send". An opted-out row wins over any other match.

    One query per ``_LOOKUP_CHUNK`` addresses, so a very long recipient string
    costs a few queries and not one per address.
    """
    emails = _recipient_emails(recipient)
    found: dict | None = None
    for start in range(0, len(emails), _LOOKUP_CHUNK):
        chunk = emails[start : start + _LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        cursor = await db.execute(
            f"SELECT * FROM marketing_prospects WHERE email COLLATE NOCASE IN ({marks})",  # noqa: S608 — placeholders only
            chunk,
        )
        for row in await cursor.fetchall():
            row = dict(row)
            if row.get("opted_out"):
                return row
            found = found or row
    return found


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
    authorization, so this write can never weaken a send gate.

    ATOMIC: a single conditional UPDATE (``WHERE email = ? COLLATE NOCASE AND
    status = 'active'``), NOT a read-then-write. A concurrent writer flipping the row
    (e.g. active → replied) between a lookup and an update can't be clobbered back to
    'contacted' — the never-downgrade guarantee holds under concurrency. ``rowcount``
    is the stamped/not-stamped signal."""
    cursor = await db.execute(
        "UPDATE marketing_prospects "
        "SET last_contacted_at = ?, status = 'contacted', updated_at = ? "
        "WHERE email = ? COLLATE NOCASE AND status = 'active'",
        (contacted_at, contacted_at, _normalize_email(email)),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_contacted_in_recipient(
    db: aiosqlite.Connection,
    recipient: str | None,
    *,
    contacted_at: str,
    refused: Iterable[str] = (),
) -> int:
    """``mark_contacted_by_email`` for a delivered RECIPIENT STRING: every ACTIVE
    prospect it names goes active → contacted. The delivery paths pass the
    recipient as sent, which can be ``Name <addr>`` or a list; an exact match on
    that string stamps nothing, so a delivered prospect would stay 'active' and be
    pitched again. Addresses come from ``_delivery_emails``: only the ones the
    send reaches, not every candidate ``find_in_recipient`` holds on, less any
    address in ``refused`` (the recipients the server refused on a partly
    accepted send, from the email adapter's delivery id).

    Same guarantees as ``mark_contacted_by_email``: one conditional UPDATE per
    ``_LOOKUP_CHUNK`` addresses (``status = 'active'``), so a row another writer
    moved on is never downgraded, and a non-prospect address is never touched.
    Returns the number of prospects stamped."""
    skip = {e for e in map(_normalize_email, refused) if e}
    emails = [e for e in _delivery_emails(recipient) if e not in skip]
    stamped = 0
    for start in range(0, len(emails), _LOOKUP_CHUNK):
        chunk = emails[start : start + _LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        cursor = await db.execute(
            "UPDATE marketing_prospects "  # noqa: S608 — placeholders only
            "SET last_contacted_at = ?, status = 'contacted', updated_at = ? "
            f"WHERE email COLLATE NOCASE IN ({marks}) AND status = 'active'",
            (contacted_at, contacted_at, *chunk),
        )
        stamped += cursor.rowcount
    await db.commit()
    return stamped


async def mark_opted_out(db: aiosqlite.Connection, id: str, *, opted_out_at: str) -> bool:
    """PERMANENT suppression — set opted_out=1. Returns False if id is unknown.
    The row is never pruned, so the address can never be silently re-enabled."""
    cursor = await db.execute(
        "UPDATE marketing_prospects SET opted_out = 1, updated_at = ? WHERE id = ?",
        (opted_out_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0
