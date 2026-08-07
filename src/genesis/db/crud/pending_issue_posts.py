"""CRUD for ``pending_issue_posts`` — the Contributor Work-Log hold store.

A row is written when the ``contributor_issue_propose`` MCP tool holds a
sanitized issue draft awaiting owner approval (paired with an
``approval_requests`` row). The resolution watcher drains ``status='held'``
rows: on approval it posts the issue to GitHub and marks 'posted'; on
rejection/timeout it marks 'rejected'/'expired'. ``mark_posted``/
``mark_rejected`` gate on ``WHERE status='held'`` so a row can leave 'held'
exactly once (double-post guard, alongside the ``request_id`` UNIQUE
constraint).
"""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    request_id: str,
    repo: str,
    title: str,
    body: str,
    source: str,
    cell_domain: str,
    cell_verb: str,
    cell_risk_class: str,
    held_at: str,
    labels: str | None = None,
    source_ref: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO pending_issue_posts
             (id, request_id, repo, title, body, labels, source, source_ref,
              cell_domain, cell_verb, cell_risk_class, held_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'held')""",
        (
            id,
            request_id,
            repo,
            title,
            body,
            labels,
            source,
            source_ref,
            cell_domain,
            cell_verb,
            cell_risk_class,
            held_at,
        ),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM pending_issue_posts WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_request(db: aiosqlite.Connection, request_id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM pending_issue_posts WHERE request_id = ?", (request_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_held(db: aiosqlite.Connection) -> list[dict]:
    """All rows still awaiting resolution — the watcher's work list."""
    cursor = await db.execute(
        "SELECT * FROM pending_issue_posts WHERE status = 'held' ORDER BY held_at"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def mark_posted(
    db: aiosqlite.Connection,
    id: str,
    *,
    issue_number: int,
    issue_url: str,
    posted_at: str,
) -> bool:
    """Transition held → posted, recording the created issue. Returns False if
    the row already left 'held' (double-post guard: only one caller can flip a
    given hold)."""
    cursor = await db.execute(
        "UPDATE pending_issue_posts "
        "SET status = 'posted', issue_number = ?, issue_url = ?, posted_at = ? "
        "WHERE id = ? AND status = 'held'",
        (issue_number, issue_url, posted_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_rejected(
    db: aiosqlite.Connection, id: str, *, rejected_at: str, expired: bool = False
) -> bool:
    """Transition held → rejected (or expired). Returns False if not still held."""
    status = "expired" if expired else "rejected"
    cursor = await db.execute(
        "UPDATE pending_issue_posts SET status = ?, rejected_at = ? "
        "WHERE id = ? AND status = 'held'",
        (status, rejected_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0
