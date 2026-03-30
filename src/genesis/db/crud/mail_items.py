"""CRUD operations for processed_emails table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    message_id: str,
    imap_uid: int | None = None,
    sender: str,
    subject: str,
    received_at: str | None = None,
    body_preview: str | None = None,
    created_at: str,
    content_hash: str | None = None,
) -> str:
    """Insert a new processed_emails row."""
    await db.execute(
        """INSERT INTO processed_emails
           (id, message_id, imap_uid, sender, subject, received_at,
            body_preview, created_at, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, message_id, imap_uid, sender, subject, received_at,
         body_preview, created_at, content_hash),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    """Get a single row by primary key."""
    cursor = await db.execute(
        "SELECT * FROM processed_emails WHERE id = ?", (id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def exists_by_message_id(db: aiosqlite.Connection, message_id: str) -> bool:
    """Check if a message_id has already been processed."""
    cursor = await db.execute(
        "SELECT 1 FROM processed_emails WHERE message_id = ? LIMIT 1",
        (message_id,),
    )
    return await cursor.fetchone() is not None


async def update_status(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
    processed_at: str | None = None,
    error_message: str | None = None,
    batch_id: str | None = None,
) -> None:
    """Update the status and optional metadata for a row."""
    fields = ["status = ?"]
    values: list = [status]

    if processed_at is not None:
        fields.append("processed_at = ?")
        values.append(processed_at)
    if error_message is not None:
        fields.append("error_message = ?")
        values.append(error_message)
    if batch_id is not None:
        fields.append("batch_id = ?")
        values.append(batch_id)

    values.append(id)
    await db.execute(
        f"UPDATE processed_emails SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    await db.commit()


async def update_layer1_verdict(
    db: aiosqlite.Connection,
    id: str,
    *,
    verdict: str,
) -> None:
    """Set the Layer 1 triage verdict."""
    await db.execute(
        "UPDATE processed_emails SET layer1_verdict = ? WHERE id = ?",
        (verdict, id),
    )
    await db.commit()


async def update_layer1_brief(
    db: aiosqlite.Connection, id: str, *, brief_json: str,
) -> None:
    """Store the Layer 1 paralegal brief as JSON."""
    await db.execute(
        "UPDATE processed_emails SET layer1_brief = ? WHERE id = ?",
        (brief_json, id),
    )
    await db.commit()


async def update_layer2_decision(
    db: aiosqlite.Connection, id: str, *, decision_json: str,
) -> None:
    """Store the Layer 2 judge decision as JSON (KEEP/DISCARD + rationale)."""
    await db.execute(
        "UPDATE processed_emails SET layer2_decision = ? WHERE id = ?",
        (decision_json, id),
    )
    await db.commit()


async def increment_retry(db: aiosqlite.Connection, id: str) -> None:
    """Increment retry_count for a failed item."""
    await db.execute(
        "UPDATE processed_emails SET retry_count = retry_count + 1 WHERE id = ?",
        (id,),
    )
    await db.commit()
