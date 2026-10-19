"""Remember whether API readiness recovered or was deliberately disabled."""


async def up(db):
    columns = {row[1] for row in await (await db.execute("PRAGMA table_info(alert_events)")).fetchall()}
    if "resolution_reason" not in columns:
        await db.execute("ALTER TABLE alert_events ADD COLUMN resolution_reason TEXT")
