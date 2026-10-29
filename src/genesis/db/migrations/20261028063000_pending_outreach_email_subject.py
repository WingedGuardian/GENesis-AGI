"""Carry an email's own subject line through ``pending_outreach``."""


async def up(db):
    row = await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_outreach'"
        )
    ).fetchone()
    if row is None:
        return  # Pre-base path: canonical create_all_tables installs the column.
    cursor = await db.execute("PRAGMA table_info(pending_outreach)")
    cols = {r[1] for r in await cursor.fetchall()}
    if "email_subject" not in cols:
        await db.execute("ALTER TABLE pending_outreach ADD COLUMN email_subject TEXT")
